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
    ClanRef,
    MessageType,
    Position,
    ResourceKind,
    ResourceNode,
    clan_rally_key,
)
from ..ecs import Entity, World
from ..rng import TickRng
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
    _leader_duties(world)
