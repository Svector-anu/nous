"""Agents must actually do things: move, gather, and build."""

from __future__ import annotations

from src.world.components import Agent, AgentState, Building, Inventory, Position
from src.world.config import WorldConfig
from src.world.tick import Simulation, build_registry, create_world

TEST_CONFIG = WorldConfig(seed=4242, grid_width=32, grid_height=32, agent_count=40, resource_count=90)


def _simulate(ticks: int) -> Simulation:
    simulation = Simulation(create_world(TEST_CONFIG))
    simulation.run(ticks)
    return simulation


def test_systems_run_in_the_documented_order():
    assert build_registry().names() == [
        "spawning",
        "messaging",
        "needs",
        "trade",
        "resting",
        "combat",
        "fsm",
        "movement",
        "build",
        "regrowth",
        "leadership",
        "social",
        "standing",
        "blackboard",
        # Last on purpose: a market resolves against the tick's final state, so it must
        # run after every system that can change the world. It only ever reads.
        "markets",
    ]


def test_world_starts_populated():
    world = create_world(TEST_CONFIG)
    assert len(world.query(Agent)) == TEST_CONFIG.agent_count
    assert world.tick == 0


def test_agents_move():
    world = create_world(TEST_CONFIG)
    start = {e: (world.get(e, Position).x, world.get(e, Position).y) for e in world.query(Agent)}

    simulation = Simulation(world)
    simulation.run(40)

    moved = sum(
        1
        for entity in world.query(Agent, Position)
        if (world.get(entity, Position).x, world.get(entity, Position).y) != start[entity]
    )
    assert moved > 0


def test_agents_gather_resources():
    world = _simulate(60).world
    carried = sum(
        world.get(entity, Inventory).total() for entity in world.query(Agent, Inventory)
    )
    assert carried > 0


def test_agents_build_huts():
    world = _simulate(400).world
    assert len(world.query(Building)) > 0


def test_most_agents_survive_the_early_world():
    """Resting keeps fed agents alive. A few starve where food is sparse, but the
    early world must not thin out sharply."""
    world = _simulate(400).world
    assert len(world.query(Agent)) >= int(TEST_CONFIG.agent_count * 0.8)


def test_every_agent_holds_a_valid_state():
    world = _simulate(200).world
    states = {world.get(entity, Agent).state for entity in world.query(Agent)}
    assert states.issubset(set(AgentState))


def test_snapshot_shape():
    snapshot = _simulate(10).snapshot()
    assert snapshot["tick"] == 10
    assert snapshot["grid"] == {"width": 32, "height": 32}
    assert set(snapshot["agents"][0]) == {
        "id", "name", "x", "y", "state", "energy", "hunger", "food", "wood", "clan",
        "user", "personality", "wants", "huts", "raids_won", "raids_lost", "received",
        "target", "standing", "rank", "rest_mode",
    }
    assert set(snapshot["resources"][0]) == {"id", "x", "y", "kind", "amount", "max"}
