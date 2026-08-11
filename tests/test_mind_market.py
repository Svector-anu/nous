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


class FundedBuyer(surplus.Buyer):
    """A buyer whose wallet answer is scripted."""

    def __init__(self, *, balance=0.0, credit=0.0, allowance=0.0, key="inf_test"):
        super().__init__(api_key=key)
        self.payload = {
            "balance_usdc": balance,
            "credit_balance_usdc": credit,
            "allowance_usdc": allowance,
            "wallet": "0xabc",
        }

    async def _fetch_me(self):
        return self.payload


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    with TestClient(create_app(CONFIG, tmp_path / "w.db")) as client:
        client.app.state.mind_catalog = FakeCatalog()
        # A world that can pay. The gate below is tested explicitly; every other test here
        # is about what happens once the world is genuinely able to sell.
        client.app.state.mind_buyer = FundedBuyer(balance=5.0, allowance=100.0)
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


def test_a_world_with_no_key_is_a_shop_window(client):
    """Visitors should see what a mind costs before one can be sold, but must not be told
    it is for sale. Distinct from the funded-but-empty case below: this one nobody has set
    up, and the ui says so differently."""
    client.app.state.mind_buyer = surplus.Buyer()  # no key
    body = client.get("/minds/catalog").json()

    assert body["buying"] is False
    assert body["configured"] is False
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


# --- selling only what the world can actually deliver -------------------------------
#
# A key proves somebody configured this. It does not prove the world can pay: settlement
# pulls USDC from the operator's wallet per request, so an empty wallet means every call
# fails *after* the visitor has paid. The catalog said "buying" while the wallet held
# nothing, which is taking money for a mind that could never think.


def _with_buyer(client, buyer):
    client.app.state.mind_buyer = buyer
    return client


def test_a_funded_and_approved_wallet_can_sell(client):
    _with_buyer(client, FundedBuyer(balance=5.0, allowance=100.0))
    body = client.get("/minds/catalog").json()

    assert body["buying"] is True
    assert body["configured"] is True


def test_an_empty_wallet_cannot_sell(client):
    """The live failure. A key was set, the catalog said buying, and the wallet held
    nothing."""
    _with_buyer(client, FundedBuyer(balance=0.0, allowance=0.0))
    body = client.get("/minds/catalog").json()

    assert body["buying"] is False
    assert body["configured"] is True, "it is configured — the wallet is the problem"
    assert body["models"], "prices should still be visible"


def test_money_with_no_allowance_cannot_sell():
    """Settlement pulls with transferFrom, so USDC nobody has approved is USDC the
    marketplace cannot take."""
    buyer = FundedBuyer(balance=50.0, allowance=0.0)
    import asyncio

    asyncio.run(buyer.refresh_funding())
    assert buyer.ready is False


def test_credit_needs_no_allowance():
    """Marketplace credit spends without an on-chain pull, so requiring an allowance for
    it would refuse a buyer who can genuinely pay."""
    buyer = FundedBuyer(balance=0.0, credit=5.0, allowance=0.0)
    import asyncio

    asyncio.run(buyer.refresh_funding())
    assert buyer.ready is True


def test_a_buyer_that_has_never_checked_sells_nothing():
    """The unchecked state must not sell. Defaulting the other way would mean any restart
    briefly offered minds it could not deliver."""
    assert surplus.Buyer(api_key="inf_test").ready is False


def test_buying_is_refused_before_the_money_is_taken(client):
    """The whole point. Refusing after payment would leave somebody having paid for
    nothing."""
    _with_buyer(client, FundedBuyer(balance=0.0, allowance=0.0))
    agent = _agent(client)
    response = client.post(
        f"/agents/{agent}/mind/buy", json={"model": "cheap"}, headers=_paid()
    )

    assert response.status_code == 503
    world = client.app.state.simulation.world
    assert world.try_get(agent, AttachedMind) is None, "a mind was attached anyway"


def test_a_refused_buy_does_not_burn_the_payment(client):
    """The proof must still be spendable afterwards. Marking it used on a sale that never
    happened would cost somebody a payment for nothing."""
    _with_buyer(client, FundedBuyer(balance=0.0, allowance=0.0))
    agent = _agent(client)
    client.post(f"/agents/{agent}/mind/buy", json={"model": "cheap"}, headers=_paid())

    _with_buyer(client, FundedBuyer(balance=5.0, allowance=100.0))
    again = client.post(
        f"/agents/{agent}/mind/buy", json={"model": "cheap"}, headers=_paid()
    )
    assert again.status_code == 202, again.text


def test_an_outage_does_not_close_the_shop(client):
    """A marketplace having a bad minute is not a reason to stop selling to everybody, so
    a failed check keeps the last known answer."""
    buyer = FundedBuyer(balance=5.0, allowance=100.0)
    _with_buyer(client, buyer)
    assert client.get("/minds/catalog").json()["buying"] is True

    async def broken():
        raise RuntimeError("surplus is down")

    buyer._fetch_me = broken
    buyer._funding_at = 0.0  # force a refresh
    assert client.get("/minds/catalog").json()["buying"] is True
