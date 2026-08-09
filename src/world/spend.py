"""The governor between an agent's intent and the wallet.

Every other system here moves numbers that only mean something inside the simulation. This
one decides whether real money leaves it, autonomously, while nobody is watching. So it is
written as a permission system that happens to keep a ledger, rather than a ledger that
happens to have a limit.

Nothing in this module signs, sends or settles anything. It answers one question — may
this agent spend this much, right now — and records what happened. The client that
actually pays is a separate concern and cannot bypass this, because it has no other way to
learn its own headroom.

The design assumption worth stating: a cap is not a control. A cap tells you the most you
can lose; it never tells you that you are losing it. An advisor died quietly on this world
and nobody knew for two days, which is exactly the failure a cap does not catch. So every
threshold crossing is recorded for the monitor to send on, and the envelope has to be
raised by hand rather than topped up automatically.
"""

from __future__ import annotations

from .components import SpendBook

# Bounded, like every accumulator in a world that runs forever.
NOTICE_LIMIT = 50
SPEND_LIMIT = 100
APPROVAL_LIMIT = 50

# Fractions of the envelope worth telling somebody about. Chosen so the first notice
# arrives with room to act and the last arrives before anything is refused.
THRESHOLDS = (0.5, 0.8, 1.0)


def key(agent_id) -> str:
    """One place agent ids become ledger keys. json has no integer keys, so a balance
    written under 1 comes back under "1"; normalising in one function is what stops the
    two halves of that from ever disagreeing."""
    return str(agent_id)


def book(world) -> SpendBook | None:
    entity = world.first(SpendBook)
    return None if entity is None else world.get(entity, SpendBook)


def available(spend: SpendBook) -> int:
    """Headroom in the envelope. Reserved money is already gone as far as this is
    concerned — two concurrent spends must not both fit in the same gap."""
    return max(0, spend.budget_units - spend.spent_units - spend.reserved_units)


def tick_headroom(spend: SpendBook, tick: int) -> int:
    """What is left of this tick's burst allowance. Zero means unlimited, matching how
    every other optional cap in this project reads."""
    if spend.per_tick_units <= 0:
        return 1 << 62
    used = spend.spent_this_tick if spend.tick_of_spend == tick else 0
    return max(0, spend.per_tick_units - used)


def refuse(spend: SpendBook, agent_id, units: int, tick: int) -> str:
    """Why this spend may not happen, or "" if it may.

    Ordered deliberately. `halted` is checked before anything else so a kill switch beats
    any amount of headroom, and `enabled` before that so a world nobody switched on can
    never be argued into spending.

    `agent_id=None` means the world itself is spending — a clan leader consulting a model
    on the world's behalf rather than an agent spending what it earned. Everything else
    still applies; only the balance check is skipped, because the world's balance *is* the
    envelope and requiring it to have earned its own money twice would be nonsense.
    """
    if not spend.enabled:
        return "spending is not enabled"
    if spend.halted:
        return "spending is halted"
    if units <= 0:
        return "nothing to spend"
    if units > available(spend):
        return f"over budget: {units} needed, {available(spend)} left"
    if units > tick_headroom(spend, tick):
        return f"over the per-tick cap: {units} needed, {tick_headroom(spend, tick)} left this tick"
    if agent_id is not None and units > spend.balances.get(key(agent_id), 0):
        return f"agent {agent_id} has not earned {units}"
    return ""


def credit(spend: SpendBook, agent_id: int, units: int) -> None:
    """Earnings. A claim on the pool, never a key — the agent holds nothing it could
    lose, and the world holds nothing it would have to custody per agent."""
    if units <= 0:
        return
    spend.balances[key(agent_id)] = spend.balances.get(key(agent_id), 0) + units


def reserve(spend: SpendBook, agent_id, units: int, tick: int) -> str:
    """Claim headroom before the money is actually sent.

    Same shape as the payment guard, for the same reason: settlement is a network round
    trip, and anything that checks a limit before awaiting and commits after it will let
    every request that arrives in between through. One payment bought five clan decisions
    that way. Here it would be one envelope buying several times its own size.
    """
    reason = refuse(spend, agent_id, units, tick)
    if reason:
        return reason
    spend.reserved_units += units
    return ""


def release(spend: SpendBook, units: int) -> None:
    """Give back a reservation that never settled. A call that failed bought nothing and
    must not hold the envelope hostage."""
    spend.reserved_units = max(0, spend.reserved_units - units)


