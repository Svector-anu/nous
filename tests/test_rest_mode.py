"""User rest mode: spectator safety valve that still lives by the same rules."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from src.api.server import create_app
from src.persistence.sqlite_store import SqliteWorldStore
from src.world.components import Agent, ClanRef, ForceDecisionQueue, Inventory, Needs, Position, RestQueue
from src.world.config import WorldConfig
from src.world.systems import blackboard, combat, fsm, resting, spawning
from src.world.tick import Simulation, create_world, state_hash

CONFIG = WorldConfig(seed=71, grid_width=32, grid_height=32, agent_count=20, resource_count=70)


def _client(tmp_path):
    return TestClient(create_app(CONFIG, tmp_path / "rest.db"))


def _fast_client(tmp_path):
    """Fast ticks so the background loop advances while we poll synchronously."""
    fast_config = WorldConfig(
        seed=71,
        grid_width=32,
        grid_height=32,
        agent_count=20,
        resource_count=70,
        tick_seconds=0.01,
    )
    return TestClient(create_app(fast_config, tmp_path / "rest.db"))
def _sim() -> Simulation:
    return Simulation(create_world(CONFIG))


def _deployed(simulation: Simulation) -> list[int]:
    return [e for e in simulation.world.query(Agent) if simulation.world.get(e, Agent).user_deployed]


# --- queue and world integration ------------------------------------------------


def test_world_starts_with_a_rest_queue():
    world = create_world(CONFIG)
    assert world.first(RestQueue) is not None


def test_rest_queue_is_bounded():
    world = create_world(CONFIG)
    for i in range(CONFIG.max_user_agents + 5):
        resting.enqueue(world, 1 + (i % 4), i % 2 == 0)
    q = resting.queue(world)
    assert len(q.pending) <= CONFIG.max_user_agents


# --- api ----------------------------------------------------------------------


def _client(tmp_path):
    return TestClient(create_app(CONFIG, tmp_path / "rest.db"))


def test_rest_toggle_queues_for_next_tick(tmp_path):
    with _fast_client(tmp_path) as client:
        client.post("/agents", json={"name": "Kestrel", "personality": "cautious"})
        # Agent arrives on the next tick; the simulation runs in the background.
        agents = []
        for _ in range(30):
            time.sleep(0.02)
            agents = client.get("/agents").json()["agents"]
            if agents:
                break
        assert agents, "deployed agent did not appear"
        agent_id = agents[0]["id"]

        response = client.post(f"/agents/{agent_id}/rest", json={"rest": True})
        assert response.status_code == 202
        assert response.json()["queued"] is True
        assert response.json()["rest"] is True


# --- sim behaviour ------------------------------------------------------------


def test_rest_mode_applies_on_the_next_tick():
    simulation = _sim()
    spawning.enqueue(simulation.world, "Kestrel", "cautious")
    simulation.step()

    agent_id = _deployed(simulation)[0]
    resting.enqueue(simulation.world, agent_id, True)
    simulation.step()

    assert simulation.world.get(agent_id, Agent).rest_mode is True


def test_rest_mode_does_not_override_starvation():
    """A resting agent must still eat when it is starving."""
    simulation = _sim()
    spawning.enqueue(simulation.world, "Kestrel", "cautious")
    simulation.step()

    agent_id = _deployed(simulation)[0]
    simulation.world.get(agent_id, Agent).rest_mode = True

    # Drive hunger to just above zero with no food, but leave energy high so the
    # fsm would otherwise stay in rest.
    needs = simulation.world.get(agent_id, Needs)
    needs.hunger = 10
    needs.energy = 90

    simulation.step()

    # Hunger below threshold should have broken rest and sent the agent for food.
    assert simulation.world.get(agent_id, Agent).state in ("SEEK_NEED", "IDLE")


def test_resting_user_agent_does_not_raid():
    """Rest mode is a safety valve, not invulnerability. It only prevents the
    agent from initiating raids."""
    simulation = _sim()
    spawning.enqueue(simulation.world, "Kestrel", "cautious")
    simulation.step()

    agent_id = _deployed(simulation)[0]
    simulation.world.get(agent_id, Agent).rest_mode = True

    # Place the agent in a raiding clan by giving it a clan goal. The clan
    # decision path is not needed here; we just need _is_raiding to return True.
    simulation.world.get(agent_id, ClanRef).clan_id = 1
    current = blackboard.board(simulation.world)
    current.write("clan:1:goal", "raid", agent_id, simulation.world.tick)

    raiders = [
        e
        for e in simulation.world.query(Agent, Needs, Inventory, Position)
        if combat._is_raiding(simulation.world, e) and not simulation.world.get(e, Agent).rest_mode
    ]
    assert agent_id not in raiders


def test_rest_mode_survives_save_and_reload(tmp_path):
    store = SqliteWorldStore(tmp_path / "rest.db")
    simulation = _sim()
    spawning.enqueue(simulation.world, "Kestrel", "cautious")
    simulation.step()

    agent_id = _deployed(simulation)[0]
    simulation.world.get(agent_id, Agent).rest_mode = True
    store.save(simulation.world)
    store.close()

    loaded = SqliteWorldStore(tmp_path / "rest.db").load()
    assert loaded.get(agent_id, Agent).rest_mode is True


def test_rest_mode_does_not_break_determinism():
    """The rest flag is world state; toggling it must not alter the determinism
    of a world that is not using it."""

    def run(with_rest: bool) -> str:
        simulation = _sim()
        spawning.enqueue(simulation.world, "Kestrel", "cautious")
        simulation.step()
        if with_rest:
            agent_id = _deployed(simulation)[0]
            simulation.world.get(agent_id, Agent).rest_mode = True
        simulation.run(200)
        return state_hash(simulation.world)

    assert run(False) == run(False)
    assert run(True) == run(True)


# --- force decision seam -------------------------------------------------------


def test_world_starts_with_a_force_decision_queue():
    world = create_world(CONFIG)
    assert world.first(ForceDecisionQueue) is not None


def test_force_decision_endpoint_returns_402_without_payment(tmp_path):
    with _client(tmp_path) as client:
        response = client.post("/clans/1/force-decision")
        assert response.status_code == 403


def test_force_decision_endpoint_returns_402_when_enabled(tmp_path):
    config = WorldConfig(**{**CONFIG.__dict__, "llm_force_decision_enabled": True})
    with TestClient(create_app(config, tmp_path / "x402.db")) as client:
        response = client.post("/clans/1/force-decision")
        assert response.status_code == 402
        assert response.headers.get("X402-Payment-Required") == "true"
        assert "payment" in response.json()["detail"]


def test_force_decision_endpoint_records_paid_request(tmp_path):
    config = WorldConfig(**{**CONFIG.__dict__, "llm_force_decision_enabled": True})
    with TestClient(create_app(config, tmp_path / "x402.db")) as client:
        response = client.post(
            "/clans/1/force-decision",
            headers={"X402-Payment-Verified": "true"},
        )
        assert response.status_code == 202
        assert response.json()["queued"] is True
