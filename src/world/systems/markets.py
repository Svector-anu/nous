"""Prediction markets on simulation events.

Spectators bet on what the world will do. This system reads agent and clan state and never
writes to it — a market cannot change the outcome it is betting on, which is the whole
reason it is safe to add to a living simulation. `tests/test_markets.py` pins that by
running two worlds side by side and asserting the agents and clans are identical.

Runs last in the tick, after every system that can change the world, so a resolution is
settled against the tick's final state rather than a half-applied one.

Determinism, the same rules the rest of the sim follows:

- a bet is an input, queued by the api and drained here at a fixed point
- subjects are drawn from the tick rng, never from wall clock or Math.random
- payouts are integer parimutuel; the division remainder goes to the lowest user id, the
  same ascending-id tie-break the rest of the codebase uses
- every dict is iterated in sorted key order, because a reloaded world must iterate
  identically

A market resolves from a fact about the world, recorded as `evidence`. That field exists so
settlement is auditable and so this can later be handed to external infrastructure without
re-parsing the english in `question`.
"""

from __future__ import annotations

from ..components import Agent, Clan, MarketBook
from ..ecs import World
from ..rng import TickRng

# Market lifecycle.
OPEN = "open"
LOCKED = "locked"
RESOLVED = "resolved"

YES = "yes"
NO = "no"
VOID = "void"

KINDS = (
    "clan_survives",
    "population_below",
    "truce_holds",
    "clan_raided",
    "agent_survives",
)


def book(world: World) -> MarketBook | None:
    entity = world.first(MarketBook)
    return world.get(entity, MarketBook) if entity is not None else None


# --- reading the world -------------------------------------------------------


def _clans(world: World) -> dict[int, Clan]:
    return {world.get(e, Clan).clan_id: world.get(e, Clan) for e in world.query(Clan)}


def _clan_alive(world: World, clan_id: int) -> bool:
    clan = _clans(world).get(clan_id)
    return clan is not None and len(clan.members) > 0


def _population(world: World) -> int:
    return len(world.query(Agent))


def _raids_suffered(clan: Clan) -> int:
    """Total times this clan has been robbed, across every attacker.

    Grudges are recorded on theft rather than on violence, so this counts raids that
    actually took food — which is what a spectator would call a raid.
    """
    return sum(int(entry[0]) for entry in clan.grudges.values())


# --- resolution --------------------------------------------------------------
#
# Each resolver returns (outcome, evidence) or None if the question cannot be answered yet.
# Returning an outcome before `resolves_tick` is how a market settles early.


def _resolve_clan_survives(world: World, market: dict, final: bool):
    clan_id = market["subject"]["clan_id"]
    alive = _clan_alive(world, clan_id)
    if not alive:
        # Death is permanent: a clan with no members never comes back, so this is
        # answerable the moment it happens rather than at the horizon.
        return NO, {"clan_id": clan_id, "members": 0}
    if final:
        clan = _clans(world)[clan_id]
        return YES, {"clan_id": clan_id, "members": len(clan.members)}
    return None


def _resolve_population_below(world: World, market: dict, final: bool):
    threshold = market["subject"]["threshold"]
    population = _population(world)
    if population < threshold:
        return YES, {"population": population, "threshold": threshold}
    if final:
        return NO, {"population": population, "threshold": threshold}
    return None


def _resolve_truce_holds(world: World, market: dict, final: bool):
    a = market["subject"]["clan_a"]
    b = market["subject"]["clan_b"]
    clans = _clans(world)
    # If either clan is gone the question is meaningless rather than false — nobody broke
    # the truce. Void refunds every stake; leaving it to resolve NO would punish a
    # position that was never wrong.
    if a not in clans or b not in clans or not clans[a].members or not clans[b].members:
        return VOID, {"reason": "a clan in the truce no longer exists"}
    if b not in clans[a].allies:
        return NO, {"clan_a": a, "clan_b": b, "allies": list(clans[a].allies)}
    if final:
        return YES, {"clan_a": a, "clan_b": b, "allies": list(clans[a].allies)}
    return None


def _resolve_clan_raided(world: World, market: dict, final: bool):
    clan_id = market["subject"]["clan_id"]
    baseline = market["subject"]["raids_at_open"]
    clans = _clans(world)
    if clan_id not in clans or not clans[clan_id].members:
        return VOID, {"reason": "the clan no longer exists"}
    suffered = _raids_suffered(clans[clan_id])
    if suffered > baseline:
        return YES, {"clan_id": clan_id, "raids": suffered, "at_open": baseline}
    if final:
        return NO, {"clan_id": clan_id, "raids": suffered, "at_open": baseline}
    return None


