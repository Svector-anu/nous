"""Agent state machine and resource harvesting.

Implements the Phase 0 diagram: IDLE -> SEEK_NEED -> GATHER -> BUILD, with REST
and FLEE as side branches. FLEE has no trigger yet because Phase 0 has no
hostiles; the state exists so combat in Phase 1 has somewhere to transition to.
"""

from __future__ import annotations

from ..components import (
    Agent,
    AgentState,
    Building,
    Inventory,
    Needs,
    Position,
    ResourceKind,
    ResourceNode,
)
from ..config import WorldConfig
from ..ecs import Entity, World
from ..rng import TickRng
from .needs import is_starving

_RESTED_FRACTION = 0.9


def _may_build(agent: Agent, config: WorldConfig, world_has_room: bool) -> bool:
    """An agent stops choosing BUILD once it owns its allowance of huts, or once the
    map is too built-up to be worth hunting for a free tile."""
    return world_has_room and agent.huts_owned < config.max_huts_per_agent


def _nearest_resource(
    world: World,
    origin: Position,
    kind: ResourceKind,
    radius: int,
) -> Entity | None:
    best_entity: Entity | None = None
    best_distance = radius * radius + 1

    for entity in world.query(ResourceNode, Position):
        node = world.get(entity, ResourceNode)
        if node.kind is not kind or node.is_dormant:
            continue
        position = world.get(entity, Position)
        dx = position.x - origin.x
        dy = position.y - origin.y
        distance = dx * dx + dy * dy
        if distance < best_distance:
            best_distance = distance
            best_entity = entity

    return best_entity


def _seek(world: World, entity: Entity, agent: Agent, rng: TickRng) -> None:
    config = world.config
    position = world.get(entity, Position)

    target = agent.target_entity
    if target is not None and world.get(target, ResourceNode).is_dormant:
        agent.clear_target()
        target = None

    if target is None:
        target = _nearest_resource(world, position, agent.wants, config.vision_radius)

        # Take what is actually in reach. Without this an agent that wants food in
        # a foraged-out region wanders forever past the wood it is standing on.
        if target is None:
            fallback = (
                ResourceKind.WOOD if agent.wants is ResourceKind.FOOD else ResourceKind.FOOD
            )
            target = _nearest_resource(world, position, fallback, config.vision_radius)
            if target is not None:
                agent.wants = fallback

        if target is None:
            if agent.target_x is None or agent.target_y is None:
                agent.target_x = rng.below(config.grid_width)
                agent.target_y = rng.below(config.grid_height)
            return

        agent.target_entity = target

    target_position = world.get(target, Position)
    agent.target_x = target_position.x
    agent.target_y = target_position.y

    if position.x == target_position.x and position.y == target_position.y:
        agent.state = AgentState.GATHER


def _held(inventory: Inventory, kind: ResourceKind) -> int:
    return inventory.food if kind is ResourceKind.FOOD else inventory.wood


def _gather(world: World, entity: Entity, agent: Agent, world_has_room: bool) -> None:
    config = world.config
    inventory = world.get(entity, Inventory)

    target = agent.target_entity
    if target is None or world.get(target, ResourceNode).is_dormant:
        agent.clear_target()
        agent.state = AgentState.IDLE
        return

    node = world.get(target, ResourceNode)

    # Capacity is per resource and refusal leaves the unit in the ground. Without
    # this an agent forages the same node forever and accumulates unbounded stock.
    if _held(inventory, node.kind) >= config.carry_capacity:
        agent.clear_target()
        agent.state = AgentState.IDLE
        return

    agent.gather_progress += 1
    if agent.gather_progress < config.gather_ticks:
        return

    agent.gather_progress = 0
    node.amount -= 1
    if node.kind is ResourceKind.FOOD:
        inventory.food += 1
    else:
        inventory.wood += 1

    if node.is_dormant:
        agent.clear_target()

    if inventory.wood >= config.wood_per_hut and _may_build(agent, config, world_has_room):
        agent.state = AgentState.BUILD
        agent.clear_target()
    elif _held(inventory, node.kind) >= config.carry_capacity:
        agent.state = AgentState.IDLE
        agent.clear_target()


def _decide_from_idle(world: World, entity: Entity, agent: Agent, world_has_room: bool) -> None:
    config = world.config
    needs = world.get(entity, Needs)
    inventory = world.get(entity, Inventory)

    # A starving agent is barred from resting: food is the only way out for it, and
    # the hunger branch below sends it after some.
    if needs.energy < config.need_threshold and not is_starving(needs):
        agent.state = AgentState.REST
        agent.clear_target()
        return

    may_build = _may_build(agent, config, world_has_room)
    room_for_food = inventory.food < config.carry_capacity
    room_for_wood = may_build and inventory.wood < config.carry_capacity

    # Needs outrank construction. Checking wood first lets an agent holding a hut's
    # worth of timber starve on a built-out map: it re-enters BUILD every tick, gets
    # preempted by hunger, and returns to BUILD without ever going to eat.
    if needs.hunger < config.need_threshold and room_for_food:
        agent.wants = ResourceKind.FOOD
        agent.state = AgentState.SEEK_NEED
        return

    if inventory.wood >= config.wood_per_hut and may_build:
        agent.state = AgentState.BUILD
        agent.clear_target()
        return

    # Only forage what there is room to carry. Sending a full agent after more of the
    # same resource makes it walk to a node, get refused, and return here every tick.
    if room_for_food and (inventory.food == 0 or not room_for_wood):
        agent.wants = ResourceKind.FOOD
        agent.state = AgentState.SEEK_NEED
        return

    if room_for_wood:
        agent.wants = ResourceKind.WOOD
        agent.state = AgentState.SEEK_NEED
        return

    if room_for_food:
        agent.wants = ResourceKind.FOOD
        agent.state = AgentState.SEEK_NEED
        return

    # Fully stocked with everything it can use: bank energy instead of pacing.
    agent.clear_target()
    agent.state = AgentState.REST if not is_starving(needs) else AgentState.SEEK_NEED


def run(world: World, rng: TickRng) -> None:
    config = world.config
    rested_energy = int(config.need_max * _RESTED_FRACTION)

    # Counted once per tick rather than per agent: on a crowded map, free tiles get
    # rare enough that hunting for one is not worth an agent's time.
    world_has_room = len(world.query(Building)) < config.build_ceiling()

    for entity in world.query(Agent, Needs, Inventory, Position):
        agent = world.get(entity, Agent)

        if agent.state is AgentState.IDLE:
            _decide_from_idle(world, entity, agent, world_has_room)
        elif agent.state is AgentState.SEEK_NEED:
            _seek(world, entity, agent, rng)
        elif agent.state is AgentState.GATHER:
            _gather(world, entity, agent, world_has_room)
        elif agent.state is AgentState.REST:
            if world.get(entity, Needs).energy >= rested_energy:
                agent.state = AgentState.IDLE
        elif agent.state is AgentState.FLEE:
            agent.state = AgentState.IDLE
            agent.clear_target()
