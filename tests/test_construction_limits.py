"""Construction is capped per agent, with a global stop when the map gets dense."""

from __future__ import annotations

from src.world.components import Agent, AgentState, Building, Inventory, Position
from src.world.config import WorldConfig
from src.world.tick import Simulation, create_world

TEST_CONFIG = WorldConfig(seed=31, grid_width=32, grid_height=32, agent_count=30, resource_count=120)


def _owned(world, entity) -> int:
    return sum(
        1
        for building in world.query(Building)
        if world.get(building, Building).owner == entity
    )


def test_no_agent_exceeds_its_hut_allowance():
    simulation = Simulation(create_world(TEST_CONFIG))
    simulation.run(6000)
    world = simulation.world

    for entity in world.query(Agent):
        assert world.get(entity, Agent).huts_owned <= TEST_CONFIG.max_huts_per_agent


def test_counter_matches_actual_owned_buildings():
    simulation = Simulation(create_world(TEST_CONFIG))
    simulation.run(6000)
    world = simulation.world

    for entity in world.query(Agent):
        assert world.get(entity, Agent).huts_owned == _owned(world, entity)


def test_total_huts_bounded_by_the_per_agent_cap():
    simulation = Simulation(create_world(TEST_CONFIG))
    simulation.run(6000)

    ceiling = TEST_CONFIG.agent_count * TEST_CONFIG.max_huts_per_agent
    assert len(simulation.world.query(Building)) <= ceiling


def test_capped_agent_stops_choosing_build():
    world = create_world(TEST_CONFIG)
    entity = world.query(Agent)[0]
    agent = world.get(entity, Agent)
    agent.huts_owned = TEST_CONFIG.max_huts_per_agent
    agent.state = AgentState.IDLE
    world.get(entity, Inventory).wood = TEST_CONFIG.wood_per_hut * 3

    Simulation(world).run(1)

    assert world.get(entity, Agent).state is not AgentState.BUILD


def test_global_stop_holds_when_the_map_is_dense():
    """A grid small enough that 3 huts per agent would otherwise overrun it."""
    dense = WorldConfig(
        seed=31, grid_width=8, grid_height=8, agent_count=40, resource_count=40
    )
    simulation = Simulation(create_world(dense))
    simulation.run(4000)

    ceiling = dense.grid_width * dense.grid_height * dense.global_build_stop_fraction
    assert len(simulation.world.query(Building)) <= ceiling


def test_agents_still_build_when_under_both_limits():
    simulation = Simulation(create_world(TEST_CONFIG))
    simulation.run(2000)
    assert len(simulation.world.query(Building)) > 0


def test_hut_owner_is_recorded():
    simulation = Simulation(create_world(TEST_CONFIG))
    simulation.run(2000)
    world = simulation.world

    huts = world.query(Building)
    assert huts
    for hut in huts:
        owner = world.get(hut, Building).owner
        assert owner is not None and world.has(owner, Agent)


def test_capped_agents_keep_cycling_rather_than_stalling():
    """Once built out and fully stocked an agent rests in place, so movement is the
    wrong liveness signal — it must still cycle through states."""
    simulation = Simulation(create_world(TEST_CONFIG))
    simulation.run(6000)
    world = simulation.world

    agents = world.query(Agent)
    seen: dict[int, set[str]] = {entity: set() for entity in agents}
    for _ in range(120):
        simulation.step()
        for entity in agents:
            if world.is_alive(entity):
                seen[entity].add(world.get(entity, Agent).state.value)

    alive = [entity for entity in agents if world.is_alive(entity)]
    assert alive
    stuck = [entity for entity in alive if len(seen[entity]) == 1]
    assert not stuck, f"{len(stuck)} agents never left a single state in 120 ticks"