def commit(spend: SpendBook, agent_id, units: int, tick: int, note: str = "") -> list[dict]:
    """Money has left. Records it, debits the agent, and returns any notices raised.

    Returns the notices rather than sending them: this module has no business knowing
    about telegram, and a system that both decides and announces is a system nobody can
    test without a network.
    """
    release(spend, units)
    spend.spent_units += units
    if spend.tick_of_spend != tick:
        spend.tick_of_spend = tick
        spend.spent_this_tick = 0
    spend.spent_this_tick += units
    if agent_id is not None:
        spend.balances[key(agent_id)] = max(0, spend.balances.get(key(agent_id), 0) - units)

    spend.spends.append({"agent_id": agent_id, "units": units, "tick": tick, "note": note})
    del spend.spends[:-SPEND_LIMIT]
    return _raise_notices(spend, tick)


def _raise_notices(spend: SpendBook, tick: int) -> list[dict]:
    """One notice per threshold per envelope, so a busy world does not send the same
    warning every tick for the rest of its life."""
    if spend.budget_units <= 0:
        return []
    used = spend.spent_units / spend.budget_units
    already = {n["threshold"] for n in spend.notices}
    fresh = []
    for threshold in THRESHOLDS:
        if used < threshold or threshold in already:
            continue
        notice = {
            "threshold": threshold,
            "tick": tick,
            "spent_units": spend.spent_units,
            "budget_units": spend.budget_units,
            # The last one is not a warning, it is a stop.
            "halts": threshold >= 1.0,
        }
        fresh.append(notice)
        spend.notices.append(notice)
        if notice["halts"]:
            # Exhausting an envelope stops spending until somebody approves more. It does
            # not roll over, and it does not quietly keep going at a lower rate.
            spend.halted = True
    del spend.notices[:-NOTICE_LIMIT]
    return fresh


def fund(spend: SpendBook, units: int, by: str, tick: int) -> None:
    """Raise the envelope with money somebody paid. Never clears a halt.

    This is `approve` minus the authority. An operator lifting a halt has looked at the
    numbers and decided; a payment arriving has decided nothing, and the first version of
    this used `approve` — so a stranger with 0.10 to spend could have cleared an operator's
    kill switch, which is the one control that is supposed to answer to nobody.

    Notices are kept for the same reason: they are the record that somebody was warned,
    and a payment is not somebody reading them.
    """
    if units <= 0:
        return
    spend.budget_units += units
    spend.approvals.append(
        {"units": units, "by": by, "tick": tick, "budget_units": spend.budget_units}
    )
    del spend.approvals[:-APPROVAL_LIMIT]


def approve(spend: SpendBook, units: int, by: str, tick: int) -> None:
    """Raise the envelope. The only way more money becomes spendable.

    Clears the halt and the notices, because the thresholds are relative to the envelope
    and an operator who has just looked at the numbers is the signal that the previous
    warnings were read.
    """
    if units <= 0:
        return
    spend.budget_units += units
    spend.halted = False
    spend.notices.clear()
    spend.approvals.append({"units": units, "by": by, "tick": tick, "budget_units": spend.budget_units})
    del spend.approvals[:-APPROVAL_LIMIT]


def halt(spend: SpendBook, why: str, tick: int) -> None:
    """Stop everything now. Deliberately not automatic-clearing: only `approve` lifts it,
    so a halt always costs a human a decision."""
    spend.halted = True
    spend.notices.append({"threshold": -1.0, "tick": tick, "reason": why, "halts": True})
    del spend.notices[:-NOTICE_LIMIT]


def status(world) -> dict:
    """Read-only summary for the api, so the monitor can alert on it without reaching
    into world state."""
    spend = book(world)
    if spend is None:
        return {
            "enabled": False,
            "funding_enabled": bool(getattr(world.config, "world_funding_enabled", False)),
            "halted": False,
            "budget_units": 0,
            "spent_units": 0,
        }
    return {
        "enabled": spend.enabled,
        # Whether visitors may *add* to the envelope, which is a different question from
        # whether the world may spend from it. Money arriving is not permission to spend.
        "funding_enabled": bool(getattr(world.config, "world_funding_enabled", False)),
        "halted": spend.halted,
        "budget_units": spend.budget_units,
        "spent_units": spend.spent_units,
        "reserved_units": spend.reserved_units,
        "available_units": available(spend),
        "per_tick_units": spend.per_tick_units,
        "agents_with_balance": sum(1 for v in spend.balances.values() if v > 0),
        # What the monitor reads to decide whether to wake somebody.
        "notices": [dict(n) for n in spend.notices],
        "approvals": len(spend.approvals),
    }
