"""Natural-language message text is deterministic, bounded, and influenced by personality."""

from __future__ import annotations

import pytest

from src.world.components import Agent, Clan, ClanGoal, MessageType, ResourceKind
from src.world.config import WorldConfig
from src.world.ecs import World
from src.world.systems import messaging
from src.world.systems.message_composer import (
    MAX_TEXT_LENGTH,
    compose_content,
    compose_text,
)


CONFIG = WorldConfig(seed=7, grid_width=16, grid_height=16, agent_count=4, resource_count=20)


def _world() -> World:
    return World(CONFIG)


def _agent(world: World, name: str, personality: str = "") -> int:
    entity = world.create_entity()
    world.add(entity, Agent(name=name, personality=personality))
    return entity


def test_rally_text_without_personality():
    world = _world()
    sender = _agent(world, "A")
    text = compose_text(world, sender, MessageType.INFO, {"rally": [3, 5]})
    assert "3" in text and "5" in text
    assert len(text) <= MAX_TEXT_LENGTH


def test_rally_text_reflects_builder_personality():
    world = _world()
    builder = _agent(world, "B", "hoards wood and builds walls")
    text = compose_text(world, builder, MessageType.INFO, {"rally": [3, 5]})
    assert "3" in text and "5" in text
    assert any(word in text.lower() for word in {"build", "walls", "wood"})
    assert len(text) <= MAX_TEXT_LENGTH


def test_rally_text_reflects_warrior_personality():
    world = _world()
    warrior = _agent(world, "W", "distrusts strangers, fights raiders")
    text = compose_text(world, warrior, MessageType.INFO, {"rally": [3, 5]})
    assert "3" in text and "5" in text
    assert any(word in text.lower() for word in {"defend", "hold", "ready"})
    assert len(text) <= MAX_TEXT_LENGTH


def test_goal_text_varies_by_personality():
    world = _world()
    gatherer = _agent(world, "G", "always gathers food")
    text = compose_text(world, gatherer, MessageType.INFO, {"goal": ClanGoal.GATHER_FOOD.value})
    assert any(word in text.lower() for word in {"food", "harvest", "gather", "hunt", "eat"})
    assert len(text) <= MAX_TEXT_LENGTH


def test_goal_text_fallback_for_unknown_goal():
    world = _world()
    sender = _agent(world, "A")
    text = compose_text(world, sender, MessageType.INFO, {"goal": "not_a_real_goal"})
    assert "not_a_real_goal" in text


def test_request_text_for_food():
    world = _world()
    sender = _agent(world, "A")
    text = compose_text(
        world, sender, MessageType.REQUEST, {"resource": ResourceKind.FOOD.value}
    )
    assert "food" in text.lower() or "starving" in text.lower()
    assert len(text) <= MAX_TEXT_LENGTH


def test_offer_text_for_food():
    world = _world()
    sender = _agent(world, "A", "likes to share")
    text = compose_text(
        world, sender, MessageType.OFFER, {"resource": ResourceKind.FOOD.value, "at": [1, 2]}
    )
    assert "food" in text.lower() or "share" in text.lower()
    assert len(text) <= MAX_TEXT_LENGTH


def test_alert_text_for_raid():
    world = _world()
    sender = _agent(world, "A", "warlike and distrustful")
    text = compose_text(
        world, sender, MessageType.ALERT, {"resource": ResourceKind.FOOD.value, "reason": "raid"}
    )
    assert any(word in text.lower() for word in {"raid", "defend", "fight", "arms"})
    assert len(text) <= MAX_TEXT_LENGTH


def test_compose_content_preserves_structured_fields():
    world = _world()
    sender = _agent(world, "A")
    content = {"rally": [7, 9]}
    composed = compose_content(world, sender, MessageType.INFO, content)
    assert composed["rally"] == [7, 9]
    assert "text" in composed
    assert composed["text"] != ""


def test_personality_is_deterministic():
    world = _world()
    sender = _agent(world, "A", "hoards wood")
    first = compose_text(world, sender, MessageType.INFO, {"rally": [3, 5]})
    second = compose_text(world, sender, MessageType.INFO, {"rally": [3, 5]})
    assert first == second


def test_message_envelope_still_valid_with_composed_content():
    world = _world()
    sender = _agent(world, "A")
    receiver = _agent(world, "B")
    content = compose_content(world, sender, MessageType.INFO, {"rally": [1, 1]})
    message = messaging.make_message(sender, receiver, MessageType.INFO, content)
    assert set(message) == {"from", "to", "type", "content"}
    assert message["content"]["rally"] == [1, 1]
    assert "text" in message["content"]


def test_clan_leader_goal_message_has_text():
    """When a clan leader announces a goal change, the message carries natural text."""
    from src.world.components import (
        BROADCAST_CLAN,
        Blackboard,
        ClanRef,
        Inbox,
        Inventory,
        Needs,
        Outbox,
        Position,
    )
    from src.world.rng import TickRng
    from src.world.systems import social

    world = _world()
    world.add(world.create_entity(), messaging.MessageLog())
    world.add(world.create_entity(), Blackboard())

    leader = world.create_entity()
    world.add(leader, Agent(name="Leader", personality="warlike"))
    world.add(leader, Position(5, 5))
    world.add(leader, Needs(energy=40, hunger=40, social=40, safety=40))
    world.add(leader, ClanRef())
    world.add(leader, Inbox())
    world.add(leader, Outbox())

    member = world.create_entity()
    world.add(member, Agent(name="Member"))
    world.add(member, Position(5, 5))
    world.add(member, Needs(energy=40, hunger=40, social=40, safety=40))
    world.add(member, ClanRef())
    world.add(member, Inbox())
    world.add(member, Outbox())

    clan = Clan(clan_id=1)
    clan.add(leader)
    clan.add(member)
    clan.leader = leader
    world.add(world.create_entity(), clan)
    world.get(leader, ClanRef).clan_id = 1
    world.get(member, ClanRef).clan_id = 1

    # Force a goal change by giving the clan enough resources to expand.
    world.add(leader, Inventory(wood=50))
    world.add(member, Inventory(wood=50))
    clan.goal_set_tick = -1000

    world.tick = 1
    social.run(world, TickRng(CONFIG.seed, world.tick, "social"))

    outbox = world.get(leader, Outbox)
    texts = [m["content"].get("text", "") for m in outbox.messages]
    assert any(texts), "leader sent no text-bearing message"
    for text in texts:
        assert len(text) <= MAX_TEXT_LENGTH
