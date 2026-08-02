"""Structured messaging: envelope shape, addressing, next-tick delivery, bounds."""

from __future__ import annotations

from src.world.components import (
    BROADCAST_ALL,
    BROADCAST_CLAN,
    Agent,
    Clan,
    ClanRef,
    Inbox,
    MessageLog,
    MessageType,
    Outbox,
    Position,
)
from src.world.config import WorldConfig
from src.world.ecs import Entity, World
from src.world.rng import TickRng
from src.world.systems import messaging
from src.world.tick import Simulation, create_world


CONFIG = WorldConfig(seed=8, grid_width=32, grid_height=32, agent_count=20, resource_count=60)


def _world_with_agents(count: int) -> tuple[World, list[Entity]]:
    world = World(CONFIG)
    world.add(world.create_entity(), MessageLog())
    entities = []
    for index in range(count):
        entity = world.create_entity()
        world.add(entity, Agent(name=f"a{index}"))
        world.add(entity, Position(index, 0))
        world.add(entity, ClanRef())
        world.add(entity, Inbox())
        world.add(entity, Outbox())
        entities.append(entity)
    return world, entities


def _deliver(world: World) -> None:
    world.tick += 1
    messaging.run(world, TickRng(CONFIG.seed, world.tick, "messaging"))


def test_envelope_has_exactly_the_specified_shape():
    message = messaging.make_message(3, 7, MessageType.OFFER, {"food": 2})
    assert set(message) == {"from", "to", "type", "content"}
    assert message["from"] == 3
    assert message["to"] == 7
    assert message["type"] == "offer"
    assert message["content"] == {"food": 2}


def test_direct_message_reaches_only_its_recipient():
    world, (a, b, c) = _world_with_agents(3)
    messaging.send(world, a, messaging.make_message(a, b, MessageType.INFO, {"x": 1}))
    _deliver(world)

    assert len(world.get(b, Inbox).messages) == 1
    assert world.get(c, Inbox).messages == []
    assert world.get(a, Inbox).messages == []


def test_delivery_happens_on_the_next_tick_not_this_one():
    world, (a, b) = _world_with_agents(2)
    messaging.send(world, a, messaging.make_message(a, b, MessageType.INFO, {}))

    assert world.get(b, Inbox).messages == [], "delivered in the same tick it was sent"
    _deliver(world)
    assert len(world.get(b, Inbox).messages) == 1


def test_outbox_is_drained_after_delivery():
    world, (a, b) = _world_with_agents(2)
    messaging.send(world, a, messaging.make_message(a, b, MessageType.INFO, {}))
    _deliver(world)
    assert world.get(a, Outbox).messages == []

    _deliver(world)
    assert len(world.get(b, Inbox).messages) == 1, "message was delivered twice"


def test_broadcast_all_reaches_everyone_except_the_sender():
    world, entities = _world_with_agents(4)
    sender = entities[0]
    messaging.send(
        world, sender, messaging.make_message(sender, BROADCAST_ALL, MessageType.ALERT, {})
    )
    _deliver(world)

    assert world.get(sender, Inbox).messages == []
    for other in entities[1:]:
        assert len(world.get(other, Inbox).messages) == 1


def test_clan_broadcast_reaches_only_clan_mates():
    world, (a, b, outsider) = _world_with_agents(3)
    clan = Clan(clan_id=1)
    world.add(world.create_entity(), clan)
    clan.add(a)
    clan.add(b)
    world.get(a, ClanRef).clan_id = 1
    world.get(b, ClanRef).clan_id = 1

    messaging.send(
        world, a, messaging.make_message(a, BROADCAST_CLAN, MessageType.INFO, {"rally": [1, 1]})
    )
    _deliver(world)

    assert len(world.get(b, Inbox).messages) == 1
    assert world.get(outsider, Inbox).messages == []
    assert world.get(a, Inbox).messages == []


def test_clan_broadcast_from_a_clanless_agent_goes_nowhere():
    world, (a, b) = _world_with_agents(2)
    messaging.send(world, a, messaging.make_message(a, BROADCAST_CLAN, MessageType.INFO, {}))
    _deliver(world)
    assert world.get(b, Inbox).messages == []


def test_message_to_a_dead_agent_is_dropped():
    world, (a, b) = _world_with_agents(2)
    messaging.send(world, a, messaging.make_message(a, b, MessageType.INFO, {}))
    world.destroy_entity(b)
    _deliver(world)
    assert world.get(a, Outbox).messages == []


def test_inbox_is_bounded():
    world, (a, b) = _world_with_agents(2)
    for index in range(CONFIG.inbox_capacity * 3):
        messaging.send(world, a, messaging.make_message(a, b, MessageType.INFO, {"n": index}))
    _deliver(world)

    inbox = world.get(b, Inbox)
    assert len(inbox.messages) == CONFIG.inbox_capacity
    assert inbox.messages[-1]["content"]["n"] == CONFIG.inbox_capacity * 3 - 1, "kept the wrong end"


