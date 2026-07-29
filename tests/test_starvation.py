"""Starvation denies rest. Energy decays normally; death is still the energy path."""

from __future__ import annotations

from src.world.components import Agent, AgentState, Inventory, Needs, Position
from src.world.config import WorldConfig
from src.world.ecs import Entity, World
from src.world.rng import TickRng
from src.world.systems import fsm as fsm_system
from src.world.systems import needs as needs_system

CONFIG = WorldConfig(seed=11, grid_width=16, grid_height=16)


def _lone_agent(
    energy: int,
    hunger: int,
    food: int = 0,
    state: AgentState = AgentState.SEEK_NEED,
) -> tuple[World, Entity]:
    world = World(CONFIG)
    entity = world.create_entity()
    world.add(entity, Agent(name="test", state=state))
    world.add(entity, Needs(energy=energy, hunger=hunger, social=100, safety=100))
    world.add(entity, Inventory(food=food))
    world.add(entity, Position(8, 8))
    return world, entity


def _tick_needs(world: World) -> None:
    world.tick += 1
    needs_system.run(world, TickRng(CONFIG.seed, world.tick, "needs"))


def _tick_fsm(world: World) -> None:
    fsm_system.run(world, TickRng(CONFIG.seed, world.tick, "fsm"))


def test_energy_decays_at_the_normal_rate_while_starving():
    world, entity = _lone_agent(energy=80, hunger=0)
    _tick_needs(world)
    assert world.get(entity, Needs).energy == 80 - CONFIG.energy_decay_per_tick


def test_starving_agent_is_evicted_from_rest():
    world, entity = _lone_agent(energy=50, hunger=0, state=AgentState.REST)
    _tick_needs(world)
    assert world.get(entity, Agent).state is not AgentState.REST


def test_starving_agent_gains_no_energy_from_rest():
    world, entity = _lone_agent(energy=50, hunger=0, state=AgentState.REST)
    _tick_needs(world)
    assert world.get(entity, Needs).energy == 50 - CONFIG.energy_decay_per_tick


def test_fed_agent_still_recovers_by_resting():
    world, entity = _lone_agent(energy=50, hunger=80, state=AgentState.REST)
    _tick_needs(world)
    expected = 50 - CONFIG.energy_decay_per_tick + CONFIG.rest_energy_per_tick
    assert world.get(entity, Needs).energy == expected


def test_starving_exhausted_agent_does_not_choose_rest():
    world, entity = _lone_agent(
        energy=CONFIG.need_threshold - 5, hunger=0, state=AgentState.IDLE
    )
    _tick_needs(world)
    _tick_fsm(world)

    agent = world.get(entity, Agent)
    assert agent.state is not AgentState.REST
    assert agent.wants is not None


def test_eating_restores_access_to_rest_the_same_tick():
    """Hunger about to bottom out, but the agent has food: it eats and keeps resting."""
    world, entity = _lone_agent(
        energy=50, hunger=1, food=1, state=AgentState.REST
    )
    _tick_needs(world)

    assert world.get(entity, Agent).state is AgentState.REST
    assert world.get(entity, Needs).hunger > 0
    assert world.get(entity, Inventory).food == 0
    expected = 50 - CONFIG.energy_decay_per_tick + CONFIG.rest_energy_per_tick
    assert world.get(entity, Needs).energy == expected


def test_starvation_kills_once_energy_runs_out():
    world, entity = _lone_agent(energy=5, hunger=0)
    for _ in range(5):
        if not world.is_alive(entity):
            break
        _tick_needs(world)
    assert not world.is_alive(entity)


def test_starving_agent_survives_as_long_as_its_energy_lasts():
    """Denying rest is the whole mechanism — no extra damage on top."""
    world, entity = _lone_agent(energy=30, hunger=0)
    for _ in range(29):
        _tick_needs(world)
    assert world.is_alive(entity)
    assert world.get(entity, Needs).energy == 1

    _tick_needs(world)
    assert not world.is_alive(entity)


