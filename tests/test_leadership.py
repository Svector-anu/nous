"""Hierarchical cognition: only leaders, strictly capped, always falling back to rules."""

from __future__ import annotations

from src.llm.advisor import (
    GOALS,
    AdvisorBudget,
    ClaudeAdvisor,
    GoalBrief,
    GoalDecision,
    NullAdvisor,
    ScriptedAdvisor,
    build_advisor,
)
from src.world.components import Agent, Clan, ClanGoal, DecisionLog
from src.world.config import WorldConfig
from src.world.tick import Simulation, create_world, state_hash

CONFIG = WorldConfig(seed=61, grid_width=32, grid_height=32, agent_count=30, resource_count=90)


def _sim(advisor=None, config: WorldConfig = CONFIG) -> Simulation:
    world = create_world(config)
    world.advisor = advisor
    return Simulation(world)


def _clan_ids(world) -> list[int]:
    return [world.get(e, Clan).clan_id for e in world.query(Clan)]


def _log(world) -> DecisionLog:
    return world.get(world.first(DecisionLog), DecisionLog)


# --- the default is no llm at all -------------------------------------------


def test_default_world_has_no_advisor():
    assert create_world(CONFIG).advisor is None


def test_llm_disabled_by_default():
    assert WorldConfig().llm_enabled is False


def test_build_advisor_returns_null_when_disabled():
    assert isinstance(build_advisor(WorldConfig()), NullAdvisor)


def test_build_advisor_goes_live_when_enabled():
    """Credentials are not gated on env vars — the sdk also resolves auth tokens and
    `ant auth login` profiles, so an env check would refuse a working setup."""
    advisor = build_advisor(WorldConfig(llm_enabled=True))
    assert isinstance(advisor, ClaudeAdvisor)
    advisor.close()


def test_auth_failure_disables_the_advisor_permanently():
    """One bad-credentials answer must not burn the whole session budget."""
    import anthropic
    from concurrent.futures import Future

    advisor = ClaudeAdvisor(budget=AdvisorBudget(max_inflight=4, max_calls_per_session=100))
    advisor._client = object()

    failed: Future = Future()
    failed.set_exception(
        anthropic.AuthenticationError("bad key", response=_FakeResponse(401), body=None)
    )
    advisor._inflight = [(failed, GoalBrief(1, 0, 3, 50.0, 0.5, 1.0, 0, "rally", 0, 0), 0.0)]

    assert advisor.collect() == []
    assert advisor._unavailable_reason is not None
    assert advisor.submit(GoalBrief(1, 0, 3, 50.0, 0.5, 1.0, 0, "rally", 0, 0)) is False


def test_non_auth_failure_does_not_disable_the_advisor():
    from concurrent.futures import Future

    advisor = ClaudeAdvisor()
    advisor._client = object()

    failed: Future = Future()
    failed.set_exception(RuntimeError("transient network blip"))
    advisor._inflight = [(failed, GoalBrief(1, 0, 3, 50.0, 0.5, 1.0, 0, "rally", 0, 0), 0.0)]

    assert advisor.collect() == []
    assert advisor._unavailable_reason is None, "a transient failure must not disable it"


def test_null_advisor_never_answers():
    advisor = NullAdvisor()
    brief = GoalBrief(1, 0, 3, 50.0, 0.5, 1.0, 0, "rally", 0, 0)
    assert advisor.submit(brief) is False
    assert advisor.collect() == []


def test_world_runs_identically_with_and_without_a_null_advisor():
    plain = _sim()
    plain.run(600)
    advised = _sim(advisor=NullAdvisor())
    advised.run(600)
    assert state_hash(plain.world) == state_hash(advised.world)


# --- rules remain the floor -------------------------------------------------


def test_rules_still_set_goals_when_the_advisor_says_nothing():
    simulation = _sim(advisor=NullAdvisor())
    simulation.run(400)
    world = simulation.world

    clans = [world.get(e, Clan) for e in world.query(Clan)]
    assert clans
    for clan in clans:
        assert clan.goal in {g.value for g in ClanGoal}
        assert clan.goal_source == "rules"


def test_a_clan_always_has_a_goal_while_a_decision_is_pending():
    """The advisor is slow on purpose: the clan must not stall waiting for it."""
    simulation = _sim()
    simulation.run(300)
    world = simulation.world
    target = _clan_ids(world)[0]
    world.advisor = ScriptedAdvisor({target: ClanGoal.EXPAND.value}, lag_calls=50)

    simulation.run(5)
    clan = next(world.get(e, Clan) for e in world.query(Clan) if world.get(e, Clan).clan_id == target)
    assert clan.goal in {g.value for g in ClanGoal}


def test_advisor_failure_falls_back_to_rules():
    class ExplodingAdvisor:
        def submit(self, brief):
            raise RuntimeError("boom")

        def collect(self):
            return []

        def pending(self):
            return 0

        def close(self):
            return None

    simulation = _sim(advisor=ExplodingAdvisor())
    try:
        simulation.run(400)
    except RuntimeError:
        raise AssertionError("an advisor failure must not propagate into the tick loop")


# --- decisions are applied --------------------------------------------------


