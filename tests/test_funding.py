"""Paying for the world's thinking.

Clan leaders consult a model and somebody pays for every call. Until now that was always
the operator, which caps how much the world can think at whatever one person will spend.
This is the seam that lets the people watching fund it instead.

It is the second place money enters the world, so it goes through the same guard as the
first — a second replay check would be a second thing to keep correct, and the first one
was already wrong once.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace

from starlette.testclient import TestClient

from src.api import a2a
from src.api.server import create_app
from src.world import spend as governor
from src.world.components import ForceDecisionQueue, SpendBook
from src.world.config import WorldConfig

TX = "0x" + "a" * 64

CONFIG = WorldConfig(
    agent_count=4,
    resource_count=8,
    x402_enabled=True,
    llm_force_decision_enabled=True,
    world_funding_enabled=True,
)


def _paid(tx: str = TX) -> dict:
    return {"X402-Payment-Verified": "true", "X402-Transaction-Hash": tx}


def _book(app) -> SpendBook:
    world = app.state.simulation.world
    return world.get(world.first(SpendBook), SpendBook)


# --- the happy path -----------------------------------------------------------------


def test_a_payment_raises_the_envelope(tmp_path: pathlib.Path):
    app = create_app(CONFIG, tmp_path / "f.db")
    with TestClient(app) as client:
        before = _book(app).budget_units
        response = client.post("/world/fund", headers=_paid())
        after = _book(app).budget_units

    assert response.status_code == 202
    assert response.json()["units"] > 0
    assert after > before


def test_the_credit_lands_in_the_same_audit_trail_as_an_operator_s(tmp_path: pathlib.Path):
    """`approve` is what an operator calls. a payment doing the same thing is the whole
    idea, and it must be as legible afterwards."""
    app = create_app(CONFIG, tmp_path / "f.db")
    with TestClient(app) as client:
        client.post("/world/fund", headers=_paid())
        book = _book(app)

    assert book.approvals, "a funded envelope left no record of who funded it"
    assert book.approvals[-1]["by"].startswith("payment:")


def test_funding_does_not_switch_spending_on(tmp_path: pathlib.Path):
    """Money arriving is not permission to spend it. an operator still decides that."""
    app = create_app(CONFIG, tmp_path / "f.db")
    with TestClient(app) as client:
        client.post("/world/fund", headers=_paid())
        assert _book(app).enabled is False


# --- refusals -----------------------------------------------------------------------


def test_the_same_payment_cannot_fund_twice(tmp_path: pathlib.Path):
    app = create_app(CONFIG, tmp_path / "f.db")
    with TestClient(app) as client:
        assert client.post("/world/fund", headers=_paid()).status_code == 202
        assert client.post("/world/fund", headers=_paid()).status_code == 409


def test_a_payment_spent_on_a_decision_cannot_then_fund(tmp_path: pathlib.Path):
    """Two doors, one spent set. Without that, one transaction buys a decision *and* an
    envelope."""
    app = create_app(CONFIG, tmp_path / "f.db")
    with TestClient(app) as client:
        assert client.post("/clans/1/force-decision", headers=_paid()).status_code == 202
        assert client.post("/world/fund", headers=_paid()).status_code == 409


def test_an_unpaid_request_is_a_402_carrying_every_rail(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    app = create_app(CONFIG, tmp_path / "f.db")
    with TestClient(app) as client:
        response = client.post("/world/fund")

    assert response.status_code == 402
    chains = {option["chain_id"] for option in response.json()["detail"]["accepts"]}
    assert {4663, 8453} <= chains


def test_a_refused_payment_does_not_burn_the_hash(tmp_path: pathlib.Path):
    """Claiming before verifying must not let one bad request permanently spend a hash
    somebody goes on to pay with for real."""
    app = create_app(CONFIG, tmp_path / "f.db")
    with TestClient(app) as client:
        world = app.state.simulation.world
        queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)

        assert client.post("/world/fund", headers={"X402-Transaction-Hash": TX}).status_code == 402
        assert f"tx:{TX}" not in queue.spent

        assert client.post("/world/fund", headers=_paid()).status_code == 202


def test_funding_is_refused_when_it_is_not_enabled(tmp_path: pathlib.Path):
    """Taking money for compute is a decision, not a default."""
    app = create_app(replace(CONFIG, world_funding_enabled=False), tmp_path / "off.db")
    with TestClient(app) as client:
        assert client.post("/world/fund", headers=_paid()).status_code == 403


# --- through a2a --------------------------------------------------------------------


def _send(client: TestClient, data: dict, metadata: dict | None = None) -> dict:
    message: dict = {
        "kind": "message", "role": "user", "messageId": "m1",
        "parts": [{"kind": "data", "data": data}],
    }
    if metadata:
        message["metadata"] = metadata
    return client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": message}},
    ).json()["result"]


def test_an_agent_can_fund_the_world(tmp_path: pathlib.Path):
    app = create_app(CONFIG, tmp_path / "f.db")
    with TestClient(app) as client:
        task = _send(
            client,
            {"skill": "fund-the-minds"},
            {a2a.PAYMENT_PAYLOAD: {"transaction": TX, "verified": "true"}},
        )

    assert task["status"]["state"] == "completed"
    data = task["artifacts"][0]["parts"][1]["data"]
    assert data["units"] > 0
    assert data[a2a.PAYMENT_STATUS] == "payment-completed"


def test_an_unpaid_agent_gets_a_payment_challenge(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    app = create_app(CONFIG, tmp_path / "f.db")
    with TestClient(app) as client:
        task = _send(client, {"skill": "fund-the-minds"})

    assert task["status"]["state"] == "input-required"
    metadata = task["status"]["message"]["metadata"]
    assert metadata[a2a.PAYMENT_STATUS] == "payment-required"


def test_the_card_advertises_funding_only_when_it_is_on(tmp_path: pathlib.Path):
    app = create_app(CONFIG, tmp_path / "on.db")
    with TestClient(app) as client:
        on = [s["id"] for s in client.get("/.well-known/agent-card.json").json()["skills"]]

    app = create_app(replace(CONFIG, world_funding_enabled=False), tmp_path / "off.db")
    with TestClient(app) as client:
        off = [s["id"] for s in client.get("/.well-known/agent-card.json").json()["skills"]]

    assert "fund-the-minds" in on
    assert "fund-the-minds" not in off


# --- what a funded world can then do ------------------------------------------------


def test_a_funded_envelope_is_spendable_once_an_operator_enables_it(tmp_path: pathlib.Path):
    """The end of the loop: a stranger's payment becomes a decision the world can afford
    to think about."""
    app = create_app(CONFIG, tmp_path / "f.db")
    with TestClient(app) as client:
        client.post("/world/fund", headers=_paid())
        book = _book(app)
        book.enabled = True
        governor.credit(book, 1, book.budget_units)

        assert governor.refuse(book, 1, 1, tick=1) == ""
