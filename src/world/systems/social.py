"""Clan formation, leadership, and reporting to the blackboard.

Runs after the agents have acted, so what lands on the board is what was actually being
gathered this tick. Everything written here is read on the next tick — the same one-tick
lag messaging has.
"""

from __future__ import annotations

from ..components import (
    BROADCAST_CLAN,
    KEY_FOOD_LOCATIONS,
    KEY_WOOD_LOCATIONS,
    Agent,
    AgentState,
    Clan,
    ClanGoal,
    ClanRef,
    Inventory,
    MessageType,
    Needs,
    Position,
    ResourceKind,
    ResourceNode,
    clan_centre_key,
    clan_goal_key,
    clan_rally_key,
)
from ..ecs import Entity, World
from ..rng import TickRng
from . import leadership
from .blackboard import board
from .messaging import make_message, send

_LOCATION_KEYS = {
    ResourceKind.FOOD: KEY_FOOD_LOCATIONS,
    ResourceKind.WOOD: KEY_WOOD_LOCATIONS,
}


def find_clan(world: World, clan_id: int) -> Clan | None:
    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        if clan.clan_id == clan_id:
            return clan
    return None


def _prune(world: World) -> None:
    """Drop dead members and empty clans. Leadership falls to the lowest surviving id."""
    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        for member in list(clan.members):
            if not world.is_alive(member):
                clan.remove(member)
        if clan.leader is not None and not world.is_alive(clan.leader):
            clan.leader = clan.members[0] if clan.members else None
        if not clan.members:
            world.destroy_entity(entity)


def _record_sightings(world: World) -> None:
    current = board(world)
    if current is None:
        return

    limit = world.config.blackboard_max_locations
    seen: dict[ResourceKind, list[list[int]]] = {ResourceKind.FOOD: [], ResourceKind.WOOD: []}
    reporter: dict[ResourceKind, Entity] = {}

    for entity in world.query(Agent, Position):
        agent = world.get(entity, Agent)
        if agent.state is not AgentState.GATHER:
            continue
        target = agent.target_entity
        if target is None or not world.is_alive(target):
            continue
        node = world.try_get(target, ResourceNode)
        if node is None or node.is_dormant:
            continue

        position = world.get(target, Position)
        coordinate = [position.x, position.y]
        bucket = seen[node.kind]
        if coordinate not in bucket and len(bucket) < limit:
            bucket.append(coordinate)
            reporter.setdefault(node.kind, entity)

    for kind, key in _LOCATION_KEYS.items():
        if seen[kind]:
            current.write(key, seen[kind], reporter[kind], world.tick)


def _update_influence(world: World) -> None:
    """A clan's centre is the mean position of its living members, recomputed each tick.

    No smoothing buffer: members already sit ~1.4 tiles from their centre, so the
    instantaneous mean is stable, and holding no history keeps save/load exact.
    Influence is a soft radius that grows with membership — read-only, no exclusion.
    """
    config = world.config
    current = board(world)

    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        points = [
            world.get(member, Position)
            for member in clan.members
            if world.is_alive(member) and world.has(member, Position)
        ]
        if not points:
            clan.centre = None
            clan.influence_radius = 0
            continue

        clan.centre = [
            round(sum(p.x for p in points) / len(points)),
            round(sum(p.y for p in points) / len(points)),
        ]
        clan.influence_radius = (
            config.influence_base_radius + config.influence_per_member * len(points)
        )
        if current is not None:
            current.write(
                clan_centre_key(clan.clan_id),
                [clan.centre[0], clan.centre[1], clan.influence_radius],
                clan.leader if clan.leader is not None else clan.members[0],
                world.tick,
            )


def influence_at(clan: Clan, x: int, y: int) -> float:
    """1.0 at the centre, falling to 0 at the edge of the radius. Soft by design."""
    if clan.centre is None or clan.influence_radius <= 0:
        return 0.0
    distance = max(abs(x - clan.centre[0]), abs(y - clan.centre[1]))
    if distance >= clan.influence_radius:
        return 0.0
    return 1.0 - distance / clan.influence_radius


def _clan_food_ratio(world: World, clan: Clan) -> float:
    capacity = world.config.carry_capacity
    held, count = 0, 0
    for member in clan.members:
        inventory = world.try_get(member, Inventory)
        if inventory is not None:
            held += inventory.food
            count += 1
    return held / (count * capacity) if count else 1.0


