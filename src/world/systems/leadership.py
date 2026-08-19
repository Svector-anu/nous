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
    ForceDecisionQueue,
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
    """Small read-only summary for the viewer. Never calls the network.

    `llm_enabled` is what the operator asked for. `working` is what is actually true, and
    the two come apart constantly: an advisor self-disables on bad credentials, and a
    world that has spent its budget stops asking. Both leave the config saying yes while
    every clan quietly falls back to rules.

    Reporting only the config is how a dead advisor went unnoticed on a live world for
    days — the status said `ClaudeAdvisor` the entire time it was answering nothing. A
    status that cannot say no is not a status.
    """
    config = world.config
    state = advisor_state(world)
    advisor = world.advisor
    calls_made = state.calls_made if state is not None else 0
    max_calls = config.llm_max_calls_per_session

    if advisor is None:
        # A world without an attached advisor behaves exactly like a disabled one.
        return {
            "llm_enabled": config.llm_enabled,
            "advisor": "NullAdvisor",
            "working": False,
            "reason": "no advisor attached",
            "pending": 0,
            "calls_made": 0,
            "max_calls": max_calls,
            "budget_spent": False,
        }

    # Read, never construct: `available` would build a client, so asking whether this
    # works would be the thing that decides whether it works.
    reason = str(getattr(advisor, "unavailable_reason", "") or "")
    budget_spent = calls_made >= max_calls
    if not reason:
        if not config.llm_enabled:
            reason = "llm disabled in config"
        elif getattr(advisor, "name", "") == "none":
            reason = "no provider configured"
        elif budget_spent:
            reason = f"call budget spent ({calls_made}/{max_calls})"

    return {
        "llm_enabled": config.llm_enabled,
        "advisor": type(advisor).__name__,
        "working": not reason,
        "reason": reason,
        "pending": advisor.pending(),
        "calls_made": calls_made,
        "max_calls": max_calls,
        "budget_spent": budget_spent,
        # The world-wide default model. Per-clan overrides show up on the clan itself.
        "model": getattr(config, "llm_model", ""),
    }


def _note_budget_spent(world: World, state) -> None:
    """Say it out loud, once, the moment the last call is spent.

    Running out of budget is not an error and nothing goes wrong: every clan simply uses
    the rules from here on. That is exactly why it needs announcing — a world that stops
    consulting a model looks identical to one that never started, and the only moment the
    difference is visible is this one.

    Fires on equality, so it logs exactly once per world however many ticks follow. A cap
    lowered below what a world has already spent never trips it; the `budget_spent` field
    in `advisor_status` covers that case, and covers it on every request.
    """
    if state.calls_made == world.config.llm_max_calls_per_session:
        logger.warning(
            "advisor budget spent: %d of %d calls used; every clan now uses the rules. "
            "raise llm_max_calls_per_session (env LLM_MAX_CALLS_PER_SESSION) to continue.",
            state.calls_made,
            world.config.llm_max_calls_per_session,
        )


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
            _note_budget_spent(world, state)
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
        # Carry the clan's chosen model override into the worker thread. Empty means
        # "use the advisor's default"; the advisor resolves it without a shared mutation.
        model=clan.advisor_model,
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


# How many receipts to keep. Bounded like every other log here.
PAID_RECEIPT_LIMIT = 20


def apply_forced_decisions(world: World) -> list[int]:
    """Drain paid force-decision requests. Returns the clan ids that will re-decide.

    A clan reviews its goal every `goal_review_ticks`. Forcing a decision resets that
    clock, so `social` — which runs immediately after this and chooses goals by rule —
    reconsiders the clan on this very tick instead of whenever its turn came round.

    Deliberately independent of the advisor. The rules are the floor, not a degraded
    mode, so a forced decision produces a real, immediate, visible change whether or not
    an llm is configured — and somebody who paid for one gets what they paid for on a
    world that costs nothing to run.

    Drained here rather than at the http boundary because what history depends on must be
    the queue entry at a fixed tick, never the request that produced it.
    """
    entity = world.first(ForceDecisionQueue)
    if entity is None:
        return []
    queue = world.get(entity, ForceDecisionQueue)
    if not queue.pending:
        return []

    # Ascending, so a reloaded world applies them in the same order.
    wanted = sorted({int(entry.get("clan_id", 0)) for entry in queue.pending})
    # The receipt, kept before the queue is cleared: what each payment actually bought,
    # so a spectator can see somebody paid and go and check the transaction themselves.
    # A paid action that leaves no trace is indistinguishable from one that did nothing.
    for entry in queue.pending:
        queue.applied.append(
            {
                "clan_id": int(entry.get("clan_id", 0)),
                "tick": world.tick,
                "proof": str(entry.get("proof", "")),
                # Carried from the request rather than read from config here: the price
                # can change between a payment being made and this tick applying it, and
                # the receipt should say what was charged, not what is charged now.
                "amount": str(entry.get("amount", "")),
                "currency": str(entry.get("currency", "")),
            }
        )
    del queue.applied[:-PAID_RECEIPT_LIMIT]
    _bank_payments(world, queue.pending)
    queue.pending.clear()

    forced: list[int] = []
    for entity_id in world.query(Clan):
        clan = world.get(entity_id, Clan)
        if clan.clan_id in wanted and clan.members:
            # 0 is what a clan that has never decided carries, and `social` treats it as
            # due — so this asks for a decision rather than inventing one here.
            clan.goal_set_tick = 0
            forced.append(clan.clan_id)
    return forced


