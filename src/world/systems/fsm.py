"""Agent state machine and resource harvesting.

Implements the Phase 0 diagram: IDLE -> SEEK_NEED -> GATHER -> BUILD, with REST
and FLEE as side branches. FLEE has no trigger yet because Phase 0 has no
hostiles; the state exists so combat in Phase 1 has somewhere to transition to.
"""

from __future__ import annotations

from ..components import (
    KEY_FOOD_LOCATIONS,
    KEY_WOOD_LOCATIONS,
    Agent,
    AgentState,
    Building,
    ClanGoal,
    ClanRef,
    Inventory,
    Needs,
    Position,
    ResourceKind,
    ResourceNode,
    clan_centre_key,
    clan_goal_key,
    clan_rally_key,
)
from ..config import WorldConfig
from ..ecs import Entity, World
from ..rng import TickRng
from ..spatial import ResourceIndex
from .blackboard import board
from .needs import is_starving

_RESTED_FRACTION = 0.9


def _may_build(agent: Agent, config: WorldConfig, world_has_room: bool) -> bool:
    """An agent stops choosing BUILD once it owns its allowance of huts, or once the
    map is too built-up to be worth hunting for a free tile."""
    return world_has_room and agent.huts_owned < config.max_huts_per_agent


def _chebyshev(position: Position, target: tuple[int, int]) -> int:
    return max(abs(position.x - target[0]), abs(position.y - target[1]))


def _blackboard_hint(
    world: World, origin: Position, kind: ResourceKind, index: ResourceIndex
) -> tuple[int, int] | None:
    """Nearest reported location of `kind` that still has something in the ground."""
    current = board(world)
    if current is None:
        return None

    reported = current.read(
        KEY_FOOD_LOCATIONS if kind is ResourceKind.FOOD else KEY_WOOD_LOCATIONS
    )
    if not isinstance(reported, list) or not reported:
        return None

    ordered = sorted(
        reported,
        key=lambda c: (c[0] - origin.x) ** 2 + (c[1] - origin.y) ** 2,
    )
    for coordinate in ordered:
        x, y = int(coordinate[0]), int(coordinate[1])
        if (x, y) != (origin.x, origin.y) and index.live_at(x, y, kind):
            return x, y
    return None


def _clan_goal(world: World, entity: Entity) -> str | None:
    reference = world.try_get(entity, ClanRef)
    if reference is None or reference.clan_id is None:
        return None
    current = board(world)
    if current is None:
        return None
    goal = current.read(clan_goal_key(reference.clan_id))
    return goal if isinstance(goal, str) else None


def _goal_preference(goal: str | None) -> ResourceKind | None:
    """Which resource the clan's current goal pulls a member toward."""
    if goal == ClanGoal.GATHER_FOOD.value:
        return ResourceKind.FOOD
    if goal in (ClanGoal.GATHER_WOOD.value, ClanGoal.EXPAND.value):
        return ResourceKind.WOOD
    return None


def _clan_centre(world: World, entity: Entity) -> tuple[int, int, int] | None:
    """(x, y, influence_radius) of the agent's clan, or None."""
    reference = world.try_get(entity, ClanRef)
    if reference is None or reference.clan_id is None:
        return None
    current = board(world)
    if current is None:
        return None
    centre = current.read(clan_centre_key(reference.clan_id))
    if isinstance(centre, list) and len(centre) == 3:
        return int(centre[0]), int(centre[1]), int(centre[2])
    return None


def _rally_point(world: World, entity: Entity) -> tuple[int, int] | None:
    reference = world.try_get(entity, ClanRef)
    if reference is None or reference.clan_id is None:
        return None
    current = board(world)
    if current is None:
        return None
    rally = current.read(clan_rally_key(reference.clan_id))
    if isinstance(rally, list) and len(rally) == 2:
        return int(rally[0]), int(rally[1])
    return None


def _seek(
    world: World, entity: Entity, agent: Agent, rng: TickRng, index: ResourceIndex
) -> None:
    config = world.config
    position = world.get(entity, Position)

    target = agent.target_entity
    if target is not None and world.get(target, ResourceNode).is_dormant:
        agent.clear_target()
        target = None

    if target is None:
        target = index.nearest(position, agent.wants, config.vision_radius)

        # Take what is actually in reach. Without this an agent that wants food in
        # a foraged-out region wanders forever past the wood it is standing on.
        if target is None:
            fallback = (
                ResourceKind.WOOD if agent.wants is ResourceKind.FOOD else ResourceKind.FOOD
            )
            target = index.nearest(position, fallback, config.vision_radius)
            if target is not None:
                agent.wants = fallback

        # Nothing in sight. Before wandering blind, check what the clan has written
        # down. Hints are validated against live nodes at read time, so a sighting of
        # a since-foraged tile is ignored rather than walked to.
        if target is None:
            hint = _blackboard_hint(world, position, agent.wants, index)
            if hint is not None:
                agent.target_x, agent.target_y = hint
                return

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


