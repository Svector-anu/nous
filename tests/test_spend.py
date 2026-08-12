"""The governor that stands between an agent and real money.

Everything here is about refusing. A ledger that adds up correctly but lets a runaway
drain an envelope is worse than no ledger, because it looks like a control.
"""

from __future__ import annotations

from src.world import spend as governor
from src.world.components import SpendBook


def _book(**kwargs) -> SpendBook:
    base = {"enabled": True, "budget_units": 1000, "per_tick_units": 0}
    base.update(kwargs)
    book = SpendBook(**base)
    return book


# --- the three brakes ---------------------------------------------------------------


def test_nothing_spends_until_an_operator_turns_it_on():
    """A deploy must never begin spending by itself."""
    book = SpendBook(budget_units=1000)
    governor.credit(book, 1, 500)
    assert governor.refuse(book, 1, 10, tick=1) == "spending is not enabled"


def test_a_halt_beats_any_amount_of_headroom():
    """Checked before the budget on purpose: a kill switch that a large envelope can
    argue with is not a kill switch."""
    book = _book(budget_units=10**9)
    governor.credit(book, 1, 10**9)
    governor.halt(book, "operator stopped it", tick=5)
    assert "halted" in governor.refuse(book, 1, 1, tick=6)


def test_spending_stops_at_the_envelope():
    book = _book(budget_units=100)
    governor.credit(book, 1, 10_000)
    assert governor.refuse(book, 1, 100, tick=1) == ""
    assert "over budget" in governor.refuse(book, 1, 101, tick=1)


def test_the_per_tick_cap_stops_a_burst():
    """Without it a single tick drains the envelope before any notice is read, which
    makes the envelope decorative."""
    book = _book(budget_units=10_000, per_tick_units=100)
    governor.credit(book, 1, 10_000)

    assert governor.reserve(book, 1, 60, tick=7) == ""
    governor.commit(book, 1, 60, tick=7)
    assert "per-tick" in governor.refuse(book, 1, 60, tick=7)
    # A new tick restores the allowance without needing a scheduler.
    assert governor.refuse(book, 1, 60, tick=8) == ""


def test_an_agent_cannot_spend_what_it_has_not_earned():
    book = _book()
    governor.credit(book, 1, 5)
    assert "has not earned" in governor.refuse(book, 1, 6, tick=1)


# --- reservations -------------------------------------------------------------------


def test_two_concurrent_spends_cannot_share_the_same_headroom():
    """Settlement is a network round trip. Anything that checks a limit before awaiting
    and commits after it lets everything arriving in between through — one payment bought
    five clan decisions that way, and here it would be one envelope buying several times
    its own size."""
    book = _book(budget_units=100)
    governor.credit(book, 1, 1000)

    assert governor.reserve(book, 1, 80, tick=1) == ""
    # The second request arrives while the first is still in flight.
    assert "over budget" in governor.reserve(book, 1, 80, tick=1)


def test_a_failed_call_gives_its_reservation_back():
    """A call that bought nothing must not hold the envelope hostage."""
    book = _book(budget_units=100)
    governor.credit(book, 1, 1000)

    governor.reserve(book, 1, 80, tick=1)
    governor.release(book, 80)
    assert governor.available(book) == 100
    assert governor.reserve(book, 1, 80, tick=1) == ""


def test_committing_clears_the_reservation_rather_than_double_counting():
    book = _book(budget_units=100)
    governor.credit(book, 1, 1000)
    governor.reserve(book, 1, 40, tick=1)
    governor.commit(book, 1, 40, tick=1)

    assert book.reserved_units == 0
    assert book.spent_units == 40
    assert governor.available(book) == 60


# --- notices ------------------------------------------------------------------------


def test_crossing_a_threshold_raises_a_notice():
    """A cap tells you how much you can lose. A notice tells you that you are losing it,
    which is the part that was missing when an advisor died quietly for two days."""
    book = _book(budget_units=100)
    governor.credit(book, 1, 1000)
    governor.reserve(book, 1, 50, tick=1)
    fresh = governor.commit(book, 1, 50, tick=1)

    assert [n["threshold"] for n in fresh] == [0.5]


def test_a_threshold_is_announced_once_not_every_tick():
    book = _book(budget_units=100)
    governor.credit(book, 1, 1000)
    for tick in range(1, 6):
        governor.reserve(book, 1, 10, tick=tick)
        governor.commit(book, 1, 10, tick=tick)

    crossings = [n["threshold"] for n in book.notices]
    assert crossings == sorted(set(crossings)), f"repeated notices: {crossings}"


def test_exhausting_the_envelope_halts_rather_than_slowing_down():
    """It does not roll over and it does not quietly keep going. Somebody has to look."""
    book = _book(budget_units=100)
    governor.credit(book, 1, 1000)
    governor.reserve(book, 1, 100, tick=1)
    fresh = governor.commit(book, 1, 100, tick=1)

    assert book.halted is True
    assert any(n["halts"] for n in fresh)
    assert "halted" in governor.refuse(book, 1, 1, tick=2)


