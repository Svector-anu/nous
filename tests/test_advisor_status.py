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
