"""Nodes go dormant instead of dying, and the world sustains itself indefinitely."""

from __future__ import annotations

from src.world.components import Agent, ResourceKind, ResourceNode
from src.world.config import WorldConfig
from src.world.ecs import World
from src.world.systems import regrowth
from src.world.tick import Simulation, create_world

TEST_CONFIG = WorldConfig(seed=77, grid_width=32, grid_height=32, agent_count=30, resource_count=70)


def _node_world(amount: int, max_amount: int, regrow_every: int) -> tuple[World, int]:
    world = World(TEST_CONFIG)
    entity = world.create_entity()
    world.add(
        entity,
        ResourceNode(
            kind=ResourceKind.FOOD,
            amount=amount,
            max_amount=max_amount,
            regrow_every=regrow_every,
        ),
    )
    return world, entity


def _advance(world: World, ticks: int) -> None:
    for _ in range(ticks):
        world.tick += 1
        regrowth.run(world, None)  # type: ignore[arg-type]


def test_dormant_node_recovers_one_unit_per_interval():
    world, entity = _node_world(amount=0, max_amount=5, regrow_every=10)
    _advance(world, 10)
    assert world.get(entity, ResourceNode).amount == 1
    _advance(world, 10)
    assert world.get(entity, ResourceNode).amount == 2


def test_regrowth_stops_at_max_amount():
    world, entity = _node_world(amount=4, max_amount=5, regrow_every=1)
    _advance(world, 50)
    assert world.get(entity, ResourceNode).amount == 5


def test_genesis_nodes_start_full_and_have_a_regrow_interval():
    world = create_world(TEST_CONFIG)
    for entity in world.query(ResourceNode):
        node = world.get(entity, ResourceNode)
        assert node.amount == node.max_amount
        assert node.regrow_every >= 1
        assert TEST_CONFIG.node_min_amount <= node.max_amount <= TEST_CONFIG.node_max_amount


def test_nodes_are_never_destroyed():
    world = create_world(TEST_CONFIG)
    before = len(world.query(ResourceNode))

    simulation = Simulation(world)
    simulation.run(1500)

    assert len(world.query(ResourceNode)) == before


def test_world_does_not_forage_itself_into_a_dead_stop():
    simulation = Simulation(create_world(TEST_CONFIG))
    simulation.run(3000)

    active = [
        entity
        for entity in simulation.world.query(ResourceNode)
        if not simulation.world.get(entity, ResourceNode).is_dormant
    ]
    assert active, "every node was dormant — the economy stalled"
    assert len(simulation.world.query(Agent)) >= TEST_CONFIG.agent_count // 2
