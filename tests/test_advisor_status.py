"""Viewer-facing advisor status: soft limits, never blocking, $0 when disabled."""

from __future__ import annotations

from src.llm.advisor import AdvisorBudget, NullAdvisor, ScriptedAdvisor
from src.world.components import AdvisorState
from src.world.components import Clan
from src.world.config import WorldConfig
from src.world.systems.leadership import advisor_status
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(seed=61, grid_width=32, grid_height=32, agent_count=30, resource_count=90)


def test_advisor_status_is_present_in_snapshot():
    simulation = Simulation(create_world(CONFIG))
    snapshot = simulation.snapshot()
    assert "advisor" in snapshot
    assert snapshot["advisor"]["llm_enabled"] is False
    assert snapshot["advisor"]["pending"] == 0
    assert snapshot["advisor"]["calls_made"] == 0


def test_advisor_status_reflects_disabled_llm():
    world = create_world(CONFIG)
    status = advisor_status(world)
    assert status["llm_enabled"] is False
    assert status["advisor"] == "NullAdvisor"
    assert status["pending"] == 0
    assert status["calls_made"] == 0


def test_advisor_status_shows_in_flight_requests():
    simulation = Simulation(create_world(CONFIG))
    simulation.run(300)
    world = simulation.world
    clan_ids = [world.get(e, Clan).clan_id for e in world.query(Clan)]
    world.advisor = ScriptedAdvisor({cid: "expand" for cid in clan_ids}, lag_calls=10)
    simulation.run(5)

    status = advisor_status(world)
    assert status["pending"] > 0
    assert status["calls_made"] > 0
    assert status["calls_made"] <= CONFIG.llm_max_calls_per_session


def test_advisor_status_does_not_call_network():
    """The snapshot helper must never call the advisor's network path."""
    from src.llm.advisor import build_advisor

    config = WorldConfig(llm_enabled=True, llm_provider="anthropic")
    world = create_world(config)
    world.advisor = build_advisor(config)
    # No API key; the advisor would fail on an actual call. The status read is safe.
    status = advisor_status(world)
    assert status["advisor"] == "ClaudeAdvisor"
    assert status["pending"] == 0
    world.advisor.close()


def test_advisor_status_reports_session_cap():
    simulation = Simulation(create_world(CONFIG))
    status = simulation.snapshot()["advisor"]
    assert status["max_calls"] == CONFIG.llm_max_calls_per_session


# --- saying no ------------------------------------------------------------------
#
# The config saying "yes" is not evidence of anything. These cover the three ways an
# advisor stops answering while `llm_enabled` stays True, which is how a dead advisor
# went unnoticed on a live world: the status kept naming a provider that answered nothing.


def test_status_says_no_when_the_advisor_has_self_disabled():
    from src.llm.advisor import build_advisor

    config = WorldConfig(llm_enabled=True, llm_provider="anthropic")
    world = create_world(config)
    world.advisor = build_advisor(config)
    # Exactly what a 401 does on the first real call.
    world.advisor._disable("authentication failed: 401 invalid token")

    status = advisor_status(world)
    assert status["working"] is False
    assert "401" in status["reason"]
    # The old shape stays truthful about what was *asked* for, which is why it was
    # never enough on its own.
    assert status["llm_enabled"] is True
    assert status["advisor"] == "ClaudeAdvisor"
    world.advisor.close()


def test_status_says_no_when_the_budget_is_spent():
    config = WorldConfig(llm_enabled=True, llm_max_calls_per_session=5)
    world = create_world(config)
    world.advisor = ScriptedAdvisor({})
    state = world.get(world.first(AdvisorState), AdvisorState)
    state.calls_made = 5

    status = advisor_status(world)
    assert status["budget_spent"] is True
    assert status["working"] is False
    assert "5/5" in status["reason"]


def test_status_says_no_when_the_provider_is_none():
    world = create_world(WorldConfig(llm_enabled=True))
    world.advisor = NullAdvisor()
    status = advisor_status(world)
    assert status["working"] is False
    assert status["reason"] == "no provider configured"


def test_status_says_yes_when_the_advisor_can_actually_answer():
    """The counterpart that stops `working` from being a field that is always False."""
    config = WorldConfig(llm_enabled=True, llm_max_calls_per_session=100)
    world = create_world(config)
    world.advisor = ScriptedAdvisor({})

    status = advisor_status(world)
    assert status["working"] is True
    assert status["reason"] == ""
    assert status["budget_spent"] is False


def test_reading_the_status_does_not_build_a_client():
    """Asking whether it works must not be the thing that decides whether it works."""
    from src.llm.advisor import build_advisor

    config = WorldConfig(llm_enabled=True, llm_provider="anthropic")
    world = create_world(config)
    world.advisor = build_advisor(config)
    assert world.advisor._client is None

    advisor_status(world)

    assert world.advisor._client is None, "status read constructed a provider client"
    world.advisor.close()


def test_spending_the_budget_is_announced_exactly_once(caplog):
    """A world that stops consulting a model looks identical to one that never started.
    This is the only moment the difference is visible, so it has to be said out loud —
    and said once, not on every tick for the rest of the world's life."""
    import logging

    from src.world.systems.leadership import _note_budget_spent

    world = create_world(WorldConfig(llm_enabled=True, llm_max_calls_per_session=3))
    state = world.get(world.first(AdvisorState), AdvisorState)

    with caplog.at_level(logging.WARNING, logger="neociv"):
        for _ in range(6):
            state.calls_made += 1
            _note_budget_spent(world, state)

    spent = [r for r in caplog.records if "budget spent" in r.getMessage()]
    assert len(spent) == 1, f"expected one announcement, got {len(spent)}"
    assert "3 of 3" in spent[0].getMessage()
