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
    AdvisorState,
    Clan,
    ClanGoal,
    DecisionLog,
    Inventory,
    MessageType,
    Needs,
    Position,
)
from ..ecs import Entity, World
from ..rng import TickRng
from .messaging import BROADCAST_CLAN, MessageType, make_message, send
from .message_composer import compose_content

logger = logging.getLogger("neociv.leadership")


def log(world: World) -> DecisionLog | None:
    entity = world.first(DecisionLog)
    return world.get(entity, DecisionLog) if entity is not None else None


def advisor_state(world: World) -> AdvisorState | None:
    entity = world.first(AdvisorState)
    return world.get(entity, AdvisorState) if entity is not None else None


def advisor_status(world: World) -> dict:
    """Small read-only summary for the viewer. Never calls the network."""
    state = advisor_state(world)
    advisor = world.advisor
    if advisor is None:
        # A world without an attached advisor behaves exactly like a disabled one.
        return {
            "llm_enabled": world.config.llm_enabled,
            "advisor": "NullAdvisor",
            "pending": 0,
            "calls_made": 0,
            "max_calls": world.config.llm_max_calls_per_session,
        }
    return {
        "llm_enabled": world.config.llm_enabled,
        "advisor": type(advisor).__name__,
        "pending": advisor.pending(),
        "calls_made": state.calls_made if state is not None else 0,
        "max_calls": world.config.llm_max_calls_per_session,
    }


def _inflight_clans(advisor) -> set[int]:
    """What the advisor claims to be waiting on, or nothing if it cannot say.

    An advisor is third-party code. Treating a missing or throwing method as "nothing in
    flight" is the safe reading: recovery will re-send, bounded by attempts and budget,
    rather than the tick loop dying.
    """
    try:
        return set(advisor.inflight_clans())
    except Exception as error:  # noqa: BLE001 - an advisor must never halt the sim
        logger.warning("advisor.inflight_clans failed (%s); assuming nothing in flight", error)
        return set()


def _recover_in_flight(world: World, advisor, state: AdvisorState) -> None:
    """Re-send requests that were in flight when the process stopped.

    `AdvisorState.pending` is durable; the advisor's futures are not. After a reload the
    two disagree, and every entry in that gap is a request the world is waiting on and
    would otherwise wait on forever — the clan's `last_advisor_tick` persisted too, so it
    is not eligible to be asked again.

    A retry is a real API call, so it is counted like any other and capped by
    `llm_max_recovery_attempts`.
    """
    live = _inflight_clans(advisor)
    if not state.pending:
        return

    config = world.config
    survivors: list[dict] = []

    for entry in state.pending:
        clan_id = entry["clan_id"]
        if clan_id in live:
            survivors.append(entry)
            continue

        clan = _clan_by_id(world, clan_id)
        if clan is None or not clan.members:
            logger.info("dropping orphaned advisor request for clan %d", clan_id)
            continue

        if entry["attempts"] >= config.llm_max_recovery_attempts:
            logger.warning(
                "clan %d: advisor request abandoned after %d attempts; rules stand",
                clan_id,
                entry["attempts"],
            )
            # Let the clan be asked afresh once its cooldown lapses.
            clan.last_advisor_tick = world.tick
            continue

        if state.calls_made >= config.llm_max_calls_per_session:
            logger.warning("clan %d: no budget left to recover request; rules stand", clan_id)
            continue

        try:
            accepted = advisor.submit(_brief(world, clan, _nearby_clan_count(world, clan)))
        except Exception as error:  # noqa: BLE001 - same containment as everywhere else
            logger.warning("clan %d: recovery submit failed (%s)", clan_id, error)
            accepted = False

        if accepted:
            state.calls_made += 1
            entry["attempts"] += 1
            survivors.append(entry)
            logger.info(
                "clan %d: re-sent advisor request after reload (attempt %d)",
                clan_id,
                entry["attempts"],
            )

    state.pending = survivors


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
    from .social import _choose_goal, _clan_food_ratio, _clan_huts, _mean_hunger

    living = [m for m in clan.members if world.is_alive(m)]
    wood = sum(
        world.get(m, Inventory).wood for m in living if world.has(m, Inventory)
    )
    raids = sum(
        world.get(m, Agent).raids_lost for m in living if world.has(m, Agent)
    )
    leader = clan.leader
    leader_agent = world.try_get(leader, Agent) if leader is not None else None
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
        # Evaluated now, against the same state the model is about to see, so the two
        # answers are genuinely comparable rather than taken at different ticks.
        rules_goal=_choose_goal(world, clan).value,
        leader_personality=leader_agent.personality if leader_agent is not None else "",
        leader_name=leader_agent.name if leader_agent is not None else "",
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

    previous_goal = clan.goal
    clan.goal = decision.goal
    clan.goal_set_tick = world.tick
    clan.goal_source = decision.source
    clan.goal_reason = decision.reason
    leader = clan.leader
    if leader is not None and world.is_alive(leader) and previous_goal != decision.goal:
        content: dict = {"goal": clan.goal}
        if decision.message:
            content["text"] = decision.message[:120]
        send(
            world,
            leader,
            make_message(
                leader,
                BROADCAST_CLAN,
                MessageType.INFO,
                compose_content(
                    world,
                    leader,
                    MessageType.INFO,
                    content,
                    world.tick,
                ),
            ),
        )
    record(
        world,
        {
            "tick": world.tick,
            "clan": clan.clan_id,
            "goal": decision.goal,
            "source": decision.source,
            "reason": decision.reason,
            "latency_ms": decision.latency_ms,
            "rules_goal": decision.rules_goal,
            "verdict": decision.verdict,
            "message": decision.message,
            # The exact exchange, so a decision can be audited long after it was made.
            # Bounded by llm_log_limit like everything else in this world.
            "prompt": decision.prompt,
            "raw_response": decision.raw_response,
        },
    )
    logger.info(
        "clan %d: %s chose %s (rules would choose %s, %s) in %dms — %s",
        clan.clan_id,
        decision.source,
        decision.goal,
        decision.rules_goal or "?",
        decision.verdict,
        decision.latency_ms,
        decision.reason,
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

    state = advisor_state(world)
    if state is None:
        return

    _recover_in_flight(world, advisor, state)

    try:
        decisions = advisor.collect()
    except Exception as error:  # noqa: BLE001 - an advisor must never halt the sim
        logger.warning("advisor.collect failed (%s); clans stay on rule-based goals", error)
        decisions = []

    for decision in decisions:
        state.drop_pending(decision.clan_id)
        _apply(world, decision)

    # A request that failed or timed out leaves the advisor with nothing in flight for
    # that clan; clear it so recovery does not keep re-sending a doomed request.
    live = _inflight_clans(advisor)
    state.pending = [e for e in state.pending if e["clan_id"] in live]

    config = world.config
    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        if not clan.members:
            continue
        # First-run safety valve: restrict the live path to one clan until its answers
        # have actually been compared against the rules.
        if config.llm_only_clan_id is not None and clan.clan_id != config.llm_only_clan_id:
            continue
        leader = clan.leader
        if leader is None or not world.is_alive(leader) or not world.has(leader, Position):
            continue
        if (
            clan.last_advisor_tick >= 0
            and world.tick - clan.last_advisor_tick < config.llm_min_ticks_between_calls
        ):
            continue
        if state.is_pending(clan.clan_id):
            continue
        # The durable spend counter, not the advisor's in-memory one, is the authority:
        # a restart must not hand the world a fresh budget.
        if state.calls_made >= config.llm_max_calls_per_session:
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
            state.calls_made += 1
            state.add_pending(clan.clan_id, world.tick)
