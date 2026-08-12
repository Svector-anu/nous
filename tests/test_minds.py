"""Minds a visitor brought: something outside this process driving one agent.

The mind calls in and the world never calls out, so there is no transport to stub and no
network anywhere here. What these cover is the door: who may steer, what a steer is
allowed to do, and what a hostile caller cannot make the world do.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace

from starlette.testclient import TestClient

from src.api.server import create_app
from src.world.components import Agent, AttachedMind, MindQueue, Outbox, ResourceKind
from src.world.config import WorldConfig
from src.world.systems import minds
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(
    seed=7,
    agent_count=6,
    resource_count=12,
    attached_minds_enabled=True,
    attached_mind_cooldown_ticks=5,
)


def _world_with_mind(config=CONFIG, token="secret-token", **kwargs):
    world = create_world(config)
    entity = next(iter(world.query(Agent)))
    world.add(
        entity,
        AttachedMind(owner="0xabc", token_hash=minds.hash_token(token), **kwargs),
    )
    return world, entity


# --- the seam -----------------------------------------------------------------------


def test_a_steer_sets_what_the_agent_wants():
    world, entity = _world_with_mind()
    world.get(entity, Agent).wants = ResourceKind.FOOD
    minds.enqueue_steer(world, entity, wants="wood")

    Simulation(world).run(1)
    assert world.get(entity, Agent).wants is ResourceKind.WOOD


def test_a_mind_gains_no_power_the_agent_did_not_have():
    """It picks differently, never better. Both values were already reachable by the
    state machine — without this a paid mind is a bought advantage."""
    assert set(minds.WANTS) == {"food", "wood"}
    assert set(minds.WANTS.values()) == {ResourceKind.FOOD, ResourceKind.WOOD}


def test_a_steer_waits_for_a_tick_rather_than_landing_on_arrival():
    """A steer is an input to the simulation. Applying it when the request lands would
    make history depend on wall clock."""
    world, entity = _world_with_mind()
    world.get(entity, Agent).wants = ResourceKind.FOOD
    minds.enqueue_steer(world, entity, wants="wood")

    assert world.get(entity, Agent).wants is ResourceKind.FOOD  # not yet
    Simulation(world).run(1)
    assert world.get(entity, Agent).wants is ResourceKind.WOOD


def test_a_world_with_the_feature_off_discards_what_it_was_sent():
    """Rather than accumulating it against being switched on later."""
    world, entity = _world_with_mind(config=replace(CONFIG, attached_minds_enabled=False))
    world.get(entity, Agent).wants = ResourceKind.FOOD
    minds.enqueue_steer(world, entity, wants="wood")

    Simulation(world).run(2)
    assert world.get(entity, Agent).wants is ResourceKind.FOOD
    assert world.get(world.first(MindQueue), MindQueue).pending == []


# --- what a hostile caller can do ---------------------------------------------------


def test_an_unknown_want_changes_nothing():
    """A caller that sends nonsense should change nothing, not something arbitrary."""
    world, entity = _world_with_mind()
    world.get(entity, Agent).wants = ResourceKind.FOOD
    mind = world.get(entity, AttachedMind)

    assert minds.apply(world, entity, mind, {"wants": "gold"}) is False
    assert world.get(entity, Agent).wants is ResourceKind.FOOD


def test_an_empty_steer_changes_nothing():
    world, entity = _world_with_mind()
    mind = world.get(entity, AttachedMind)
    assert minds.apply(world, entity, mind, {}) is False


def test_speech_is_truncated():
    world, entity = _world_with_mind()
    minds.enqueue_steer(world, entity, say="x" * 5000)
    Simulation(world).run(1)

    assert len(world.get(entity, AttachedMind).last_said) == minds.MAX_SAY


def test_speech_goes_through_the_ordinary_message_path():
    """A mind that could talk faster than the world does would be its own advantage."""
    world, entity = _world_with_mind()
    minds.enqueue_steer(world, entity, say="meet me at the river")
    Simulation(world).run(1)

    messages = world.get(entity, Outbox).messages
    assert messages and messages[-1]["text"] == "meet me at the river"


def test_posting_in_a_loop_cannot_fill_the_queue():
    """One mind must not be able to starve every other by posting repeatedly."""
    world, entity = _world_with_mind()
    for index in range(500):
        minds.enqueue_steer(world, entity, wants="wood", say=f"n{index}")

    pending = world.get(world.first(MindQueue), MindQueue).pending
    assert len(pending) == 1, "a repeated steer should replace, not accumulate"
    assert pending[0]["say"] == "n499"


def test_the_queue_is_bounded_across_many_agents():
    world, _ = _world_with_mind()
    for entity in range(1000, 1000 + minds.MAX_PENDING * 3):
        minds.enqueue_steer(world, entity, wants="food")

    pending = world.get(world.first(MindQueue), MindQueue).pending
    assert len(pending) <= minds.MAX_PENDING


def test_a_steer_for_a_dead_agent_is_dropped():
    world, entity = _world_with_mind()
    minds.enqueue_steer(world, 999999, wants="wood")
    Simulation(world).run(1)  # must not raise


# --- the rate limit -----------------------------------------------------------------


def test_the_world_listens_on_its_own_cadence():
    """A mind may post as often as it likes; the rate limit lives in the world.

    Asserted on the steer count rather than on `wants`, because the state machine writes
    `wants` every tick for its own reasons — a steer that was correctly refused and a
    rule that happened to pick the same thing are indistinguishable from the outside.
    """
    world, entity = _world_with_mind()
    mind = world.get(entity, AttachedMind)

    minds.enqueue_steer(world, entity, wants="wood")
    Simulation(world).run(1)
    assert mind.steers == 1

    # Immediately again, inside the cooldown.
    minds.enqueue_steer(world, entity, wants="food")
    Simulation(world).run(1)
    assert mind.steers == 1, "a steer inside the cooldown was applied"

    # Once the cooldown lapses it takes effect.
    Simulation(world).run(CONFIG.attached_mind_cooldown_ticks)
    minds.enqueue_steer(world, entity, wants="wood")
    Simulation(world).run(1)
    assert mind.steers == 2


# --- tokens -------------------------------------------------------------------------


def test_the_token_is_never_stored():
    """World state is saved to disk and shipped in backups. A credential that exists only
    as a hash cannot leak from either."""
    world, entity = _world_with_mind(token="secret-token")
    mind = world.get(entity, AttachedMind)

    assert "secret-token" not in mind.token_hash
    assert minds.token_matches(mind, "secret-token") is True
    assert minds.token_matches(mind, "secret-token ") is False
    assert minds.token_matches(mind, "") is False


def test_a_mind_with_no_token_matches_nothing():
    world, entity = _world_with_mind()
    mind = world.get(entity, AttachedMind)
    mind.token_hash = ""
    assert minds.token_matches(mind, "") is False
    assert minds.token_matches(mind, "anything") is False


# --- the door, end to end -----------------------------------------------------------


def _send(client: TestClient, data: dict) -> dict:
    return client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "kind": "message",
                    "role": "user",
                    "messageId": "m1",
                    "parts": [{"kind": "data", "data": data}],
                }
            },
        },
    ).json()["result"]


def test_a_mind_can_observe_and_steer_its_own_agent(tmp_path: pathlib.Path):
    app = create_app(CONFIG, tmp_path / "minds.db")
    with TestClient(app) as client:
        world = app.state.simulation.world
        entity = next(iter(world.query(Agent)))
        world.get(entity, Agent).user_deployed = True

        issued = client.post(f"/agents/{entity}/mind").json()
        token = issued["token"]

        seen = _send(client, {"skill": "observe-my-agent", "agent_id": entity, "token": token})
        assert seen["status"]["state"] == "completed"
        assert seen["artifacts"][0]["parts"][1]["data"]["choices"] == ["food", "wood"]

        steered = _send(
            client,
            {"skill": "steer-my-agent", "agent_id": entity, "token": token, "wants": "wood"},
        )
        assert steered["artifacts"][0]["parts"][1]["data"]["queued"] is True


def test_a_wrong_token_cannot_steer_somebody_elses_agent(tmp_path: pathlib.Path):
    app = create_app(CONFIG, tmp_path / "minds.db")
    with TestClient(app) as client:
        world = app.state.simulation.world
        entity = next(iter(world.query(Agent)))
        world.get(entity, Agent).user_deployed = True
        client.post(f"/agents/{entity}/mind")

        refused = _send(
            client,
            {"skill": "steer-my-agent", "agent_id": entity, "token": "guessed", "wants": "wood"},
        )
        assert refused["status"]["state"] == "failed"
        assert world.get(world.first(MindQueue), MindQueue).pending == []


def test_a_bad_id_and_a_bad_token_are_refused_identically(tmp_path: pathlib.Path):
    """Telling a caller which agents exist is a way of enumerating them."""
    app = create_app(CONFIG, tmp_path / "minds.db")
    with TestClient(app) as client:
        world = app.state.simulation.world
        entity = next(iter(world.query(Agent)))
        world.get(entity, Agent).user_deployed = True
        client.post(f"/agents/{entity}/mind")

        missing = _send(client, {"skill": "steer-my-agent", "agent_id": 999999, "token": "x"})
        wrong = _send(client, {"skill": "steer-my-agent", "agent_id": entity, "token": "x"})

        assert (
            missing["status"]["message"]["parts"][0]["text"]
            == wrong["status"]["message"]["parts"][0]["text"]
        )


def test_re_attaching_revokes_the_previous_token(tmp_path: pathlib.Path):
    """The only way to rotate a token that leaked."""
    app = create_app(CONFIG, tmp_path / "minds.db")
    with TestClient(app) as client:
        world = app.state.simulation.world
        entity = next(iter(world.query(Agent)))
        world.get(entity, Agent).user_deployed = True

        first = client.post(f"/agents/{entity}/mind").json()["token"]
        second = client.post(f"/agents/{entity}/mind").json()["token"]
        assert first != second

        stale = _send(client, {"skill": "steer-my-agent", "agent_id": entity, "token": first})
        assert stale["status"]["state"] == "failed"


def test_the_card_advertises_the_mind_skills_only_when_enabled(tmp_path: pathlib.Path):
    app = create_app(CONFIG, tmp_path / "on.db")
    with TestClient(app) as client:
        on = [s["id"] for s in client.get("/.well-known/agent-card.json").json()["skills"]]

    app = create_app(replace(CONFIG, attached_minds_enabled=False), tmp_path / "off.db")
    with TestClient(app) as client:
        off = [s["id"] for s in client.get("/.well-known/agent-card.json").json()["skills"]]

    assert "steer-my-agent" in on
    assert "steer-my-agent" not in off


def test_attaching_is_refused_when_the_feature_is_off(tmp_path: pathlib.Path):
    app = create_app(replace(CONFIG, attached_minds_enabled=False), tmp_path / "off.db")
    with TestClient(app) as client:
        world = app.state.simulation.world
        entity = next(iter(world.query(Agent)))
        world.get(entity, Agent).user_deployed = True
        assert client.post(f"/agents/{entity}/mind").status_code == 403


def test_status_reports_what_is_attached():
    world, _ = _world_with_mind()
    status = minds.status(world)
    assert status["enabled"] is True
    assert status["attached"] == 1
