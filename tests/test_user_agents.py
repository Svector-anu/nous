"""User-deployed agents: spawning, determinism, and behaving like everyone else."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.server import create_app
from src.persistence.sqlite_store import SqliteWorldStore
from src.world.components import (
    Agent,
    ClanRef,
    Inbox,
    Inventory,
    Needs,
    Outbox,
    Position,
    SpawnQueue,
)
from src.world.config import WorldConfig
from src.world.tick import Simulation, create_world, state_hash

CONFIG = WorldConfig(seed=71, grid_width=32, grid_height=32, agent_count=20, resource_count=70)


def _sim() -> Simulation:
    return Simulation(create_world(CONFIG))


def _user_agents(world) -> list[int]:
    return [e for e in world.query(Agent) if world.get(e, Agent).user_deployed]


# --- spawning ---------------------------------------------------------------


def test_world_starts_with_an_empty_spawn_queue():
    world = create_world(CONFIG)
    assert world.first(SpawnQueue) is not None
    assert _user_agents(world) == []


def test_deployed_agent_enters_on_the_next_tick():
    from src.world.systems import spawning

    simulation = _sim()
    spawning.enqueue(simulation.world, "Kestrel", "cautious forager")

    assert _user_agents(simulation.world) == []
    simulation.step()

    deployed = _user_agents(simulation.world)
    assert len(deployed) == 1
    agent = simulation.world.get(deployed[0], Agent)
    assert agent.name == "Kestrel"
    assert agent.personality == "cautious forager"
    assert agent.user_deployed is True


def test_queue_is_drained_after_spawning():
    from src.world.systems import spawning

    simulation = _sim()
    spawning.enqueue(simulation.world, "One", "")
    simulation.step()
    simulation.step()

    assert len(_user_agents(simulation.world)) == 1, "spawned twice"
    assert spawning.queue(simulation.world).pending == []


def test_deployed_agent_has_the_full_component_set():
    from src.world.systems import spawning

    simulation = _sim()
    spawning.enqueue(simulation.world, "Complete", "note")
    simulation.step()
    entity = _user_agents(simulation.world)[0]

    for component in (Position, Needs, Inventory, ClanRef, Inbox, Outbox, Agent):
        assert simulation.world.has(entity, component), f"missing {component.__name__}"


def test_deployed_agent_spawns_inside_the_grid():
    from src.world.systems import spawning

    simulation = _sim()
    for index in range(8):
        spawning.enqueue(simulation.world, f"a{index}", "")
    simulation.step()

    for entity in _user_agents(simulation.world):
        position = simulation.world.get(entity, Position)
        assert 0 <= position.x < CONFIG.grid_width
        assert 0 <= position.y < CONFIG.grid_height


def test_several_deployments_in_one_tick_all_arrive():
    from src.world.systems import spawning

    simulation = _sim()
    for name in ("A", "B", "C"):
        spawning.enqueue(simulation.world, name, "")
    simulation.step()

    names = {simulation.world.get(e, Agent).name for e in _user_agents(simulation.world)}
    assert names == {"A", "B", "C"}


# --- determinism ------------------------------------------------------------


def test_same_deployments_at_the_same_ticks_reproduce_the_world():
    from src.world.systems import spawning

    def run() -> str:
        simulation = _sim()
        simulation.run(40)
        spawning.enqueue(simulation.world, "Twin", "identical twin")
        simulation.run(60)
        return state_hash(simulation.world)

    assert run() == run()


def test_deployment_tick_matters():
    from src.world.systems import spawning

    def run(at: int) -> str:
        simulation = _sim()
        simulation.run(at)
        spawning.enqueue(simulation.world, "Shift", "")
        simulation.run(100 - at)
        return state_hash(simulation.world)

    assert run(20) != run(40)


def test_pending_deployment_survives_save_and_reload(tmp_path):
    from src.world.systems import spawning

    store = SqliteWorldStore(tmp_path / "spawn.db")
    simulation = Simulation(create_world(CONFIG), store=store)
    simulation.run(30)
    spawning.enqueue(simulation.world, "Persisted", "waited through a restart")
    store.save(simulation.world)
    store.close()

    reopened = SqliteWorldStore(tmp_path / "spawn.db")
    resumed = Simulation(reopened.load())
    reopened.close()

    assert len(spawning.queue(resumed.world).pending) == 1
    resumed.step()
    deployed = _user_agents(resumed.world)
    assert len(deployed) == 1
    assert resumed.world.get(deployed[0], Agent).personality == "waited through a restart"


def test_personality_survives_save_and_reload(tmp_path):
    from src.world.systems import spawning

    store = SqliteWorldStore(tmp_path / "card.db")
    simulation = Simulation(create_world(CONFIG), store=store)
    spawning.enqueue(simulation.world, "Remembered", "keeps its note")
    simulation.run(20)
    store.save(simulation.world)
    store.close()

    loaded = SqliteWorldStore(tmp_path / "card.db").load()
    agent = loaded.get(_user_agents(loaded)[0], Agent)
    assert agent.name == "Remembered"
    assert agent.personality == "keeps its note"
    assert agent.user_deployed is True


# --- behaves like everyone else ---------------------------------------------


def test_deployed_agents_live_by_the_same_rules():
    from src.world.systems import spawning

    simulation = _sim()
    for index in range(5):
        spawning.enqueue(simulation.world, f"user{index}", "ordinary")
    simulation.run(1200)
    world = simulation.world

    deployed = _user_agents(world)
    assert deployed, "every user agent died — they should live like the rest"
    for entity in deployed:
        needs = world.get(entity, Needs)
        inventory = world.get(entity, Inventory)
        assert 0 < needs.energy <= CONFIG.need_max
        assert inventory.food <= CONFIG.carry_capacity


def test_deployed_agents_can_join_clans():
    from src.world.systems import spawning

    simulation = Simulation(create_world(WorldConfig()))
    for index in range(10):
        spawning.enqueue(simulation.world, f"joiner{index}", "sociable")
    simulation.run(1500)
    world = simulation.world

    joined = [
        e for e in _user_agents(world) if world.get(e, ClanRef).clan_id is not None
    ]
    assert joined, "no deployed agent ever joined a clan"


# --- market integration -----------------------------------------------------


def test_deployed_agent_gets_a_survival_market():
    from src.world.systems import markets, spawning

    simulation = _sim()
    simulation.run(20)
    spawning.enqueue(simulation.world, "Trader", "")
    simulation.step()

    deployed = _user_agents(simulation.world)
    assert len(deployed) == 1
    entity = deployed[0]

    book = markets.book(simulation.world)
    agent_markets = [
        m for m in book.markets
        if m["kind"] == "agent_survives" and m["subject"].get("agent") == entity
    ]
    assert len(agent_markets) == 1
    m = agent_markets[0]
    assert m["subject"]["name"] == "Trader"
    assert m["trigger"] == "agent_spawned"
    assert m["state"] == markets.OPEN


def test_non_user_agents_do_not_get_a_spawn_market():
    from src.world.systems import markets

    simulation = _sim()
    simulation.run(20)

    book = markets.book(simulation.world)
    spawn_markets = [m for m in book.markets if m["trigger"] == "agent_spawned"]
    assert spawn_markets == []


# --- api --------------------------------------------------------------------


def _client(tmp_path):
    return TestClient(create_app(CONFIG, tmp_path / "api.db"))


def test_post_agents_queues_a_deployment(tmp_path):
    with _client(tmp_path) as client:
        response = client.post("/agents", json={"name": "Vale", "personality": "quiet"})
        assert response.status_code == 202
        assert response.json()["queued"] is True

        listed = client.get("/agents").json()
        assert listed["pending"][0]["name"] == "Vale"


def test_empty_name_is_rejected(tmp_path):
    with _client(tmp_path) as client:
        assert client.post("/agents", json={"name": "   "}).status_code == 422


def test_overlong_name_is_rejected(tmp_path):
    with _client(tmp_path) as client:
        response = client.post("/agents", json={"name": "x" * 100})
        assert response.status_code == 422


def test_overlong_personality_is_rejected(tmp_path):
    with _client(tmp_path) as client:
        response = client.post("/agents", json={"name": "ok", "personality": "y" * 500})
        assert response.status_code == 422


def test_personality_is_optional(tmp_path):
    with _client(tmp_path) as client:
        assert client.post("/agents", json={"name": "Solo"}).status_code == 202


def test_spawn_queue_is_bounded(tmp_path):
    with _client(tmp_path) as client:
        codes = [
            client.post("/agents", json={"name": f"n{i}"}).status_code
            for i in range(CONFIG.max_pending_spawns + 5)
        ]
        assert 429 in codes or 409 in codes, "queue accepted unbounded deployments"


def test_agents_endpoint_reports_the_limit(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/agents").json()["limit"] == CONFIG.max_user_agents
