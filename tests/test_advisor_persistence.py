"""Advisor spend and in-flight requests are durable world state.

Two bugs this locks down, both found by adversarial review of the first cut:

1. `calls_made` lived on the in-memory advisor, so a restart handed the world a fresh
   session budget. A crash loop could spend without limit.
2. An in-flight request vanished on reload while the clan's `last_advisor_tick` persisted,
   so the request was never answered *and* the clan was not eligible to be asked again.
"""

from __future__ import annotations

from src.llm.advisor import ScriptedAdvisor
from src.persistence.sqlite_store import SqliteWorldStore
from src.world.components import AdvisorState, Clan, ClanGoal
from src.world.config import WorldConfig
from src.world.systems.leadership import advisor_state
from src.world.tick import Simulation, create_world, state_hash

CONFIG = WorldConfig(
    seed=88,
    grid_width=32,
    grid_height=32,
    agent_count=30,
    resource_count=90,
    llm_min_ticks_between_calls=40,
    llm_max_calls_per_session=6,
)


def _clan_ids(world) -> list[int]:
    return sorted(world.get(e, Clan).clan_id for e in world.query(Clan))


def _settled(advisor=None, config: WorldConfig = CONFIG, ticks: int = 300) -> Simulation:
    simulation = Simulation(create_world(config))
    simulation.run(ticks)
    simulation.world.advisor = advisor
    return simulation


# --- the counters exist and are durable -------------------------------------


def test_genesis_creates_advisor_state():
    world = create_world(CONFIG)
    state = advisor_state(world)
    assert state is not None
    assert state.calls_made == 0
    assert state.pending == []


def test_spend_is_recorded_in_the_world():
    simulation = _settled()
    world = simulation.world
    world.advisor = ScriptedAdvisor({cid: "expand" for cid in _clan_ids(world)}, lag_calls=0)

    simulation.run(60)

    assert advisor_state(world).calls_made > 0


def test_spend_survives_save_and_reload(tmp_path):
    store = SqliteWorldStore(tmp_path / "spend.db")
    simulation = _settled()
    world = simulation.world
    world.advisor = ScriptedAdvisor({cid: "expand" for cid in _clan_ids(world)}, lag_calls=0)
    simulation.run(60)

    spent = advisor_state(world).calls_made
    assert spent > 0
    store.save(world)
    store.close()

    reloaded = SqliteWorldStore(tmp_path / "spend.db").load()
    assert advisor_state(reloaded).calls_made == spent, "a restart handed out a fresh budget"


def test_a_restart_cannot_exceed_the_session_budget(tmp_path):
    """The bug in plain terms: restart in a loop, spend without limit."""
    budget = 4
    config = WorldConfig(
        seed=88, grid_width=32, grid_height=32, agent_count=30, resource_count=90,
        llm_min_ticks_between_calls=10, llm_max_calls_per_session=budget,
    )
    path = tmp_path / "loop.db"

    simulation = _settled(config=config)
    world = simulation.world
    world.advisor = ScriptedAdvisor({cid: "expand" for cid in _clan_ids(world)}, lag_calls=0)
    simulation.run(40)
    SqliteWorldStore(path).save(world)

    # Five restarts, each running long enough to spend freely if the budget reset.
    for _ in range(5):
        store = SqliteWorldStore(path)
        reloaded = store.load()
        reloaded.advisor = ScriptedAdvisor(
            {cid: "expand" for cid in _clan_ids(reloaded)}, lag_calls=0
        )
        resumed = Simulation(reloaded)
        resumed.run(60)
        store.save(reloaded)
        store.close()

    assert advisor_state(reloaded).calls_made <= budget


# --- in-flight requests are not silently dropped ----------------------------


def test_in_flight_requests_are_recorded_as_pending():
    simulation = _settled()
    world = simulation.world
    # lag high enough that requests stay outstanding
    world.advisor = ScriptedAdvisor({cid: "expand" for cid in _clan_ids(world)}, lag_calls=500)

    simulation.run(60)

    state = advisor_state(world)
    assert state.pending, "an outstanding request left no durable trace"
    assert state.pending[0]["attempts"] == 1


def test_pending_clears_once_the_decision_arrives():
    simulation = _settled()
    world = simulation.world
    world.advisor = ScriptedAdvisor({cid: "expand" for cid in _clan_ids(world)}, lag_calls=0)

    simulation.run(60)

    assert advisor_state(world).pending == []


def test_in_flight_request_is_resent_after_a_reload(tmp_path):
    """The core of the finding: a request in flight at save time must not be lost.

    Budget deliberately has headroom — with none left there is nothing to retry *with*,
    and refusing is the correct answer (see the no-budget path below).
    """
    config = WorldConfig(
        seed=88, grid_width=32, grid_height=32, agent_count=30, resource_count=90,
        llm_min_ticks_between_calls=40, llm_max_calls_per_session=100,
    )
    store = SqliteWorldStore(tmp_path / "inflight.db")
    simulation = _settled(config=config)
    world = simulation.world
    world.advisor = ScriptedAdvisor({cid: "expand" for cid in _clan_ids(world)}, lag_calls=500)
    simulation.run(60)

    pending_before = [dict(e) for e in advisor_state(world).pending]
    spent_before = advisor_state(world).calls_made
    assert pending_before, "test did not manage to leave a request in flight"
    store.save(world)
    store.close()

    reloaded = SqliteWorldStore(tmp_path / "inflight.db").load()
    state = advisor_state(reloaded)
    assert [e["clan_id"] for e in state.pending] == [e["clan_id"] for e in pending_before]
    assert state.calls_made == spent_before

    # A fresh process: the advisor has nothing in flight, so recovery must re-send.
    fresh = ScriptedAdvisor({cid: "expand" for cid in _clan_ids(reloaded)}, lag_calls=0)
    reloaded.advisor = fresh
    resumed = Simulation(reloaded)
    resumed.step()

    assert fresh.calls > 0, "the in-flight request was silently dropped on reload"
    assert advisor_state(reloaded).calls_made > spent_before, "a retry must be counted"


