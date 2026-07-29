"""Raiding.

Runs after trade, so asking nicely always happens before taking by force, and before the
fsm, so an agent that just lost a fight decides its next move already wounded.

The whole design is deliberately small:

- **scarcity drives it.** only a member of a clan on the `raid` goal robs anyone, and a
  clan only reaches that goal after a full review window on `gather_food` left it still
  short. a world with enough food never sees a raid.
- **no pursuit.** a raider robs someone it already finds itself next to. it never chases.
  that is what makes the obvious livelock — a starving raider forever chasing a target it
  cannot catch — impossible by construction rather than by tuning.
- **energy is the only currency.** a fight costs both sides energy, costs the loser more,
  and kills through `energy == 0`, the same single death rule starvation uses. no health
  component, no weapons, no revenge, no territory.
"""

from __future__ import annotations

from ..components import (
    BROADCAST_CLAN,
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
)
from ..ecs import Entity, World
from ..rng import TickRng
from .blackboard import board
from .messaging import make_message, send
from .needs import kill_if_exhausted
from .social import clan_goal_key
from .trade import transfer


def _clan_of(world: World, entity: Entity) -> int | None:
    reference = world.try_get(entity, ClanRef)
    return reference.clan_id if reference is not None else None


def _is_raiding(world: World, entity: Entity) -> bool:
    clan_id = _clan_of(world, entity)
    if clan_id is None:
        return False
    current = board(world)
    if current is None:
        return False
    return current.read(clan_goal_key(clan_id)) == ClanGoal.RAID.value


def _victim(world: World, raider: Entity, positions: dict[Entity, Position]) -> Entity | None:
    """Robbable neighbour in reach: not a clanmate, carrying food.

    Among those in reach, someone from a clan that has robbed us is taken first — a
    grudge is settled before a stranger is troubled. Distance breaks ties, then entity id,
    so the choice is total and needs no rng of its own.
    """
    radius = world.config.transfer_radius
    origin = positions[raider]
    raider_clan = _clan_of(world, raider)
    own_clan = _clan_by_id(world, raider_clan) if raider_clan is not None else None

    best: Entity | None = None
    best_rank: tuple[int, int, int] | None = None

    for other, position in sorted(positions.items()):
        if other == raider:
            continue
        other_clan = _clan_of(world, other)
        if other_clan is not None and other_clan == raider_clan:
            continue
        if world.get(other, Inventory).food <= 0:
            continue
        dx = position.x - origin.x
        dy = position.y - origin.y
        if abs(dx) > radius or abs(dy) > radius:
            continue

        grudge = own_clan.grudge_against(other_clan) if own_clan is not None else 0
        # Negated so a larger grudge sorts first; distance and id keep it deterministic.
        rank = (-grudge, dx * dx + dy * dy, other)
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best = other

    return best


def _clan_by_id(world: World, clan_id: int) -> Clan | None:
    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        if clan.clan_id == clan_id:
            return clan
    return None


def _remember_raid(world: World, victim: Entity, raider: Entity) -> None:
    """The victim's clan remembers the raider's clan.

    Both sides must be affiliated for a grudge to exist: a lone robber has no clan to
    blame, and a lone victim has no clan to do the remembering.
    """
    victim_clan_id = _clan_of(world, victim)
    raider_clan_id = _clan_of(world, raider)
    if victim_clan_id is None or raider_clan_id is None:
        return

    victim_clan = _clan_by_id(world, victim_clan_id)
    if victim_clan is None:
        return
    victim_clan.record_raid(raider_clan_id, world.tick)


def _flee_from(world: World, loser: Entity, winner: Entity) -> None:
    config = world.config
    here = world.get(loser, Position)
    there = world.get(winner, Position)

    dx = here.x - there.x
    dy = here.y - there.y
    if dx == 0 and dy == 0:
        dx = 1

    agent = world.get(loser, Agent)
    agent.clear_target()
    agent.target_x = max(0, min(config.grid_width - 1, here.x + dx * config.flee_distance))
    agent.target_y = max(0, min(config.grid_height - 1, here.y + dy * config.flee_distance))
    agent.state = AgentState.FLEE


def _resolve(world: World, raider: Entity, victim: Entity, rng: TickRng) -> None:
    config = world.config
    raider_needs = world.get(raider, Needs)
    victim_needs = world.get(victim, Needs)

    # Energy is the only measure of strength, so a well-fed defender usually keeps its
    # food and a desperate raider is exactly the one most likely to lose the attempt.
    total = raider_needs.energy + victim_needs.energy
    raider_wins = rng.chance(raider_needs.energy / total) if total > 0 else True

    winner, loser = (raider, victim) if raider_wins else (victim, raider)
    winner_needs = raider_needs if raider_wins else victim_needs
    loser_needs = victim_needs if raider_wins else raider_needs

    winner_needs.energy = max(0, winner_needs.energy - config.combat_energy_cost)
    loser_needs.energy = max(
        0, loser_needs.energy - config.combat_energy_cost - config.combat_loser_penalty
    )

    world.get(winner, Agent).raids_won += 1
    world.get(loser, Agent).raids_lost += 1

    if raider_wins:
        taken = transfer(world, victim, raider, ResourceKind.FOOD, config.raid_steal_amount)
        # A grudge is earned by losing food, not by losing a fight — a scuffle that took
        # nothing leaves no debt.
        if taken > 0:
            _remember_raid(world, victim=victim, raider=raider)

    _raise_alarm(world, victim)

    if kill_if_exhausted(world, loser, loser_needs):
        return
    _flee_from(world, loser, winner)
    kill_if_exhausted(world, winner, winner_needs)


def _raise_alarm(world: World, victim: Entity) -> None:
    """`alert` already means "I need food now" and donors answer it by dipping into their
    reserve. A robbed agent has just lost food, so the existing handler is the right one —
    combat simply gives the message a second trigger."""
    if _clan_of(world, victim) is None:
        return
    position = world.get(victim, Position)
    send(
        world,
        victim,
        make_message(
            victim,
            BROADCAST_CLAN,
            MessageType.ALERT,
            {"resource": ResourceKind.FOOD.value, "reason": "raid", "at": [position.x, position.y]},
        ),
    )


def run(world: World, rng: TickRng) -> None:
    raiders = [
        entity
        for entity in world.query(Agent, Needs, Inventory, Position)
        if _is_raiding(world, entity)
    ]
    if not raiders:
        return

    positions = {
        entity: world.get(entity, Position)
        for entity in world.query(Agent, Needs, Inventory, Position)
    }

    for raider in raiders:
        if not world.is_alive(raider):
            continue
        if world.get(raider, Inventory).food >= world.config.carry_capacity:
            continue
        victim = _victim(world, raider, positions)
        if victim is None or not world.is_alive(victim):
            continue
        _resolve(world, raider, victim, rng)
        positions = {
            entity: position
            for entity, position in positions.items()
            if world.is_alive(entity)
        }
