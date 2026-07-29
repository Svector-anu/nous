"""Clan leaders consulting the middle cognition tier.

Runs immediately before `social`, which is where rule-based goals are chosen and where
every goal is published to the blackboard. The split is deliberate:

    leadership  applies any advisor decision that has arrived, and asks for new ones
    social      chooses a goal by rules for any clan the advisor did not answer for

So the rules are not a degraded mode — they are the floor. A clan whose advisor is slow,
unavailable, over budget, or wrong always has a goal this tick, and the model's answer
refines it when it lands rather than gating it.

Bottom-tier agents are never consulted. Only a clan with a living leader is, and only
every `llm_min_ticks_between_calls` ticks.
"""

from __future__ import annotations

import logging

from ..components import (
    Agent,
    Clan,
    ClanGoal,
    DecisionLog,
    Inventory,
    Needs,
    Position,
)
from ..ecs import Entity, World
from ..rng import TickRng

logger = logging.getLogger("neociv.leadership")


def log(world: World) -> DecisionLog | None:
    entity = world.first(DecisionLog)
    return world.get(entity, DecisionLog) if entity is not None else None


def record(world: World, entry: dict) -> None:
    current = log(world)
    if current is not None:
        current.record(entry, world.config.llm_log_limit)


def _clan_by_id(world: World, clan_id: int) -> Clan | None:
    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        if clan.clan_id == clan_id:
            return clan
    return None


def _brief(world: World, clan: Clan, nearby: int):
    from ...llm.advisor import GoalBrief
    from .social import _clan_food_ratio, _clan_huts, _mean_hunger

    living = [m for m in clan.members if world.is_alive(m)]
    wood = sum(
        world.get(m, Inventory).wood for m in living if world.has(m, Inventory)
    )
    raids = sum(
        world.get(m, Agent).raids_lost for m in living if world.has(m, Agent)
    )
    return GoalBrief(
        clan_id=clan.clan_id,
        tick=world.tick,
        members=len(living),
        mean_hunger=_mean_hunger(world, clan),
        food_ratio=_clan_food_ratio(world, clan),
        huts_per_member=_clan_huts(world, clan) / max(1, len(living)),
        wood_held=wood,
        current_goal=clan.goal,
        nearby_clans=nearby,
        recent_raids_suffered=raids,
    )


def _nearby_clan_count(world: World, clan: Clan) -> int:
    if clan.centre is None:
        return 0
    reach = clan.influence_radius * 2
    count = 0
    for entity in world.query(Clan):
        other = world.get(entity, Clan)
        if other.clan_id == clan.clan_id or other.centre is None:
            continue
        distance = max(
            abs(other.centre[0] - clan.centre[0]), abs(other.centre[1] - clan.centre[1])
        )
        if distance <= reach:
            count += 1
    return count


def _apply(world: World, decision) -> None:
    clan = _clan_by_id(world, decision.clan_id)
    if clan is None or not clan.members:
        return

    clan.goal = decision.goal
    clan.goal_set_tick = world.tick
    clan.goal_source = decision.source
    clan.goal_reason = decision.reason
    record(
        world,
        {
            "tick": world.tick,
            "clan": clan.clan_id,
            "goal": decision.goal,
            "source": decision.source,
            "reason": decision.reason,
            "latency_ms": decision.latency_ms,
        },
    )


def run(world: World, rng: TickRng) -> None:
    """Nothing an advisor does may break the world.

    The advisor is the one component here that reaches outside the simulation, so it is
    the one that can fail in ways the rest of the code never does — a network stall, an
    SDK change, a bad answer, an exception from a third-party implementation. Every call
    into it is contained, and every failure resolves to "no decision", which the rules
    then handle on the very same tick.
    """
    advisor = world.advisor
    if advisor is None:
        return

    try:
        decisions = advisor.collect()
    except Exception as error:  # noqa: BLE001 - an advisor must never halt the sim
        logger.warning("advisor.collect failed (%s); clans stay on rule-based goals", error)
        decisions = []

    for decision in decisions:
        _apply(world, decision)

    config = world.config
    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        if not clan.members:
            continue
        leader = clan.leader
        if leader is None or not world.is_alive(leader) or not world.has(leader, Position):
            continue
        if (
            clan.last_advisor_tick >= 0
            and world.tick - clan.last_advisor_tick < config.llm_min_ticks_between_calls
        ):
            continue

        try:
            accepted = advisor.submit(_brief(world, clan, _nearby_clan_count(world, clan)))
        except Exception as error:  # noqa: BLE001 - same contract as collect
            logger.warning("advisor.submit failed for clan %d (%s)", clan.clan_id, error)
            # Back off this clan for a full cooldown rather than retrying every tick.
            clan.last_advisor_tick = world.tick
            continue

        if accepted:
            clan.last_advisor_tick = world.tick