def test_recovery_refuses_when_the_budget_is_spent(tmp_path):
    """You cannot retry with money you do not have. The request is abandoned and the
    rules carry the clan — the one thing that must not happen is spending past the cap."""
    config = WorldConfig(
        seed=88, grid_width=32, grid_height=32, agent_count=30, resource_count=90,
        llm_min_ticks_between_calls=40, llm_max_calls_per_session=2,
    )
    store = SqliteWorldStore(tmp_path / "broke.db")
    simulation = _settled(config=config)
    world = simulation.world
    world.advisor = ScriptedAdvisor({cid: "expand" for cid in _clan_ids(world)}, lag_calls=500)
    simulation.run(60)
    spent = advisor_state(world).calls_made
    store.save(world)
    store.close()

    reloaded = SqliteWorldStore(tmp_path / "broke.db").load()
    fresh = ScriptedAdvisor({cid: "expand" for cid in _clan_ids(reloaded)}, lag_calls=0)
    reloaded.advisor = fresh
    Simulation(reloaded).step()

    assert advisor_state(reloaded).calls_made == spent <= config.llm_max_calls_per_session
    for clan_entity in reloaded.query(Clan):
        assert reloaded.get(clan_entity, Clan).goal in {g.value for g in ClanGoal}


def test_recovery_is_bounded_by_max_attempts(tmp_path):
    """A crash loop must not retry the same request forever."""
    config = WorldConfig(
        seed=88, grid_width=32, grid_height=32, agent_count=30, resource_count=90,
        llm_min_ticks_between_calls=40, llm_max_calls_per_session=100,
        llm_max_recovery_attempts=2,
    )
    path = tmp_path / "attempts.db"
    simulation = _settled(config=config)
    world = simulation.world
    world.advisor = ScriptedAdvisor({cid: "expand" for cid in _clan_ids(world)}, lag_calls=500)
    simulation.run(60)
    SqliteWorldStore(path).save(world)

    for _ in range(6):
        store = SqliteWorldStore(path)
        reloaded = store.load()
        reloaded.advisor = ScriptedAdvisor(
            {cid: "expand" for cid in _clan_ids(reloaded)}, lag_calls=500
        )
        Simulation(reloaded).step()
        store.save(reloaded)
        store.close()

    for entry in advisor_state(reloaded).pending:
        assert entry["attempts"] <= config.llm_max_recovery_attempts


def test_orphaned_request_for_a_dead_clan_is_dropped():
    simulation = _settled()
    world = simulation.world
    state = advisor_state(world)
    state.pending = [{"clan_id": 9999, "tick": 0, "attempts": 1}]
    world.advisor = ScriptedAdvisor({}, lag_calls=0)

    simulation.step()

    assert state.pending == [], "a request for a clan that no longer exists must be dropped"


# --- identical to a world that never reloaded -------------------------------


def test_reload_with_nothing_in_flight_is_byte_identical(tmp_path):
    """With no request outstanding there is nothing to recover, so a reloaded world must
    be indistinguishable from one that ran straight through."""
    answers = None

    def build(sim):
        nonlocal answers
        answers = {cid: "expand" for cid in _clan_ids(sim.world)}
        return ScriptedAdvisor(answers, lag_calls=0)

    straight = _settled()
    straight.world.advisor = build(straight)
    straight.run(120)
    uninterrupted = state_hash(straight.world)

    store = SqliteWorldStore(tmp_path / "clean.db")
    interrupted = _settled()
    interrupted.world.advisor = build(interrupted)
    interrupted.run(60)
    assert advisor_state(interrupted.world).pending == [], "expected nothing in flight"
    store.save(interrupted.world)
    store.close()

    reloaded = SqliteWorldStore(tmp_path / "clean.db").load()
    reloaded.advisor = ScriptedAdvisor(answers, lag_calls=0)
    resumed = Simulation(reloaded)
    resumed.run(60)

    assert state_hash(resumed.world) == uninterrupted


def test_reload_converges_with_a_request_in_flight(tmp_path):
    """With a request outstanding the two worlds cannot be byte-identical — the retry is
    a real extra call and is correctly counted. What must hold is that the reloaded world
    reaches the same goals, and never overspends."""
    lag = 3
    answers_for = lambda world: {cid: "expand" for cid in _clan_ids(world)}  # noqa: E731

    straight = _settled()
    straight.world.advisor = ScriptedAdvisor(answers_for(straight.world), lag_calls=lag)
    straight.run(120)

    store = SqliteWorldStore(tmp_path / "mid.db")
    interrupted = _settled()
    interrupted.world.advisor = ScriptedAdvisor(answers_for(interrupted.world), lag_calls=lag)
    interrupted.run(60)
    store.save(interrupted.world)
    store.close()

    reloaded = SqliteWorldStore(tmp_path / "mid.db").load()
    reloaded.advisor = ScriptedAdvisor(answers_for(reloaded), lag_calls=lag)
    resumed = Simulation(reloaded)
    resumed.run(60)

    straight_goals = {
        straight.world.get(e, Clan).clan_id: straight.world.get(e, Clan).goal
        for e in straight.world.query(Clan)
    }
    resumed_goals = {
        resumed.world.get(e, Clan).clan_id: resumed.world.get(e, Clan).goal
        for e in resumed.world.query(Clan)
    }
    assert resumed_goals == straight_goals

    assert advisor_state(resumed.world).calls_made <= CONFIG.llm_max_calls_per_session