def _bank_payments(world: World, entries: list[dict]) -> None:
    """Turn a paid decision into money the world can spend on thinking, and into earnings
    for the leader who was interrupted.

    The whole payment enters the envelope; a share of it is also credited to the clan's
    leader as its own balance. Both, not either — a balance is a *claim* on the pool, so
    crediting an agent without funding the pool would promise money the world does not
    have. That invariant is the reason this is the only place an agent earns: paying out
    for a won raid or a finished hut would mint claims against nothing.

    Silent when the world has no spend book, which is every world that has not been
    resumed since it existed.
    """
    from .. import spend as governor
    from ..config import TICKS_PER_DAY  # noqa: F401 - kept for symmetry with callers

    book = governor.book(world)
    if book is None or not entries:
        return

    from ...chain.settings import USDG_DECIMALS
    from ...chain.x402 import price_units

    share = min(1.0, max(0.0, float(getattr(world.config, "agent_earning_share", 0.0))))
    for entry in entries:
        units = price_units(str(entry.get("amount", "")), USDG_DECIMALS)
        if units <= 0:
            continue
        governor.fund(book, units, by=f"paid:{entry.get('proof', '')}", tick=world.tick)
        if share <= 0:
            continue
        clan = _clan_by_id(world, int(entry.get("clan_id", 0)))
        leader = clan.leader if clan is not None else None
        if leader is None or not world.is_alive(leader):
            # Nobody to pay. The money stays in the envelope, which is the honest outcome
            # — it does not vanish and it is not credited to whoever happens to be next.
            continue
        governor.credit(book, leader, int(units * share))


def _metered(world) -> tuple:
    """The spend book and the cost of a call, when the world is metering its own thinking.

    Returns (None, 0) unless an operator has switched spending on. That default matters:
    every world so far has billed the operator's gateway account directly and known
    nothing about the envelope, and enabling this must be a decision rather than a
    consequence of deploying.
    """
    from .. import spend as governor

    book = governor.book(world)
    if book is None or not book.enabled:
        return None, 0
    return book, max(0, int(getattr(world.config, "llm_cost_units_per_call", 0)))


def _unreserve(world, book, cost: int) -> None:
    """Give back what a call that never happened was holding."""
    if book is None or cost <= 0:
        return
    from .. import spend as governor

    governor.release(book, cost)


def _reconcile_reserved(world, book, cost: int, state: AdvisorState) -> None:
    """Hold no more than the requests actually in flight are worth.

    A reservation is released when its decision lands, but an advisor that times out
    returns nothing at all — the world never learns, and the reservation would sit there
    forever holding budget nobody can spend. Rebuilding the figure from what is genuinely
    pending is self-healing and needs no bookkeeping to go wrong.
    """
    from .. import spend as governor

    owed = len(state.pending) * cost
    if book.reserved_units > owed:
        governor.release(book, book.reserved_units - owed)


def run(world: World, rng: TickRng) -> None:
    """Nothing an advisor does may break the world.

    The advisor is the one component here that reaches outside the simulation, so it is
    the one that can fail in ways the rest of the code never does — a network stall, an
    SDK change, a bad answer, an exception from a third-party implementation. Every call
    into it is contained, and every failure resolves to "no decision", which the rules
    then handle on the very same tick.
    """
    # Before every advisor guard below: a paid decision must land whether or not an llm
    # is configured, and those guards return early when it is not.
    apply_forced_decisions(world)

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

    book, cost = _metered(world)

    for decision in decisions:
        state.drop_pending(decision.clan_id)
        _apply(world, decision)
        if book is not None and cost > 0:
            # The money has left. `commit` releases the reservation this call made and
            # records the spend, so what the world paid to think is visible in the same
            # place as what people paid to fund it.
            from .. import spend as governor

            governor.commit(book, None, cost, world.tick, note=f"clan {decision.clan_id}")

    # A request that failed or timed out leaves the advisor with nothing in flight for
    # that clan; clear it so recovery does not keep re-sending a doomed request.
    live = _inflight_clans(advisor)
    state.pending = [e for e in state.pending if e["clan_id"] in live]

    # Once pending holds only what is genuinely still in flight, it is the truth about
    # what the envelope should be holding. Reconciling here rather than at the top of the
    # tick is the difference between exact and a tick behind.
    if book is not None:
        _reconcile_reserved(world, book, cost, state)

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
        # And the money. The call cap counts calls; this counts what they cost, against an
        # envelope somebody paid for. Refused means the rules decide this tick, which is
        # what happens for every other reason a clan is not asked — it is the floor, not a
        # degraded mode.
        #
        # agent_id is None: the world is spending on its own behalf rather than an agent
        # spending what it earned, so there is no balance to check.
        if book is not None and cost > 0:
            from .. import spend as governor

            refused = governor.reserve(book, None, cost, world.tick)
            if refused:
                logger.debug("clan %d: not asking, %s", clan.clan_id, refused)
                continue

        try:
            accepted = advisor.submit(_brief(world, clan, _nearby_clan_count(world, clan)))
        except Exception as error:  # noqa: BLE001 - same contract as collect
            logger.warning("advisor.submit failed for clan %d (%s)", clan.clan_id, error)
            # Back off this clan for a full cooldown rather than retrying every tick.
            clan.last_advisor_tick = world.tick
            _unreserve(world, book, cost)
            continue

        if not accepted:
            # The advisor declined — no call was made, so the money it was holding goes
            # back. `_reconcile_reserved` would catch this on the next tick anyway, but a
            # reservation released where it was taken is one nobody has to reason about.
            _unreserve(world, book, cost)

        if accepted:
            clan.last_advisor_tick = world.tick
            state.calls_made += 1
            _note_budget_spent(world, state)
            state.add_pending(clan.clan_id, world.tick)
