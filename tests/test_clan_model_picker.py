"""Per-clan model selection: the feature from GitHub issue #16.

Covers:
  1. Clan.advisor_model defaults to "" and round-trips through sqlite.
  2. _brief() passes clan.advisor_model into GoalBrief.model.
  3. OpenAICompatibleAdvisor uses brief.model when set, falls back to self.model.
  4. ClaudeAdvisor._ask uses brief.model when set.
  5. list_models() returns a list[str] (mocked gateway).
  6. advisor_status includes the "model" field.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.llm.advisor import GoalBrief
from src.llm.openai_advisor import OpenAICompatibleAdvisor
from src.world.components import Clan
from src.world.config import WorldConfig
from src.world.ecs import World


# ---------------------------------------------------------------------------
# 1. Clan.advisor_model round-trips through sqlite
# ---------------------------------------------------------------------------

def test_clan_advisor_model_default():
    clan = Clan(clan_id=1)
    assert clan.advisor_model == ""


def test_clan_advisor_model_round_trips_through_sqlite(tmp_path):
    from src.persistence.sqlite_store import SqliteWorldStore

    db = tmp_path / "world.db"
    store = SqliteWorldStore(db)

    world = World(WorldConfig())
    entity = world.create_entity()
    clan = Clan(clan_id=42, advisor_model="anthropic/claude-sonnet-4")
    world.add(entity, clan)
    store.save(world)

    loaded = store.load()
    clans = [loaded.get(e, Clan) for e in loaded.query(Clan)]
    target = next(c for c in clans if c.clan_id == 42)
    assert target.advisor_model == "anthropic/claude-sonnet-4"


def test_clan_advisor_model_defaults_on_old_save(tmp_path):
    """A save from before this field was added (missing the key) gives the default."""
    import sqlite3

    from src.persistence.sqlite_store import SqliteWorldStore

    db = tmp_path / "world.db"
    store = SqliteWorldStore(db)

    world = World(WorldConfig())
    entity = world.create_entity()
    world.add(entity, Clan(clan_id=7, advisor_model="temporary"))
    store.save(world)

    # Simulate an older save by removing advisor_model from the JSON blob.
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT entity, data FROM components WHERE kind = 'Clan'"
    ).fetchall()
    for (ent, data) in rows:
        payload = json.loads(data)
        payload.pop("advisor_model", None)
        conn.execute(
            "UPDATE components SET data = ? WHERE entity = ? AND kind = 'Clan'",
            (json.dumps(payload), ent),
        )
    conn.commit()
    conn.close()

    loaded = store.load()
    clans = [loaded.get(e, Clan) for e in loaded.query(Clan)]
    target = next(c for c in clans if c.clan_id == 7)
    assert target.advisor_model == ""


# ---------------------------------------------------------------------------
# 2. _brief() carries clan.advisor_model into GoalBrief.model
# ---------------------------------------------------------------------------

def _make_world_with_clan(advisor_model: str = "") -> tuple:
    from src.world.components import Agent, AgentState, Inventory, Needs, Position, ResourceKind

    world = World(WorldConfig())

    leader = world.create_entity()
    world.add(leader, Agent(name="L", state=AgentState.IDLE, wants=ResourceKind.FOOD))
    world.add(leader, Position(x=5, y=5))
    world.add(leader, Needs(energy=90, hunger=90, social=100, safety=100))
    world.add(leader, Inventory())

    clan = Clan(clan_id=1, leader=leader, members=[leader], advisor_model=advisor_model)
    clan_entity = world.create_entity()
    world.add(clan_entity, clan)

    return world, clan


def test_brief_passes_advisor_model():
    from src.world.systems.leadership import _brief

    world, clan = _make_world_with_clan(advisor_model="anthropic/claude-haiku-4")
    brief = _brief(world, clan, nearby=0)
    assert brief.model == "anthropic/claude-haiku-4"


def test_brief_passes_empty_when_not_set():
    from src.world.systems.leadership import _brief

    world, clan = _make_world_with_clan(advisor_model="")
    brief = _brief(world, clan, nearby=0)
    assert brief.model == ""


# ---------------------------------------------------------------------------
# 3. OpenAICompatibleAdvisor uses brief.model when set
# ---------------------------------------------------------------------------

def _make_brief(model: str = "") -> GoalBrief:
    return GoalBrief(
        clan_id=1, tick=1, members=5, mean_hunger=70.0, food_ratio=0.5,
        huts_per_member=2.0, wood_held=10, current_goal="rally",
        nearby_clans=2, recent_raids_suffered=0, model=model,
    )


def test_openai_payload_uses_brief_model_when_set():
    advisor = OpenAICompatibleAdvisor(model="default-model")
    payload = advisor._payload(_make_brief(model="override-model"))
    assert payload["model"] == "override-model"


def test_openai_payload_falls_back_to_self_model_when_brief_model_empty():
    advisor = OpenAICompatibleAdvisor(model="default-model")
    payload = advisor._payload(_make_brief(model=""))
    assert payload["model"] == "default-model"


# ---------------------------------------------------------------------------
# 4. ClaudeAdvisor._ask uses brief.model when set
# ---------------------------------------------------------------------------

def test_claude_ask_uses_brief_model():
    from src.llm.anthropic_advisor import ClaudeAdvisor

    advisor = ClaudeAdvisor(model="claude-opus-5")

    fake_response = MagicMock()
    fake_response.stop_reason = "end_turn"
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = '{"goal": "expand", "reason": "test"}'
    fake_response.content = [fake_block]

    captured_model = []

    def fake_create(**kwargs):
        captured_model.append(kwargs.get("model"))
        return fake_response

    advisor._client = MagicMock()
    advisor._client.messages.create.side_effect = fake_create

    advisor._ask(_make_brief(model="claude-sonnet-4"))
    assert captured_model == ["claude-sonnet-4"]


def test_claude_ask_falls_back_to_self_model_when_empty():
    from src.llm.anthropic_advisor import ClaudeAdvisor

    advisor = ClaudeAdvisor(model="claude-opus-5")

    fake_response = MagicMock()
    fake_response.stop_reason = "end_turn"
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = '{"goal": "expand", "reason": "test"}'
    fake_response.content = [fake_block]

    captured_model = []

    def fake_create(**kwargs):
        captured_model.append(kwargs.get("model"))
        return fake_response

    advisor._client = MagicMock()
    advisor._client.messages.create.side_effect = fake_create

    advisor._ask(_make_brief(model=""))
    assert captured_model == ["claude-opus-5"]


# ---------------------------------------------------------------------------
# 5. list_models() parses a gateway /v1/models response
# ---------------------------------------------------------------------------

def test_list_models_parses_gateway_response():
    from src.llm.openai_advisor import list_models

    fake_body = {
        "data": [
            {"id": "anthropic/claude-opus-5"},
            {"id": "anthropic/claude-sonnet-4"},
            {"id": "openai/gpt-4o"},
        ]
    }

    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = fake_body

    with patch("httpx.get", return_value=fake_response):
        result = list_models("https://api.dgrid.ai/v1", "DGRID_API_KEY")

    assert "anthropic/claude-opus-5" in result
    assert "anthropic/claude-sonnet-4" in result
    assert result == sorted(result)


def test_list_models_returns_empty_on_network_error():
    from src.llm.openai_advisor import list_models

    with patch("httpx.get", side_effect=Exception("timeout")):
        result = list_models("https://api.dgrid.ai/v1", "DGRID_API_KEY")

    assert result == []


# ---------------------------------------------------------------------------
# 6. advisor_status includes model field
# ---------------------------------------------------------------------------

def test_advisor_status_includes_model():
    from src.llm.advisor import NullAdvisor
    from src.world.systems.leadership import advisor_status
    from src.world.tick import create_world

    config = WorldConfig(
        agent_count=4, resource_count=10, grid_width=16, grid_height=16,
        llm_enabled=False, llm_model="anthropic/claude-opus-5",
    )
    world = create_world(config)
    world.advisor = NullAdvisor()
    status = advisor_status(world)
    assert "model" in status
    assert status["model"] == "anthropic/claude-opus-5"
