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


# --- switching it on at all -------------------------------------------------------
#
# The spend book is world state rather than config, so none of the config overrides can
# reach it, and it is created switched off. Everything above this line was unreachable in
# production until these existed: the metering ran, the money arrived, and nothing short
# of editing the database by hand could let the world spend a cent of it.


def _booted(monkeypatch, **env):
    from src.api.server import apply_spend_env

    world = create_world(CONFIG)
    book = world.get(world.first(SpendBook), SpendBook)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return world, book, apply_spend_env(world)


def test_an_operator_can_switch_spending_on(monkeypatch):
    world, book, applied = _booted(monkeypatch, SPEND_ENABLED="true")
    assert book.enabled is True
    assert "enabled=True" in applied


def test_an_operator_can_switch_spending_off_again(monkeypatch):
    """The variable has to be a two-way switch. One that can only turn spending on is a
    control you cannot use to stop."""
    world, book, _ = _booted(monkeypatch, SPEND_ENABLED="true")
    monkeypatch.setenv("SPEND_ENABLED", "false")

    from src.api.server import apply_spend_env

    apply_spend_env(world)
    assert book.enabled is False


def test_nothing_changes_when_the_variable_is_absent(monkeypatch):
    monkeypatch.delenv("SPEND_ENABLED", raising=False)
    world, book, applied = _booted(monkeypatch)
    assert book.enabled is False
    assert applied == []


def test_the_envelope_is_a_floor_not_a_top_up(monkeypatch):
    """Restarts are routine on this world. A variable that added its amount on every boot
    would turn a crash loop into an unbounded budget — the one failure this whole system
    exists to make impossible."""
    world, book, _ = _booted(monkeypatch, SPEND_ENABLED="true", SPEND_BUDGET_UNITS="50000")
    assert book.budget_units == 50_000

    from src.api.server import apply_spend_env

    for _ in range(5):
        apply_spend_env(world)
    assert book.budget_units == 50_000, "a restart topped the envelope up"


def test_spending_already_done_is_never_forgotten(monkeypatch):
    """It only ever raises. Rebuilding the book on boot would hand the world a fresh
    envelope every deploy, which is exactly the accounting the durable counter exists to
    prevent."""
    world, book, _ = _booted(monkeypatch, SPEND_ENABLED="true", SPEND_BUDGET_UNITS="50000")
    governor.commit(book, None, 20_000, tick=1)

    from src.api.server import apply_spend_env

    apply_spend_env(world)
    assert book.spent_units == 20_000
    # 30k was left, so the floor tops it back up to 50k available — never resets it.
    assert governor.available(book) == 50_000


def test_a_nonsense_envelope_is_ignored(monkeypatch):
    """A mistake here is somebody's money."""
    for bad in ("lots", "-5", "1e6"):
        world, book, _ = _booted(monkeypatch, SPEND_ENABLED="true", SPEND_BUDGET_UNITS=bad)
        assert book.budget_units == 0, f"{bad!r} was accepted"


def test_a_per_tick_cap_can_be_set(monkeypatch):
    """The burst control. Without it one tick can spend the whole envelope."""
    world, book, _ = _booted(monkeypatch, SPEND_ENABLED="true", SPEND_PER_TICK_UNITS="3000")
    assert book.per_tick_units == 3000


def test_a_negative_per_tick_cap_is_refused(monkeypatch):
    """Zero means unlimited here, matching every other optional cap in this project — so a
    negative one reads as unlimited too. A minus sign in the wrong place would quietly
    remove the burst control rather than tighten it, which is the opposite of what
    somebody typing a number into this field is trying to do.
    """
    world, book, _ = _booted(monkeypatch, SPEND_ENABLED="true", SPEND_PER_TICK_UNITS="-3000")
    assert book.per_tick_units == 0
    assert governor.tick_headroom(book, tick=1) > 0

    # And the guard is on the value, not on it happening to be the first thing set.
    monkeypatch.setenv("SPEND_PER_TICK_UNITS", "3000")
    from src.api.server import apply_spend_env

    apply_spend_env(world)
    monkeypatch.setenv("SPEND_PER_TICK_UNITS", "-1")
    apply_spend_env(world)
    assert book.per_tick_units == 3000, "a negative wiped a cap that was already set"