def test_advisor_decision_overrides_the_rule_based_goal():
    simulation = _sim()
    simulation.run(300)
    world = simulation.world
    target = _clan_ids(world)[0]
    world.advisor = ScriptedAdvisor({target: ClanGoal.EXPAND.value}, lag_calls=0)

    simulation.run(3)

    clan = next(world.get(e, Clan) for e in world.query(Clan) if world.get(e, Clan).clan_id == target)
    assert clan.goal == ClanGoal.EXPAND.value
    assert clan.goal_source == "llm"
    assert clan.goal_reason == "scripted"


def test_only_the_targeted_clan_is_affected():
    simulation = _sim()
    simulation.run(300)
    world = simulation.world
    ids = _clan_ids(world)
    target = ids[0]
    world.advisor = ScriptedAdvisor({target: ClanGoal.RAID.value}, lag_calls=0)

    simulation.run(3)

    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        if clan.clan_id != target:
            assert clan.goal_source == "rules"


def test_invalid_goals_are_rejected():
    decision = GoalDecision(1, "conquer_the_moon", "nonsense", "llm")
    assert not decision.is_valid()
    assert all(GoalDecision(1, g, "", "llm").is_valid() for g in GOALS)


# --- rate limits and cost controls ------------------------------------------


def test_a_clan_is_not_consulted_more_often_than_the_cooldown():
    config = WorldConfig(
        seed=61, grid_width=32, grid_height=32, agent_count=30, resource_count=90,
        llm_min_ticks_between_calls=500,
    )
    simulation = _sim(config=config)
    simulation.run(300)
    world = simulation.world
    advisor = ScriptedAdvisor({cid: ClanGoal.RALLY.value for cid in _clan_ids(world)}, lag_calls=0)
    world.advisor = advisor

    simulation.run(400)
    first_round = advisor.calls
    simulation.run(1)
    assert advisor.calls == first_round, "consulted again inside the cooldown window"


def test_session_call_budget_is_enforced():
    budget = AdvisorBudget(max_inflight=5, max_calls_per_session=3)
    advisor = ClaudeAdvisor(budget=budget)
    advisor._client = object()  # pretend the sdk is available
    advisor._pool = _ImmediatePool()

    brief = GoalBrief(1, 0, 3, 50.0, 0.5, 1.0, 0, "rally", 0, 0)
    accepted = sum(1 for _ in range(10) if advisor.submit(brief))
    assert accepted == 3


def test_inflight_cap_is_enforced():
    budget = AdvisorBudget(max_inflight=2, max_calls_per_session=100)
    advisor = ClaudeAdvisor(budget=budget)
    advisor._client = object()
    advisor._pool = _NeverFinishingPool()

    brief = GoalBrief(1, 0, 3, 50.0, 0.5, 1.0, 0, "rally", 0, 0)
    accepted = sum(1 for _ in range(10) if advisor.submit(brief))
    assert accepted == 2


def test_claude_advisor_without_sdk_is_unavailable(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("no sdk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    advisor = ClaudeAdvisor()
    brief = GoalBrief(1, 0, 3, 50.0, 0.5, 1.0, 0, "rally", 0, 0)
    assert advisor.submit(brief) is False
    assert advisor.collect() == []


# --- logging ----------------------------------------------------------------


def test_every_decision_is_logged():
    simulation = _sim(advisor=NullAdvisor())
    simulation.run(400)
    entries = _log(simulation.world).entries

    assert entries, "no decisions were logged"
    for entry in entries:
        assert set(entry) == {"tick", "clan", "goal", "source", "reason", "latency_ms"}
        assert entry["source"] in {"rules", "llm"}


def test_llm_decisions_are_logged_with_their_reason():
    simulation = _sim()
    simulation.run(300)
    world = simulation.world
    target = _clan_ids(world)[0]
    world.advisor = ScriptedAdvisor({target: ClanGoal.RAID.value}, lag_calls=0, reason="famine")

    simulation.run(3)

    llm_entries = [e for e in _log(world).entries if e["source"] == "llm"]
    assert llm_entries
    assert llm_entries[-1]["reason"] == "famine"
    assert llm_entries[-1]["clan"] == target


def test_the_log_is_bounded():
    config = WorldConfig(
        seed=61, grid_width=32, grid_height=32, agent_count=30, resource_count=90,
        llm_log_limit=10,
    )
    simulation = _sim(config=config)
    simulation.run(3000)
    assert len(_log(simulation.world).entries) <= 10


def test_log_survives_save_and_reload(tmp_path):
    from src.persistence.sqlite_store import SqliteWorldStore

    store = SqliteWorldStore(tmp_path / "decisions.db")
    simulation = _sim()
    simulation.store = store
    simulation.run(400)
    before = list(_log(simulation.world).entries)
    store.save(simulation.world)
    store.close()

    loaded = SqliteWorldStore(tmp_path / "decisions.db").load()
    assert _log(loaded).entries == before


# --- helpers ----------------------------------------------------------------


class _ImmediatePool:
    def submit(self, fn, *args):
        from concurrent.futures import Future

        future: Future = Future()
        future.set_result(None)
        return future


class _NeverFinishingPool:
    def submit(self, fn, *args):
        from concurrent.futures import Future

        return Future()


class _FakeResponse:
    """Minimal stand-in for the httpx response the SDK error types expect."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.request = None
