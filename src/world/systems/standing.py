"""Agent standing and rank progression.

Standing only increases from recorded actions: donating resources, building huts,
winning raids, surviving, and productive clan membership. It is durable world state
and never granted for free.

Ranks: member -> trusted -> officer. Officers are eligible to inherit clan
leadership when a leader dies.
"""

from __future__ import annotations

from ..components import Agent, Clan, ClanRef, Standing
from ..ecs import Entity, World
from ..rng import TickRng


_RANKS = ("member", "trusted", "officer")


def _ensure_standing(world: World, entity: Entity) -> Standing:
    """Return the agent's Standing, creating it with the current tick as spawn time if missing."""
    standing = world.try_get(entity, Standing)
    if standing is not None:
        return standing
    standing = Standing()
    agent = world.try_get(entity, Agent)
    if agent is not None:
        agent.spawn_tick = world.tick
    world.add(entity, standing)
    return standing


def add_standing(world: World, entity: Entity, amount: int) -> None:
    """Add standing for a recorded action."""
    if amount <= 0:
        return
    st = _ensure_standing(world, entity)
    st.action_value = min(st.action_value + amount, _max_standing(world))
    st.value = min(st.action_value + _time_based_standing(world, st, entity), _max_standing(world))


def _max_standing(world: World) -> int:
    """Cap standing so it cannot grow without bound."""
    config = world.config
    return max(config.standing_officer_threshold * 2, 500)


def _promote(world: World, standing: Standing) -> None:
    """Update rank based on thresholds, promoting only upward."""
    config = world.config
    if standing.rank == "member" and standing.value >= config.standing_trusted_threshold:
        standing.rank = "trusted"
    if standing.rank == "trusted" and standing.value >= config.standing_officer_threshold:
        standing.rank = "officer"


def record_resource_given(world: World, entity: Entity, food: int = 0, wood: int = 0) -> None:
    """Call when an agent donates food or wood to a clanmate."""
    if food <= 0 and wood <= 0:
        return
    standing = _ensure_standing(world, entity)
    config = world.config
    if food:
        standing.given_food += food
        add_standing(world, entity, food * config.standing_per_food_given)
    if wood:
        standing.given_wood += wood
        add_standing(world, entity, wood * config.standing_per_wood_given)


def record_hut_built(world: World, entity: Entity) -> None:
    """Call when an agent finishes building a hut."""
    add_standing(world, entity, world.config.standing_per_hut_built)


def record_raid_won(world: World, entity: Entity) -> None:
    """Call when an agent wins a raid."""
    add_standing(world, entity, world.config.standing_per_raid_won)


def record_clan_join(world: World, entity: Entity) -> None:
    """Call when an agent joins a clan so membership time is measured from this tick."""
    standing = _ensure_standing(world, entity)
    standing.join_tick = world.tick


def record_clan_leave(world: World, entity: Entity) -> None:
    """Call when an agent leaves or is removed from a clan."""
    standing = world.try_get(entity, Standing)
    if standing is not None:
        standing.join_tick = -1


def _time_based_standing(world: World, standing: Standing, entity: Entity) -> int:
    """Standing from survival and clan membership, computed at current tick."""
    agent = world.try_get(entity, Agent)
    if agent is None:
        return 0
    config = world.config
    total = 0
    total += (world.tick - agent.spawn_tick) // config.standing_per_survival_ticks
    if standing.join_tick >= 0 and _is_in_clan(world, entity):
        total += (world.tick - standing.join_tick) // config.standing_per_membership_ticks
    return total


def _is_in_clan(world: World, entity: Entity) -> bool:
    reference = world.try_get(entity, ClanRef)
    if reference is None or reference.clan_id is None:
        return False
    for clan_entity in world.query(Clan):
        clan = world.get(clan_entity, Clan)
        if clan.clan_id == reference.clan_id and entity in clan.members:
            return True
    return False


def run(world: World, rng: TickRng) -> None:
    """Ensure every agent has Standing, apply time-based gains, and promote ranks.

    Time-based standing is computed from durable spawn/join ticks rather than
    accumulated per tick, so it is bounded and deterministic.
    """
    for entity in world.query(Agent):
        st = _ensure_standing(world, entity)
        # Total standing is action-based plus the current time-based portion. Recompute
        # the total every tick so it stays correct without unbounded accumulation.
        st.value = min(
            st.action_value + _time_based_standing(world, st, entity),
            _max_standing(world),
        )
        _promote(world, st)


def eligible_successor(world: World, clan: Clan) -> Entity | None:
    """Return the lowest-id living officer in the clan, or None if no officers exist.

    The existing succession rule is the fallback when no officer is available.
    """
    candidates = []
    for member in clan.members:
        if not world.is_alive(member):
            continue
        standing = world.try_get(member, Standing)
        if standing is not None and standing.rank == "officer":
            candidates.append(member)
    return min(candidates) if candidates else None