def _clan_huts(world: World, clan: Clan) -> int:
    return sum(
        world.get(member, Agent).huts_owned
        for member in clan.members
        if world.is_alive(member) and world.has(member, Agent)
    )


def _mean_hunger(world: World, clan: Clan) -> float:
    values = [
        world.get(m, Needs).hunger
        for m in clan.members
        if world.is_alive(m) and world.has(m, Needs)
    ]
    return sum(values) / len(values) if values else world.config.need_max


def _choose_goal(world: World, clan: Clan) -> ClanGoal:
    """Deliberately dumb and readable: needs first, then growth, then cohesion.

    Expansion retires on its own. Once every member holds `max_huts_per_agent` huts the
    hut signal can never fire again, so expand and gather_wood are early-life goals and
    mature clans alternate between feeding themselves and staying together.
    """
    config = world.config

    if (
        _clan_food_ratio(world, clan) < config.clan_low_food_ratio
        or _mean_hunger(world, clan) < config.clan_hungry_threshold
    ):
        # Escalation, not a first resort, and gated on actual hunger rather than on empty
        # stores. Keying it to stores alone made every clan raid within 400 ticks of
        # genesis — at world start nobody holds food yet, which is startup conditions,
        # not famine. That cost 47 agents. Desperation is measured on the one signal that
        # means agents are genuinely going without.
        desperate = _mean_hunger(world, clan) < config.clan_desperate_threshold
        already_trying = clan.goal in (ClanGoal.GATHER_FOOD.value, ClanGoal.RAID.value)
        if desperate and already_trying:
            return ClanGoal.RAID
        return ClanGoal.GATHER_FOOD

    wanted_huts = len(clan.members) * config.huts_per_member_target
    if _clan_huts(world, clan) < wanted_huts:
        held_wood = sum(
            world.get(m, Inventory).wood
            for m in clan.members
            if world.is_alive(m) and world.has(m, Inventory)
        )
        needed = config.wood_per_hut * max(1, len(clan.members) // 2)
        return ClanGoal.EXPAND if held_wood >= needed else ClanGoal.GATHER_WOOD

    return ClanGoal.RALLY


def _set_goals(world: World) -> None:
    current = board(world)
    if current is None:
        return

    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        if not clan.members:
            continue

        # An advisor decision applied this tick leaves goal_set_tick fresh, so the rules
        # do not overwrite it. The rules are the floor, not a degraded mode.
        due = world.tick - clan.goal_set_tick >= world.config.goal_review_ticks
        if due or clan.goal_set_tick == 0:
            chosen = _choose_goal(world, clan)
            changed = clan.goal != chosen.value or clan.goal_source != "rules"
            clan.goal = chosen.value
            clan.goal_set_tick = world.tick
            clan.goal_source = "rules"
            clan.goal_reason = ""
            if changed:
                leadership.record(
                    world,
                    {
                        "tick": world.tick,
                        "clan": clan.clan_id,
                        "goal": clan.goal,
                        "source": "rules",
                        "reason": "",
                        "latency_ms": 0,
                        "rules_goal": clan.goal,
                        "verdict": "same",
                        "prompt": "",
                        "raw_response": "",
                    },
                )

        current.write(
            clan_goal_key(clan.clan_id),
            clan.goal,
            clan.leader if clan.leader is not None else clan.members[0],
            world.tick,
        )


def _can_truce(world: World, a: Clan, b: Clan) -> bool:
    """Both have hurt the other, neither is desperate, the fighting has stopped, and they
    are neighbours. A pure condition — no rng, so determinism costs nothing."""
    config = world.config

    if a.is_allied(b.clan_id) or b.is_allied(a.clan_id):
        return False
    if not a.members or not b.members:
        return False

    # A truce settles a real feud, not a one-sided raid.
    if not (a.grudge_against(b.clan_id) and b.grudge_against(a.clan_id)):
        return False

    # Nobody makes peace while starving.
    if _mean_hunger(world, a) < config.clan_desperate_threshold:
        return False
    if _mean_hunger(world, b) < config.clan_desperate_threshold:
        return False

    # The guns have to have been silent for a while.
    last_blow = max(a.last_raided_by(b.clan_id), b.last_raided_by(a.clan_id))
    if world.tick - last_blow < config.truce_peace_ticks:
        return False

    # And they have to be near enough to have contact at all.
    if a.centre is None or b.centre is None:
        return False
    distance = max(abs(a.centre[0] - b.centre[0]), abs(a.centre[1] - b.centre[1]))
    reach = a.influence_radius + b.influence_radius + config.truce_contact_slack
    return distance <= reach


def _form_truces(world: World) -> None:
    """Pairs are visited in ascending clan id, so the order truces form is total."""
    clans = sorted(
        (world.get(e, Clan) for e in world.query(Clan)), key=lambda c: c.clan_id
    )
    for index, first in enumerate(clans):
        for second in clans[index + 1 :]:
            if _can_truce(world, first, second):
                first.add_ally(second.clan_id)
                second.add_ally(first.clan_id)


def _next_clan_id(world: World) -> int:
    existing = [world.get(e, Clan).clan_id for e in world.query(Clan)]
    return max(existing, default=0) + 1


def _form_clans(world: World, rng: TickRng) -> None:
    config = world.config
    radius = config.clan_form_radius
    positions = {e: world.get(e, Position) for e in world.query(Agent, Position, ClanRef)}

    def near(a: Entity, b: Entity) -> bool:
        pa, pb = positions[a], positions[b]
        return abs(pa.x - pb.x) <= radius and abs(pa.y - pb.y) <= radius

    def is_idle_clanless(entity: Entity) -> bool:
        return (
            world.get(entity, Agent).state is AgentState.IDLE
            and world.get(entity, ClanRef).clan_id is None
        )

    ordered = sorted(positions)
    candidates = [e for e in ordered if is_idle_clanless(e)]

    for entity in candidates:
        reference = world.get(entity, ClanRef)
        if reference.clan_id is not None:
            continue
        if not rng.chance(config.clan_form_chance):
            continue

        joined = False
        for other in ordered:
            if other == entity or not near(entity, other):
                continue
            other_clan_id = world.get(other, ClanRef).clan_id
            if other_clan_id is None:
                continue
            clan = find_clan(world, other_clan_id)
            if clan is None or len(clan.members) >= config.max_clan_size:
                continue
            clan.add(entity)
            reference.clan_id = clan.clan_id
            joined = True
            break
        if joined:
            continue

        for other in candidates:
            if other <= entity or not near(entity, other):
                continue
            if world.get(other, ClanRef).clan_id is not None:
                continue
            clan = Clan(clan_id=_next_clan_id(world))
            world.add(world.create_entity(), clan)
            clan.add(entity)
            clan.add(other)
            reference.clan_id = clan.clan_id
            world.get(other, ClanRef).clan_id = clan.clan_id
            break


def _pick_rally(world: World, leader: Entity) -> list[int]:
    """The rally is a meeting point — where the leader is — never a food tile.

    Pointing a clan at the best known food node makes all eight members converge on one
    node, strip it, and starve together; measured as a 120 -> 45 population collapse.
    Resource sharing already happens through the food_locations key, which any agent
    reads when nothing is in sight. The rally is for cohesion only.
    """
    position = world.get(leader, Position)
    return [position.x, position.y]


def _leader_duties(world: World) -> None:
    current = board(world)
    if current is None:
        return

    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        leader = clan.leader
        if leader is None or not world.is_alive(leader) or not world.has(leader, Position):
            continue

        rally = _pick_rally(world, leader)
        current.write(clan_rally_key(clan.clan_id), rally, leader, world.tick)

        # Only announce a rally that actually moved. Broadcasting every tick floods
        # every member's inbox with chatter that evicts real requests, and the board
        # already carries the current point for anyone who wants it.
        previous = clan.last_rally
        if previous is None or max(abs(rally[0] - previous[0]), abs(rally[1] - previous[1])) > world.config.rally_arrival_radius:
            clan.last_rally = rally
            send(
                world,
                leader,
                make_message(leader, BROADCAST_CLAN, MessageType.INFO, {"rally": rally}),
            )


def run(world: World, rng: TickRng) -> None:
    _prune(world)
    _record_sightings(world)
    _form_clans(world, rng)
    _update_influence(world)
    _form_truces(world)
    _set_goals(world)
    _leader_duties(world)