# --- approval -----------------------------------------------------------------------


def test_only_an_approval_lifts_a_halt():
    book = _book(budget_units=100)
    governor.credit(book, 1, 1000)
    governor.reserve(book, 1, 100, tick=1)
    governor.commit(book, 1, 100, tick=1)
    assert book.halted is True

    governor.approve(book, 100, by="operator", tick=2)
    assert book.halted is False
    assert governor.refuse(book, 1, 50, tick=2) == ""


def test_an_approval_is_recorded_for_the_audit_trail():
    book = _book(budget_units=0)
    governor.approve(book, 250, by="anu", tick=9)

    assert book.approvals[-1]["units"] == 250
    assert book.approvals[-1]["by"] == "anu"
    assert book.budget_units == 250


def test_time_passing_does_not_refill_anything():
    """The envelope is approved, never topped up. A runaway costs exactly one envelope."""
    book = _book(budget_units=50)
    governor.credit(book, 1, 1000)
    governor.reserve(book, 1, 50, tick=1)
    governor.commit(book, 1, 50, tick=1)

    for tick in range(2, 200):
        assert governor.refuse(book, 1, 1, tick=tick) != ""


# --- bounded ------------------------------------------------------------------------


def test_the_ledgers_stay_bounded():
    """Every unbounded accumulator in this project has eventually eaten the simulation."""
    book = _book(budget_units=10**9, per_tick_units=0)
    governor.credit(book, 1, 10**9)
    for tick in range(governor.SPEND_LIMIT + 60):
        governor.reserve(book, 1, 1, tick=tick)
        governor.commit(book, 1, 1, tick=tick)
    for index in range(governor.APPROVAL_LIMIT + 20):
        governor.approve(book, 1, by="operator", tick=index)

    assert len(book.spends) <= governor.SPEND_LIMIT
    assert len(book.approvals) <= governor.APPROVAL_LIMIT
    assert len(book.notices) <= governor.NOTICE_LIMIT


def test_a_balance_survives_a_save_and_reload(tmp_path):
    """json has no integer keys. Keyed by int, a balance written under 1 came back under
    "1", every lookup by agent id missed, and every agent's earnings silently read as
    zero after a restart — the governor refusing spends for money that was really there,
    with no error anywhere."""
    from src.persistence.sqlite_store import SqliteWorldStore
    from src.world.config import WorldConfig
    from src.world.tick import create_world

    world = create_world(WorldConfig(agent_count=4, resource_count=8))
    book = world.get(world.first(SpendBook), SpendBook)
    book.enabled = True
    governor.approve(book, 5000, by="anu", tick=0)
    governor.credit(book, 1, 900)

    store = SqliteWorldStore(tmp_path / "w.db")
    store.save(world)
    reloaded = store.load()
    after = reloaded.get(reloaded.first(SpendBook), SpendBook)

    # The lookup the governor actually performs, with the id the world actually holds.
    assert governor.refuse(after, 1, 900, tick=1) == ""
    assert after.budget_units == 5000


def test_a_world_saved_before_the_component_existed_gets_one(tmp_path):
    """A world saved before a component existed has no entity carrying it, and nothing in
    the load path creates one — so the feature is silently unreachable on exactly the
    worlds that have been running longest. The live world is one of those."""
    from src.persistence.sqlite_store import SqliteWorldStore
    from src.world.config import WorldConfig
    from src.world.tick import create_world, ensure_singletons

    world = create_world(WorldConfig(agent_count=4, resource_count=8))
    world.remove(world.first(SpendBook), SpendBook)
    store = SqliteWorldStore(tmp_path / "old.db")
    store.save(world)

    resumed = store.load()
    assert resumed.first(SpendBook) is None, "the fixture did not reproduce an old world"

    added = ensure_singletons(resumed)
    assert "SpendBook" in added
    assert resumed.first(SpendBook) is not None
    # Off and unfunded, exactly like a fresh world. Backfilling must not enable anything.
    assert governor.status(resumed)["enabled"] is False
    assert governor.status(resumed)["budget_units"] == 0


def test_backfilling_twice_changes_nothing(tmp_path):
    """Idempotent: a resumed world keeps its history and a fresh one is untouched."""
    from src.world.config import WorldConfig
    from src.world.tick import create_world, ensure_singletons

    world = create_world(WorldConfig(agent_count=4, resource_count=8))
    book = world.get(world.first(SpendBook), SpendBook)
    governor.approve(book, 900, by="anu", tick=0)

    assert ensure_singletons(world) == []
    assert world.get(world.first(SpendBook), SpendBook).budget_units == 900
