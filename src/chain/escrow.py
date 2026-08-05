"""Real-money settlement intent, derived from already-settled world state.

Gated by `real_money_enabled`, which is False by default. With it off, every function
here is a no-op and the demo credit markets carry on untouched — the two books never
share a balance, and a demo credit never becomes money.

**This never sends value.** It records an *intent*: "this address is owed this much,
from this market, resolved at this tick". An operator's settlement job reads those
entries, pays them, and marks them paid. A transfer inside the tick loop would put the
network on the critical path of history, which is the one thing the whole architecture
is arranged to prevent.

Payouts are derived from `MarketBook` entries that have already resolved, so settlement
cannot influence the outcome it settles. Same property the demo markets rest on.
"""

from __future__ import annotations

import logging

from ..world.components import EscrowBook, MarketBook
from ..world.ecs import World
from ..world.systems import markets

logger = logging.getLogger("neociv")

PENDING = "pending"
PAID = "paid"

# Bounded, like every accumulator in this project.
PAYOUT_LIMIT = 256


def book(world: World) -> EscrowBook | None:
    entity = world.first(EscrowBook)
    return world.get(entity, EscrowBook) if entity is not None else None


def enabled(world: World) -> bool:
    return bool(getattr(world.config, "real_money_enabled", False))


def deposit(world: World, address: str, amount: int) -> int:
    """Credit a verified deposit to an address. Returns the new balance.

    The caller verifies the transaction on chain first; this only records the result,
    which is the same boundary every other outside-the-sim path here respects.
    """
    if not enabled(world):
        raise ValueError("real money is not enabled")
    if amount <= 0:
        raise ValueError("deposit must be positive")

    escrow = book(world)
    if escrow is None:
        raise ValueError("world has no escrow book")
    key = (address or "").strip().lower()
    if not key:
        raise ValueError("deposit needs an address")
    escrow.deposits[key] = escrow.deposits.get(key, 0) + int(amount)
    return escrow.deposits[key]


def balance(world: World, address: str) -> int:
    escrow = book(world)
    if escrow is None:
        return 0
    return escrow.deposits.get((address or "").strip().lower(), 0)


def record_payouts(world: World, resolved_market_id: int) -> list[dict]:
    """Record payout intent for one resolved market. Returns the entries added.

    Idempotent: a market already recorded produces nothing, so a settlement job that
    runs twice does not owe anybody twice.
    """
    if not enabled(world):
        return []

    escrow = book(world)
    market_book = markets.book(world)
    if escrow is None or market_book is None:
        return []

    entry = next((m for m in market_book.markets if m["id"] == resolved_market_id), None)
    if entry is None or entry["state"] != markets.RESOLVED:
        return []
    if any(p["market_id"] == resolved_market_id for p in escrow.payouts):
        return []

    outcome = entry.get("outcome")
    added: list[dict] = []
    # Sorted so a reloaded world records identically — the same ascending tie-break the
    # rest of the codebase uses.
    for user in sorted(entry.get("positions", {})):
        held = entry["positions"][user]
        won = held.get(outcome, 0) if outcome in (markets.YES, markets.NO) else 0
        if won <= 0:
            continue
        added.append(
            {
                "id": escrow.next_id,
                "market_id": resolved_market_id,
                "user": user,
                "amount": int(won),
                "outcome": outcome,
                "resolved_tick": entry.get("resolved_tick", world.tick),
                "state": PENDING,
                "tx_hash": "",
            }
        )
        escrow.next_id += 1

    escrow.payouts.extend(added)
    # Oldest paid entries drop first; anything still pending is always kept.
    if len(escrow.payouts) > PAYOUT_LIMIT:
        paid = [p for p in escrow.payouts if p["state"] == PAID]
        drop = {id(p) for p in paid[: len(escrow.payouts) - PAYOUT_LIMIT]}
        escrow.payouts = [p for p in escrow.payouts if id(p) not in drop]
    return added


def mark_paid(world: World, payout_id: int, tx_hash: str) -> bool:
    """An operator's settlement job reports a completed transfer."""
    if not enabled(world):
        return False
    escrow = book(world)
    if escrow is None:
        return False
    for payout in escrow.payouts:
        if payout["id"] == payout_id and payout["state"] == PENDING:
            payout["state"] = PAID
            payout["tx_hash"] = (tx_hash or "").strip()
            return True
    return False


def pending_payouts(world: World) -> list[dict]:
    escrow = book(world)
    if escrow is None:
        return []
    return [p for p in escrow.payouts if p["state"] == PENDING]
