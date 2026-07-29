"""carry_capacity is a real per-resource cap, enforced on pickup."""

from __future__ import annotations

from src.world.components import (
    Agent,
    AgentState,
    Inventory,
    Needs,
    Position,
    ResourceKind,
    ResourceNode,
)
from src.world.config import WorldConfig
from src.world.ecs import World
from src.world.rng import TickRng
from src.world.systems import fsm as fsm_system
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(seed=17, grid_width=32, grid_height=32, agent_count=30, resource_count=110)


def _agent_on_a_node(kind: ResourceKind, held: int, node_amount: int = 10):
    world = World(CONFIG)

    node = world.create_entity()
    world.add(node, Position(4, 4))
    world.add(node, ResourceNode(kind=kind, amount=node_amount, max_amount=node_amount, regrow_every=50))

    entity = world.create_entity()
    world.add(entity, Position(4, 4))
    world.add(entity, Needs(energy=90, hunger=90, social=100, safety=100))
    inventory = Inventory(
        food=held if kind is ResourceKind.FOOD else 0,
        wood=held if kind is ResourceKind.WOOD else 0,
    )
    world.add(entity, inventory)
    world.add(entity, Agent(name="t", state=AgentState.GATHER, target_entity=node, wants=kind))
    return world, entity, node


def _tick_fsm(world: World) -> None:
    world.tick += 1
    fsm_system.run(world, TickRng(CONFIG.seed, world.tick, "fsm"))


def test_pickup_refused_at_capacity_for_food():
    world, entity, node = _agent_on_a_node(ResourceKind.FOOD, held=CONFIG.carry_capacity)
    before = world.get(node, ResourceNode).amount

    for _ in range(CONFIG.gather_ticks + 2):
        _tick_fsm(world)

    assert world.get(entity, Inventory).food == CONFIG.carry_capacity
    assert world.get(node, ResourceNode).amount == before, "node was mined despite refusal"


def test_pickup_refused_at_capacity_for_wood():
    world, entity, node = _agent_on_a_node(ResourceKind.WOOD, held=CONFIG.carry_capacity)
    before = world.get(node, ResourceNode).amount

    for _ in range(CONFIG.gather_ticks + 2):
        _tick_fsm(world)

    assert world.get(entity, Inventory).wood == CONFIG.carry_capacity
    assert world.get(node, ResourceNode).amount == before


def test_refusal_leaves_the_gather_state():
    world, entity, _ = _agent_on_a_node(ResourceKind.FOOD, held=CONFIG.carry_capacity)
    _tick_fsm(world)
    assert world.get(entity, Agent).state is not AgentState.GATHER


def test_agent_below_capacity_still_gathers():
    world, entity, node = _agent_on_a_node(ResourceKind.FOOD, held=0)
    for _ in range(CONFIG.gather_ticks):
        _tick_fsm(world)
    assert world.get(entity, Inventory).food == 1
    assert world.get(node, ResourceNode).amount == 9


def test_gathering_stops_exactly_at_capacity():
    world, entity, _ = _agent_on_a_node(ResourceKind.FOOD, held=CONFIG.carry_capacity - 1)
    for _ in range(CONFIG.gather_ticks * 4):
        _tick_fsm(world)
    assert world.get(entity, Inventory).food == CONFIG.carry_capacity


def test_inventories_stay_bounded_across_a_long_run():
    simulation = Simulation(create_world(CONFIG))
    simulation.run(8000)
    world = simulation.world

    for entity in world.query(Agent, Inventory):
        inventory = world.get(entity, Inventory)
        assert inventory.food <= CONFIG.carry_capacity, f"food {inventory.food}"
        assert inventory.wood <= CONFIG.carry_capacity, f"wood {inventory.wood}"


def test_no_agent_stockpiles_beyond_capacity_after_building_out():
    """The old failure: hut-capped agents foraged food forever and hoarded hundreds."""
    simulation = Simulation(create_world(CONFIG))
    simulation.run(8000)
    world = simulation.world

    capped = [
        entity
        for entity in world.query(Agent)
        if world.get(entity, Agent).huts_owned >= CONFIG.max_huts_per_agent
    ]
    assert capped, "no agent reached its hut allowance — test is not exercising the case"
    for entity in capped:
        assert world.get(entity, Inventory).food <= CONFIG.carry_capacity
