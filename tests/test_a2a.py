"""A2A: discovery and paid skills for callers that are not people.

The point of these is that the a2a binding cannot drift away from the http routes. It is a
second door onto the same world, and the first version of it re-opened the replay hole the
http route had closed hours earlier — the shared helper returns an outcome *string* and
every non-success value is truthy, so `if not outcome` read a refusal as a success.
"""

from __future__ import annotations

import pathlib

from starlette.testclient import TestClient

from src.api import a2a
from src.api.server import create_app
from src.world.components import ForceDecisionQueue
from src.world.config import WorldConfig

TX = "0x" + "a" * 64


def _paid_world(tmp_path: pathlib.Path):
    config = WorldConfig(
        agent_count=6,
        resource_count=10,
        x402_enabled=True,
        llm_force_decision_enabled=True,
    )
    return create_app(config, tmp_path / "a2a.db"), config


def _send(client: TestClient, data: dict, metadata: dict | None = None) -> dict:
    message: dict = {
        "kind": "message",
        "role": "user",
        "messageId": "m1",
        "parts": [{"kind": "data", "data": data}],
    }
    if metadata:
        message["metadata"] = metadata
    response = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {"message": message},
        },
    )
    return response.json()


def _pay(tx: str = TX) -> dict:
    return {a2a.PAYMENT_PAYLOAD: {"transaction": tx, "verified": "true"}}


# --- discovery ---------------------------------------------------------------------


def test_the_card_is_served_at_the_well_known_path(tmp_path):
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json").json()

    assert card["name"] == "Nous"
    assert card["protocolVersion"]
    assert [s["id"] for s in card["skills"]] == ["observe-world", "read-decisions", "nudge-clan"]


def test_the_older_card_path_still_answers(tmp_path):
    """0.3 renamed the file. A client that only knows the 0.2.5 name is old, not wrong,
    and answering nothing makes discovery fail for no reason."""
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        assert client.get("/.well-known/agent.json").status_code == 200


def test_the_card_declares_the_x402_extension(tmp_path):
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json").json()

    uris = [e["uri"] for e in card["capabilities"]["extensions"]]
    assert a2a.X402_EXTENSION_URI in uris


def test_a_world_without_payments_advertises_no_paid_skill(tmp_path):
    """Advertising a skill nothing will honour wastes a caller's time and its gas."""
    app = create_app(WorldConfig(agent_count=4, resource_count=8), tmp_path / "free.db")
    with TestClient(app) as client:
        card = client.get("/.well-known/agent-card.json").json()

    assert "nudge-clan" not in [s["id"] for s in card["skills"]]
    assert "extensions" not in card["capabilities"]


def test_the_card_url_follows_the_forwarded_host(tmp_path):
    """Behind a proxy the request url is the internal one. A card advertising
    http://testserver is discoverable by nothing."""
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        card = client.get(
            "/.well-known/agent-card.json",
            headers={"x-forwarded-proto": "https", "x-forwarded-host": "nous.city"},
        ).json()

    assert card["url"] == "https://nous.city/a2a"


# --- free skills -------------------------------------------------------------------


def test_observe_world_returns_the_world(tmp_path):
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        task = _send(client, {"skill": "observe-world"})["result"]

    assert task["status"]["state"] == "completed"
    data = task["artifacts"][0]["parts"][1]["data"]
    assert data["stats"]["agents"] == 6


def test_a_message_with_no_skill_is_told_what_exists(tmp_path):
    """A caller that sent only prose gets the list rather than a guess at its intent."""
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        task = _send(client, {})["result"]

    text = task["status"]["message"]["parts"][0]["text"]
    assert task["status"]["state"] == "failed"
    for skill in a2a.FREE_SKILLS + a2a.PAID_SKILLS:
        assert skill in text


def test_an_unsupported_method_is_a_jsonrpc_error(tmp_path):
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        body = client.post(
            "/a2a", json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get"}
        ).json()

    assert body["error"]["code"] == -32601


# --- the paid skill ----------------------------------------------------------------


