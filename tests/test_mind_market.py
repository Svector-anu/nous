"""Buying a mind over http.

Two routes: what a mind costs, and paying for one. The second is money, so it carries the
same guards as every other paid route here — a proof spends once, an unpaid request gets a
402 listing every rail, and nobody but the owner may spend on an agent.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from src.api.server import create_app
from src.llm import surplus
from src.world import spend as governor
from src.world.components import Agent, AttachedMind, SpendBook
from src.world.config import WorldConfig

PRICES = {
    "models": [
        {
            "model": "cheap",
            "displayName": "Cheap",
            "providers": [{"providerName": "P", "pricing": {"input": 1.0, "output": 2.0}}],
        },
        {
            "model": "dear",
            "displayName": "Dear",
            "providers": [{"providerName": "P", "pricing": {"input": 30.0, "output": 60.0}}],
        },
    ]
}

TX = "0x" + "b" * 64


class FakeCatalog(surplus.Catalog):
    async def _fetch(self):
        return PRICES


CONFIG = WorldConfig(
    agent_count=4,
    resource_count=8,
    x402_enabled=True,
    attached_minds_enabled=True,
    world_funding_enabled=True,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    with TestClient(create_app(CONFIG, tmp_path / "w.db")) as client:
        client.app.state.mind_catalog = FakeCatalog()
        yield client


def _agent(client, name="mira"):
    response = client.post("/agents", json={"name": name, "personality": "curious"})
    assert response.status_code in (200, 201, 202), response.text
    world = client.app.state.simulation.world
    # The spawn queues; run it into existence.
    for _ in range(60):
        client.app.state.simulation.step()
        found = [
            e
            for e in world.query(Agent)
            if world.get(e, Agent).name == name and world.get(e, Agent).user_deployed
        ]
        if found:
            return found[0]
    raise AssertionError("the agent never spawned")


def _paid():
    return {"X402-Payment-Verified": "true", "X402-Transaction-Hash": TX}


# --- the price list ----------------------------------------------------------------


def test_the_catalog_is_served(client):
    body = client.get("/minds/catalog").json()

    assert [m["model"] for m in body["models"]] == ["cheap", "dear"]
    assert body["enabled"] is True


def test_every_quoted_cost_is_called_an_estimate(client):
    """It is a floor: the real charge is the token usage the call reports back. A field
    named `units_per_steer` invites somebody to bill from it."""
    body = client.get("/minds/catalog").json()

    for model in body["models"]:
        assert any(key.startswith("estimated_") for key in model)
        assert not any(key == "units_per_steer" for key in model)


def test_the_catalog_says_whether_anything_can_actually_be_bought(client):
    """A price list with no key behind it is a shop window. Visitors should be able to see
    what a mind costs before one can be sold, but not be told it is for sale."""
    body = client.get("/minds/catalog").json()

    assert body["buying"] is False
    assert body["models"], "the list should still show prices"


# --- buying ------------------------------------------------------------------------


def test_buying_without_paying_returns_every_rail(client):
    agent = _agent(client)
    response = client.post(f"/agents/{agent}/mind/buy", json={"model": "cheap"})

    assert response.status_code == 402
    assert response.json()["detail"]["accepts"], "no rails offered"


def test_a_paid_buy_sets_the_model_and_credits_the_agent(client):
    agent = _agent(client)
    response = client.post(
        f"/agents/{agent}/mind/buy", json={"model": "cheap"}, headers=_paid()
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["model"] == "cheap"
    assert body["credited_units"] > 0
    assert body["balance_units"] == body["credited_units"]

    world = client.app.state.simulation.world
    assert world.get(agent, AttachedMind).model == "cheap"


def test_the_money_lands_on_the_agent_not_only_the_envelope(client):
    """Their model, their bill. A credit that raised the envelope alone would have the
    operator paying for a visitor's model, which is the cost this feature exists to stop
    growing."""
    agent = _agent(client)
    client.post(f"/agents/{agent}/mind/buy", json={"model": "cheap"}, headers=_paid())

    world = client.app.state.simulation.world
    book = world.get(world.first(SpendBook), SpendBook)
    assert book.balances.get(governor.key(agent), 0) > 0


def test_a_payment_cannot_be_spent_twice(client):
    """A transaction hash is public on the explorer by design. The only thing that makes
    it worth anything is the server refusing it the second time."""
    agent = _agent(client)
    first = client.post(f"/agents/{agent}/mind/buy", json={"model": "cheap"}, headers=_paid())
    assert first.status_code == 202

    second = client.post(f"/agents/{agent}/mind/buy", json={"model": "dear"}, headers=_paid())
    assert second.status_code == 409

    world = client.app.state.simulation.world
    assert world.get(agent, AttachedMind).model == "cheap", "a replay changed the model"


def test_a_model_that_is_not_on_the_list_is_refused(client):
    """The gate that keeps an untrusted string from becoming something the world acts on.
    A visitor names a model, never a url — this is what makes that distinction real."""
    agent = _agent(client)
    response = client.post(
        f"/agents/{agent}/mind/buy", json={"model": "http://evil.example/v1"}, headers=_paid()
    )

    assert response.status_code == 400


def test_naming_no_model_is_refused(client):
    agent = _agent(client)
    response = client.post(f"/agents/{agent}/mind/buy", json={}, headers=_paid())

    assert response.status_code == 400


def test_an_unknown_agent_cannot_be_bought_for(client):
    response = client.post("/agents/999999/mind/buy", json={"model": "cheap"}, headers=_paid())

    assert response.status_code == 404


def test_buying_is_refused_when_minds_are_switched_off(tmp_path, monkeypatch):
    from dataclasses import replace as _replace

    monkeypatch.setenv("ATTACHED_MINDS_ENABLED", "false")
    off = _replace(CONFIG, attached_minds_enabled=False)
    with TestClient(create_app(off, tmp_path / "off.db")) as client:
        client.app.state.mind_catalog = FakeCatalog()
        response = client.post("/agents/1/mind/buy", json={"model": "cheap"}, headers=_paid())

    assert response.status_code == 403