def _resolve_agent_survives(world: World, market: dict, final: bool):
    entity = market["subject"]["agent"]
    alive = world.try_get(entity, Agent) is not None
    if not alive:
        return NO, {"agent": entity, "alive": False}
    if final:
        return YES, {"agent": entity, "alive": True}
    return None


RESOLVERS = {
    "clan_survives": _resolve_clan_survives,
    "population_below": _resolve_population_below,
    "truce_holds": _resolve_truce_holds,
    "clan_raided": _resolve_clan_raided,
    "agent_survives": _resolve_agent_survives,
}


# --- opening markets ---------------------------------------------------------


def _new_market(world: World, kind: str, subject: dict, question: str, trigger: str) -> dict:
    config = world.config
    market = book(world)
    market_id = market.next_id
    market.next_id += 1
    return {
        "id": market_id,
        "kind": kind,
        "subject": subject,
        "question": question,
        "trigger": trigger,
        "opened_tick": world.tick,
        "locks_tick": world.tick + config.market_horizon_ticks - config.market_lock_before_ticks,
        "resolves_tick": world.tick + config.market_horizon_ticks,
        "state": OPEN,
        "outcome": None,
        "pool": {YES: 0, NO: 0},
        "positions": {},
        "resolved_tick": None,
        "evidence": None,
    }