def test_an_unpaid_nudge_asks_for_payment_in_extension_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    app, config = _paid_world(tmp_path)
    with TestClient(app) as client:
        task = _send(client, {"skill": "nudge-clan", "clan_id": 1})["result"]

    # Not a protocol error: the task is waiting on the caller, exactly like one waiting
    # on a missing argument. What makes it a payment is the metadata.
    assert task["status"]["state"] == "input-required"
    metadata = task["status"]["message"]["metadata"]
    assert metadata[a2a.PAYMENT_STATUS] == "payment-required"
    accepts = metadata[a2a.PAYMENT_REQUIRED]["accepts"][0]
    assert accepts["chainId"] == config.chain_id
    # Integer token units, not the decimal string — a client should never have to guess
    # at decimals to build a transfer.
    assert accepts["maxAmountRequired"] == "100000"


def test_the_challenge_offers_every_rail_the_world_accepts(tmp_path, monkeypatch):
    """`accepts` is plural in the spec precisely so a payer can choose one it can settle.
    Offering only the asset this world started with is how a payment endpoint ends up
    discoverable by wallets that cannot pay it."""
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        task = _send(client, {"skill": "nudge-clan", "clan_id": 1})["result"]

    offered = task["status"]["message"]["metadata"][a2a.PAYMENT_REQUIRED]["accepts"]
    chains = {option["chainId"] for option in offered}
    assert 4663 in chains, "the original rail must stay first and present"
    assert 8453 in chains, "base is where the counterparty wallets are"
    for option in offered:
        assert option["payTo"], "a rail with no recipient must never be advertised"
        assert int(option["maxAmountRequired"]) > 0


def test_a_world_with_no_recipient_advertises_no_rail(tmp_path, monkeypatch):
    """A payment option with an empty payTo is a locked door with a painted-on keyhole."""
    monkeypatch.delenv("X402_RECIPIENT_ADDRESS", raising=False)
    monkeypatch.delenv("BASE_RECIPIENT_ADDRESS", raising=False)
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        task = _send(client, {"skill": "nudge-clan", "clan_id": 1})["result"]

    assert task["status"]["message"]["metadata"][a2a.PAYMENT_REQUIRED]["accepts"] == []


def test_a_paid_nudge_queues_the_decision(tmp_path):
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        task = _send(client, {"skill": "nudge-clan", "clan_id": 1}, _pay())["result"]
        world = app.state.simulation.world
        queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)
        pending = list(queue.pending)

    assert task["status"]["state"] == "completed"
    data = task["artifacts"][0]["parts"][1]["data"]
    assert data[a2a.PAYMENT_STATUS] == "payment-completed"
    assert pending and pending[-1]["clan_id"] == 1
    # The receipt has to carry what was charged, the same as over http.
    assert pending[-1]["amount"] == "0.10"


def test_a_replayed_payment_is_refused_over_a2a_too(tmp_path):
    """The bug this exists for: the outcome is a string, and every refusal is truthy, so
    reading it as a boolean made a spent proof come back `completed` — the same hole the
    http route had already closed."""
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        first = _send(client, {"skill": "nudge-clan", "clan_id": 1}, _pay())["result"]
        second = _send(client, {"skill": "nudge-clan", "clan_id": 2}, _pay())["result"]

    assert first["status"]["state"] == "completed"
    assert second["status"]["state"] == "failed"
    assert "already been used" in second["status"]["message"]["parts"][0]["text"]


def test_a_nudge_without_a_clan_id_is_refused(tmp_path):
    app, _ = _paid_world(tmp_path)
    with TestClient(app) as client:
        task = _send(client, {"skill": "nudge-clan"}, _pay())["result"]

    assert task["status"]["state"] == "failed"
    assert "clan_id" in task["status"]["message"]["parts"][0]["text"]


def test_the_paid_skill_is_refused_when_payments_are_off(tmp_path):
    app = create_app(WorldConfig(agent_count=4, resource_count=8), tmp_path / "off.db")
    with TestClient(app) as client:
        task = _send(client, {"skill": "nudge-clan", "clan_id": 1}, _pay())["result"]

    assert task["status"]["state"] == "failed"