def test_inbox_persists_until_consumed():
    """Mail waits for its recipient. A message arriving mid-gather must still be there
    when the agent is free to deal with it."""
    world, (a, b) = _world_with_agents(2)
    messaging.send(world, a, messaging.make_message(a, b, MessageType.INFO, {}))
    _deliver(world)

    for _ in range(20):
        _deliver(world)
    assert len(world.get(b, Inbox).messages) == 1, "unread mail was dropped"

    world.get(b, Inbox).messages.clear()
    _deliver(world)
    assert world.get(b, Inbox).messages == []


def test_messages_actually_flow_and_get_acted_on():
    """Unread mail is the wrong proxy now that agents consume it. The real evidence a
    message was read is food changing hands because of it."""
    simulation = Simulation(create_world(WorldConfig()))
    simulation.run(2000)
    world = simulation.world

    received = sum(world.get(e, Agent).received for e in world.query(Agent))
    assert received > 0, "no agent ever received food from a clanmate"

    for entity in world.query(Inbox):
        for message in world.get(entity, Inbox).messages:
            assert set(message) == {"from", "to", "type", "content"}
            assert message["type"] in {t.value for t in MessageType}


def test_delivered_messages_are_recorded_in_message_log():
    world, (a, b) = _world_with_agents(2)
    messaging.send(world, a, messaging.make_message(a, b, MessageType.INFO, {"x": 1}))
    _deliver(world)

    log = messaging.log(world)
    assert log is not None
    assert len(log.entries) == 1
    entry = log.entries[0]
    assert entry["from"] == a
    assert entry["from_name"] == "a0"
    assert entry["to"] == b
    assert entry["type"] == "info"
    assert entry["content"] == {"x": 1}
    assert entry["tick"] == world.tick
    assert entry["x"] == 0
    assert entry["y"] == 0


def test_broadcast_messages_are_recorded_once_in_message_log():
    world, entities = _world_with_agents(4)
    sender = entities[0]
    messaging.send(
        world, sender, messaging.make_message(sender, BROADCAST_ALL, MessageType.ALERT, {"reason": "test"})
    )
    _deliver(world)

    log = messaging.log(world)
    assert len(log.entries) == 1
    assert log.entries[0]["to"] == BROADCAST_ALL
    assert log.entries[0]["type"] == "alert"


def test_message_log_is_bounded():
    world, (a, b) = _world_with_agents(2)
    limit = CONFIG.message_log_limit
    for index in range(limit * 2):
        messaging.send(world, a, messaging.make_message(a, b, MessageType.INFO, {"n": index}))
        _deliver(world)

    log = messaging.log(world)
    assert len(log.entries) == limit
    assert log.entries[-1]["content"]["n"] == limit * 2 - 1


def test_undelivered_messages_are_not_recorded():
    world, (a, b) = _world_with_agents(2)
    messaging.send(world, a, messaging.make_message(a, b, MessageType.INFO, {}))
    world.destroy_entity(b)
    _deliver(world)

    log = messaging.log(world)
    assert len(log.entries) == 0


def test_messages_appear_in_snapshot():
    simulation = Simulation(create_world(WorldConfig()))
    simulation.run(2000)
    snapshot = simulation.snapshot()
    assert "messages" in snapshot
    assert isinstance(snapshot["messages"], list)
    # The world has been running long enough that some social messages were sent.
    assert len(snapshot["messages"]) > 0
    for entry in snapshot["messages"]:
        assert set(entry) >= {"tick", "from", "from_name", "to", "type", "content", "x", "y"}


def test_saved_worlds_without_message_log_get_migrated_on_load(tmp_path):
    from src.persistence.sqlite_store import SqliteWorldStore

    simulation = Simulation(create_world(WorldConfig()))
    simulation.run(100)
    log = messaging.log(simulation.world)
    log_entity = simulation.world.first(MessageLog)
    # Remove the MessageLog component, as if the world was saved before it existed.
    simulation.world.remove(log_entity, MessageLog)
    simulation.world.destroy_entity(log_entity)
    assert simulation.world.first(MessageLog) is None

    store = SqliteWorldStore(tmp_path / "world.db")
    store.save(simulation.world)
    reloaded = store.load()
    store.close()

    assert reloaded.first(MessageLog) is not None
    reloaded_log = reloaded.get(reloaded.first(MessageLog), MessageLog)
    # After migration, normal message delivery still works.
    reloaded_simulation = Simulation(reloaded)
    reloaded_simulation.run(100)
    assert len(reloaded_log.entries) > 0
    assert "messages" in reloaded_simulation.snapshot()