def _propose_scheduled(world: World, rng: TickRng) -> dict | None:
    """The floor: a quiet world still needs something to bet on.

    Subject choice comes from the tick rng, so the same seed opens the same markets.
    """
    clans = sorted(_clans(world).items())
    live = [(cid, clan) for cid, clan in clans if clan.members]
    population = _population(world)

    choices = []
    if live:
        choices.append("clan_survives")
        choices.append("clan_raided")
        choices.append("agent_survives")
    if population > 0:
        choices.append("population_below")
    if not choices:
        return None

    kind = choices[rng.below(len(choices))]
    horizon = world.config.market_horizon_ticks

    if kind == "clan_survives":
        clan_id, _ = live[rng.below(len(live))]
        return _new_market(
            world, kind, {"clan_id": clan_id},
            f"will clan {clan_id} still exist at tick {world.tick + horizon}?", "scheduled",
        )
    if kind == "clan_raided":
        clan_id, clan = live[rng.below(len(live))]
        return _new_market(
            world, kind, {"clan_id": clan_id, "raids_at_open": _raids_suffered(clan)},
            f"will clan {clan_id} be raided before tick {world.tick + horizon}?", "scheduled",
        )
    if kind == "agent_survives":
        clan_id, clan = live[rng.below(len(live))]
        entity = clan.members[rng.below(len(clan.members))]
        agent = world.try_get(entity, Agent)
        name = agent.name if agent is not None else f"agent {entity}"
        return _new_market(
            world, kind, {"agent": entity, "name": name},
            f"will {name} still be alive at tick {world.tick + horizon}?", "scheduled",
        )
    # population_below: a bar just under the current count, so the question is live rather
    # than a foregone conclusion in either direction.
    threshold = max(1, population - max(2, population // 20))
    return _new_market(
        world, kind, {"threshold": threshold},
        f"will population drop below {threshold} by tick {world.tick + horizon}?", "scheduled",
    )


def _propose_events(world: World, previous: dict) -> list[dict]:
    """Markets that open because something interesting just happened.

    `previous` is the small summary this system kept from last tick — markets may not read
    any other system's private state, and diffing two snapshots is how the viewer's
    director finds events too.
    """
    proposals = []
    horizon = world.config.market_horizon_ticks
    clans = _clans(world)

    for clan_id, clan in sorted(clans.items()):
        if not clan.members:
            continue
        was = previous.get(clan_id)
        if was is None:
            continue

        # A truce just formed: will it hold?
        for ally in clan.allies:
            if ally in was["allies"] or ally <= clan_id:
                continue  # record the pair once, from the lower id
            proposals.append(_new_market(
                world, "truce_holds", {"clan_a": clan_id, "clan_b": ally},
                f"will the truce between clan {clan_id} and clan {ally} hold "
                f"to tick {world.tick + horizon}?", "truce_formed",
            ))

        # A clan just took a beating: does it survive it?
        suffered = _raids_suffered(clan)
        if suffered - was["raids"] >= 2:
            proposals.append(_new_market(
                world, "clan_survives", {"clan_id": clan_id},
                f"clan {clan_id} was raided hard — will it still exist at "
                f"tick {world.tick + horizon}?", "raid_wave",
            ))

    return proposals


def _summarise(world: World) -> dict:
    return {
        clan_id: {"allies": list(clan.allies), "raids": _raids_suffered(clan)}
        for clan_id, clan in _clans(world).items()
    }


# --- positions and payout ----------------------------------------------------


def place(world: World, user: str, market_id: int, side: str, stake: int) -> None:
    """Queue a position. Validation belongs to the caller; this only records intent."""
    market = book(world)
    if market is None:
        raise ValueError("world has no market book")
    market.pending.append(
        {"user": user, "market_id": market_id, "side": side, "stake": stake,
         "requested_tick": world.tick}
    )


def _apply_pending(world: World, market: MarketBook) -> None:
    by_id = {m["id"]: m for m in market.markets}
    for request in market.pending:
        entry = by_id.get(request["market_id"])
        if entry is None or entry["state"] != OPEN:
            continue
        user = request["user"]
        side = request["side"]
        if side not in (YES, NO):
            continue
        stake = int(request["stake"])
        if stake <= 0 or stake > world.config.market_max_stake:
            continue
        balance = market.balances.setdefault(user, world.config.market_starting_balance)
        if balance < stake:
            continue
        market.balances[user] = balance - stake
        entry["pool"][side] += stake
        held = entry["positions"].setdefault(user, {YES: 0, NO: 0})
        held[side] += stake
    market.pending.clear()


def _pay_out(market: MarketBook, entry: dict, outcome: str) -> None:
    """Integer parimutuel. Winners split the whole pool in proportion to their stake.

    Floor division leaves a remainder; it goes to the lowest user id, which is the same
    ascending-id tie-break the rest of the simulation uses so that a reloaded world settles
    identically. A void, or a market nobody took the other side of, refunds every stake —
    with no counterparty there is nothing to win.
    """
    winners = sorted(
        user for user, held in entry["positions"].items()
        if outcome in (YES, NO) and held[outcome] > 0
    )
    total_pool = entry["pool"][YES] + entry["pool"][NO]
    winning_stake = entry["pool"][outcome] if outcome in (YES, NO) else 0

    if outcome == VOID or winning_stake == 0 or not winners:
        for user in sorted(entry["positions"]):
            held = entry["positions"][user]
            market.balances[user] = market.balances.get(user, 0) + held[YES] + held[NO]
        return

    paid = 0
    for user in winners:
        share = entry["positions"][user][outcome] * total_pool // winning_stake
        market.balances[user] = market.balances.get(user, 0) + share
        paid += share
    remainder = total_pool - paid
    if remainder:
        market.balances[winners[0]] = market.balances.get(winners[0], 0) + remainder


# --- the tick ----------------------------------------------------------------


def run(world: World, rng: TickRng) -> None:
    if not getattr(world.config, "markets_enabled", False):
        return
    market = book(world)
    if market is None:
        return

    config = world.config
    _apply_pending(world, market)

    for entry in market.markets:
        if entry["state"] == OPEN and world.tick >= entry["locks_tick"]:
            entry["state"] = LOCKED

    for entry in market.markets:
        if entry["state"] == RESOLVED:
            continue
        resolver = RESOLVERS.get(entry["kind"])
        if resolver is None:
            continue
        final = world.tick >= entry["resolves_tick"]
        settled = resolver(world, entry, final)
        if settled is None:
            continue
        outcome, evidence = settled
        entry["state"] = RESOLVED
        entry["outcome"] = outcome
        entry["evidence"] = evidence
        entry["resolved_tick"] = world.tick
        _pay_out(market, entry, outcome)

    previous = getattr(world, "_market_summary", None)
    summary = _summarise(world)
    open_count = sum(1 for e in market.markets if e["state"] != RESOLVED)

    proposals: list[dict] = []
    if previous is not None:
        proposals.extend(_propose_events(world, previous))
    world._market_summary = summary

    scheduled_due = world.tick - market.last_scheduled_tick >= config.market_open_every_ticks
    if scheduled_due:
        market.last_scheduled_tick = world.tick
        candidate = _propose_scheduled(world, rng)
        if candidate is not None:
            proposals.append(candidate)

    for candidate in proposals:
        if open_count >= config.market_max_open:
            # next_id was already consumed; that is fine, ids need only be unique.
            break
        market.markets.append(candidate)
        open_count += 1

    # Bounded, like every other accumulator here. Settled markets are dropped oldest first;
    # anything unresolved is always kept.
    if len(market.markets) > config.market_history_limit:
        resolved = [e for e in market.markets if e["state"] == RESOLVED]
        keep = len(market.markets) - config.market_history_limit
        drop = {id(e) for e in resolved[:keep]}
        market.markets = [e for e in market.markets if id(e) not in drop]
