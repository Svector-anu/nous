"""The world paying for its own thinking.

Money arrived and nothing spent it: `budget_units` climbed on the live world while
`spent_units` stayed at zero, because the advisor billed the operator's gateway account
and had never heard of the envelope. Two pots, neither aware of the other.

These cover the join. The advisor asks only while there is budget for the call, and what
it spends lands in the same ledger people fund.
"""

from __future__ import annotations

from src.llm.advisor import ScriptedAdvisor
from src.world import spend as governor
from src.world.components import AdvisorState, Clan, SpendBook
from src.world.config import WorldConfig
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(
    seed=11,
    agent_count=30,
    resource_count=60,
    llm_enabled=True,
    llm_min_ticks_between_calls=1,
    llm_max_calls_per_session=10_000,
    llm_cost_units_per_call=1_000,
)


def _running(config=CONFIG, budget=0, enabled=True):
    world = create_world(config)
    simulation = Simulation(world)
    simulation.run(400)  # let clans form
    book = world.get(world.first(SpendBook), SpendBook)
    book.enabled = enabled
    if budget:
        governor.fund(book, budget, by="operator", tick=world.tick)
    clan_ids = [world.get(e, Clan).clan_id for e in world.query(Clan)]
    world.advisor = ScriptedAdvisor({cid: "expand" for cid in clan_ids}, lag_calls=0)
    return world, simulation, book


def test_thinking_is_charged_to_the_envelope():
    """The join this exists for: what the world spends on a decision lands in the same
    ledger people fund."""
    world, simulation, book = _running(budget=50_000)
    simulation.run(20)

    assert book.spent_units > 0, "the advisor answered and nothing was charged"
    assert book.spent_units % CONFIG.llm_cost_units_per_call == 0


def test_an_empty_envelope_stops_the_asking():
    """Refused means the rules decide, which is what happens for every other reason a
    clan is not asked. The floor, not a degraded mode."""
    world, simulation, book = _running(budget=CONFIG.llm_cost_units_per_call * 2)
    simulation.run(60)

    assert book.spent_units <= book.budget_units
    state = world.get(world.first(AdvisorState), AdvisorState)
    # It stopped asking rather than running the budget into the ground.
    assert state.calls_made <= 3, f"{state.calls_made} calls against a budget for 2"


def test_the_world_still_runs_with_no_budget_at_all():
    world, simulation, book = _running(budget=0)
    simulation.run(40)  # must not raise

    assert book.spent_units == 0
    # Every clan still has a goal — the rules never stopped.
    assert all(world.get(e, Clan).goal for e in world.query(Clan))


def test_nothing_is_metered_until_an_operator_switches_it_on():
    """Every world so far billed the operator's gateway directly and knew nothing about
    the envelope. Enabling this has to be a decision, not a consequence of deploying."""
    world, simulation, book = _running(budget=50_000, enabled=False)
    simulation.run(20)

    state = world.get(world.first(AdvisorState), AdvisorState)
    assert state.calls_made > 0, "the advisor should still work when metering is off"
    assert book.spent_units == 0


def test_a_halt_stops_the_thinking_too():
    """The kill switch has to reach the thing actually spending money.

    The envelope is deliberately far larger than the run can use. Written with a small one
    this passed against a governor with the halt check deleted, because the budget ran dry
    inside the first ten ticks and the world stopped for its own reasons — a green test
    watching the wrong thing stop.
    """
    world, simulation, book = _running(budget=5_000_000)
    simulation.run(10)
    spent_before = book.spent_units
    assert spent_before > 0, "nothing was spending, so nothing could be stopped"
    assert not book.halted, "the envelope halted on its own; this proves nothing"

    governor.halt(book, "operator stopped it", tick=world.tick)
    simulation.run(40)

    # A halt stops the asking. Requests already in flight still land and are still
    # charged, because that money genuinely left — so the bill can rise by at most the one
    # round of calls that was already out the door, once.
    clans = sum(1 for _ in world.query(Clan))
    settled = book.spent_units
    assert settled <= spent_before + clans * CONFIG.llm_cost_units_per_call

    simulation.run(40)
    assert book.spent_units == settled, "spending resumed while halted"


def test_reservations_do_not_leak_when_an_advisor_never_answers():
    """A reservation is released when its decision lands. An advisor that times out
    returns nothing at all, so the world never learns the call is dead — and every one of
    them would sit there holding budget nobody can ever spend. Left alone this starves the
    world of its own money without a single thing going wrong loudly: over sixty ticks it
    holds thirty times what could possibly be in flight.
    """

    class Lost(ScriptedAdvisor):
        """Accepts every request and is never heard from again."""

        def collect(self):
            return []

        def inflight_clans(self):
            return set()

    world, simulation, book = _running(budget=5_000_000)
    clan_ids = [world.get(e, Clan).clan_id for e in world.query(Clan)]
    world.advisor = Lost({cid: "expand" for cid in clan_ids}, lag_calls=0)

    simulation.run(60)
    held = book.reserved_units
    ceiling = len(clan_ids) * CONFIG.llm_cost_units_per_call
    assert held <= ceiling, f"holding {held}, more than every clan at once ({ceiling})"

    # And it is a ceiling, not a slower climb.
    simulation.run(60)
    assert book.reserved_units <= ceiling


def test_spending_survives_a_restart(tmp_path):
    """`spent_units` is world state. A world that forgot what it had spent would hand
    itself a fresh envelope on every deploy."""
    from src.persistence.sqlite_store import SqliteWorldStore

    world, simulation, book = _running(budget=50_000)
    simulation.run(20)
    spent = book.spent_units
    assert spent > 0

    store = SqliteWorldStore(tmp_path / "w.db")
    store.save(world)
    after = store.load()
    assert after.get(after.first(SpendBook), SpendBook).spent_units == spent