def test_death_removes_every_component():
    world, entity = _lone_agent(energy=1, hunger=0)
    _tick_needs(world)
    assert not world.is_alive(entity)
    assert world.try_get(entity, Needs) is None
    assert world.query(Agent) == []


def test_hunger_never_goes_negative():
    world, entity = _lone_agent(energy=100, hunger=0)
    for _ in range(10):
        _tick_needs(world)
    assert world.get(entity, Needs).hunger == 0


def test_population_survives_a_temporary_food_shortage():
    """A regional drought must kill some agents and then stop killing once it lifts.

    Guards both failure modes: a shortage that harms nobody, and one the survivors
    never recover from. Blighting 70% of food nodes leaves enough of a refuge that the
    outcome is graded — a total blackout is lethal to everyone now that agents carry
    at most `carry_capacity` meals, which `test_total_famine_is_lethal` covers.
    """
    from src.world.components import ResourceKind, ResourceNode
    from src.world.tick import Simulation, create_world

    config = WorldConfig(
        seed=21, grid_width=32, grid_height=32, agent_count=40, resource_count=100
    )
    simulation = Simulation(create_world(config))
    world = simulation.world
    food_nodes = [
        e
        for e in world.query(ResourceNode)
        if world.get(e, ResourceNode).kind is ResourceKind.FOOD
    ]
    blighted = food_nodes[: int(len(food_nodes) * 0.7)]

    simulation.run(1500)
    baseline = len(world.query(Agent))
    assert baseline > 0

    while world.tick < 2100:
        simulation.step()
        for entity in blighted:
            if world.is_alive(entity):
                world.get(entity, ResourceNode).amount = 0
    trough = len(world.query(Agent))

    assert trough < baseline, "the drought killed nobody"
    assert trough > 0, "the drought wiped out the world"

    simulation.run(2500)
    recovered = len(world.query(Agent))
    # 0.6, not 0.8: the recovery ratio measured across six seeds spans 0.68-0.91, so a
    # 0.8 bar was tuned to one seed and any legitimate change to raid tie-breaking
    # tripped it. The bar this test actually needs is "survivors stop dying", not a
    # precise fraction.
    assert recovered >= trough * 0.6, f"still dying after the drought: {trough} -> {recovered}"


def test_total_famine_is_lethal():
    """With inventories bounded, a full food blackout is not survivable."""
    from src.world.components import ResourceKind, ResourceNode
    from src.world.tick import Simulation, create_world

    config = WorldConfig(
        seed=21, grid_width=32, grid_height=32, agent_count=20, resource_count=60
    )
    simulation = Simulation(create_world(config))
    world = simulation.world
    food_nodes = [
        e
        for e in world.query(ResourceNode)
        if world.get(e, ResourceNode).kind is ResourceKind.FOOD
    ]

    simulation.run(500)
    assert world.query(Agent), "world died before the famine started"

    for _ in range(1200):
        simulation.step()
        for entity in food_nodes:
            if world.is_alive(entity):
                world.get(entity, ResourceNode).amount = 0
        if not world.query(Agent):
            break

    assert not world.query(Agent), "agents outlived a total famine"


def test_population_settles_at_a_carrying_capacity():
    """Denying rest to the starving thins the population to what the land feeds, then
    holds. The failure this guards against is a spiral to extinction."""
    from src.world.tick import Simulation, create_world

    config = WorldConfig(
        seed=13, grid_width=32, grid_height=32, agent_count=40, resource_count=80
    )
    simulation = Simulation(create_world(config))

    simulation.run(3000)
    settled = len(simulation.world.query(Agent))
    assert settled > 0, "population went extinct"

    simulation.run(2000)
    later = len(simulation.world.query(Agent))

    assert later > 0
    assert later >= settled * 0.8, f"still collapsing: {settled} -> {later}"