def _decide_from_idle(
    world: World, entity: Entity, agent: Agent, world_has_room: bool, rng: TickRng
) -> None:
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

    goal = _clan_goal(world, entity)

    if inventory.wood >= config.wood_per_hut and may_build:
        # An expanding clan builds inside its own influence. Members almost always
        # already are — they sit ~1.4 tiles from the centre — so this walk is rare,
        # and it is what stops a clan's huts from scattering across the map.
        if goal == ClanGoal.EXPAND.value and needs.energy >= config.social_energy_floor:
            home = _clan_centre(world, entity)
            position = world.get(entity, Position)
            if home is not None and _chebyshev(position, home[:2]) > home[2]:
                agent.clear_target()
                agent.target_x, agent.target_y = home[0], home[1]
                agent.state = AgentState.FOLLOW
                return
        agent.state = AgentState.BUILD
        agent.clear_target()
        return

    # Everything above this line is survival or a standing commitment and is never
    # overridden. From here down the choice is discretionary, which is the only place
    # a clan goal gets to lean on it — as a bias, decided by rng, not a command.
    if goal == ClanGoal.RALLY.value and not is_starving(needs) and needs.energy >= config.social_energy_floor:
        rally = _rally_point(world, entity)
        position = world.get(entity, Position)
        if rally is not None and _chebyshev(position, rally) > config.rally_arrival_radius:
            if rng.chance(config.goal_bias_chance):
                agent.clear_target()
                agent.target_x, agent.target_y = rally
                agent.state = AgentState.FOLLOW
                return

    preferred = _goal_preference(goal)
    if preferred is not None and rng.chance(config.goal_bias_chance):
        if preferred is ResourceKind.FOOD and room_for_food:
            agent.wants = ResourceKind.FOOD
            agent.state = AgentState.SEEK_NEED
            return
        if preferred is ResourceKind.WOOD and room_for_wood:
            agent.wants = ResourceKind.WOOD
            agent.state = AgentState.SEEK_NEED
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

    # Nothing left to forage. If the clan has named a meeting point and this agent is
    # both well rested and not standing at it already, go join the others.
    # Socialising is strictly a surplus activity: walking costs energy and returns none,
    # so an agent only does it well clear of the threshold where it would rather rest.
    if not is_starving(needs) and needs.energy >= config.social_energy_floor:
        rally = _rally_point(world, entity)
        position = world.get(entity, Position)
        if rally is not None and _chebyshev(position, rally) > config.rally_arrival_radius:
            agent.clear_target()
            agent.target_x, agent.target_y = rally
            agent.state = AgentState.FOLLOW
            return

    agent.clear_target()
    agent.state = AgentState.REST if not is_starving(needs) else AgentState.SEEK_NEED


def run(world: World, rng: TickRng) -> None:
    config = world.config
    rested_energy = int(config.need_max * _RESTED_FRACTION)

    # Counted once per tick rather than per agent: on a crowded map, free tiles get
    # rare enough that hunting for one is not worth an agent's time.
    world_has_room = len(world.query(Building)) < config.build_ceiling()
    index = ResourceIndex(world, config.spatial_chunk_size)

    for entity in world.query(Agent, Needs, Inventory, Position):
        agent = world.get(entity, Agent)

        if agent.state is AgentState.IDLE:
            _decide_from_idle(world, entity, agent, world_has_room, rng)
        elif agent.state is AgentState.SEEK_NEED:
            _seek(world, entity, agent, rng, index)
        elif agent.state is AgentState.GATHER:
            _gather(world, entity, agent, world_has_room)
        elif agent.state is AgentState.REST:
            if world.get(entity, Needs).energy >= rested_energy:
                agent.state = AgentState.IDLE
        elif agent.state is AgentState.FOLLOW:
            rally = _rally_point(world, entity)
            position = world.get(entity, Position)
            if rally is None or _chebyshev(position, rally) <= config.rally_arrival_radius:
                agent.state = AgentState.IDLE
                agent.clear_target()
            else:
                agent.target_x, agent.target_y = rally
        elif agent.state is AgentState.MEET:
            # Arrived at the donor, or the trip stopped making sense. Either way go
            # idle: if still hungry the agent re-asks, and the donor is adjacent now.
            position = world.get(entity, Position)
            target = (agent.target_x, agent.target_y)
            arrived = target[0] is None or _chebyshev(position, (target[0], target[1])) <= config.transfer_radius
            if arrived or world.get(entity, Inventory).food >= config.carry_capacity:
                agent.state = AgentState.IDLE
                agent.clear_target()
        elif agent.state is AgentState.FLEE:
            agent.state = AgentState.IDLE
            agent.clear_target()
