"""Structured message delivery.

Runs first each tick: everything queued in an Outbox last tick lands in the recipients'
Inboxes now. That one-tick lag is deliberate — it is the same delay the blackboard has,
so all social information travels at one speed and no agent can react to something in
the same tick it was said.

Delivered mail persists until the recipient consumes it. Only `inbox_capacity` evicts,
oldest first.

Envelope, exactly as specified:

    {"from": agent_id, "to": agent_id | "clan" | "all",
     "type": "info" | "request" | "offer" | "alert", "content": {...}}
"""

from __future__ import annotations

from ..components import (
    BROADCAST_CLAN,
    BROADCAST_ALL,
    Agent,
    Clan,
    ClanRef,
    Inbox,
    MessageLog,
    MessageType,
    Outbox,
    Position,
)
from ..ecs import Entity, World
from ..rng import TickRng


def log(world: World) -> MessageLog | None:
    entity = world.first(MessageLog)
    return world.get(entity, MessageLog) if entity is not None else None


def _sender_name(world: World, sender: Entity) -> str:
    agent = world.try_get(sender, Agent)
    if agent is not None:
        return agent.name
    return f"#{sender}"


def _record_message(
    world: World,
    message_log: MessageLog,
    sender: Entity,
    to: Entity | str,
    message: dict,
) -> None:
    position = world.try_get(sender, Position)
    entry = {
        "tick": world.tick,
        "from": sender,
        "from_name": _sender_name(world, sender),
        "to": to if isinstance(to, int) else to,
        "type": message.get("type"),
        "content": message.get("content", {}),
        "x": position.x if position is not None else None,
        "y": position.y if position is not None else None,
    }
    message_log.record(entry, world.config.message_log_limit)


def make_message(
    sender: Entity,
    to: Entity | str,
    message_type: MessageType,
    content: dict,
) -> dict:
    return {
        "from": sender,
        "to": to,
        "type": message_type.value,
        "content": content,
    }


def send(world: World, sender: Entity, message: dict) -> None:
    outbox = world.try_get(sender, Outbox)
    if outbox is not None:
        outbox.messages.append(message)


def _clan_members(world: World, sender: Entity) -> list[Entity]:
    reference = world.try_get(sender, ClanRef)
    if reference is None or reference.clan_id is None:
        return []
    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        if clan.clan_id == reference.clan_id:
            return [member for member in clan.members if member != sender]
    return []


def _recipients(world: World, sender: Entity, to: Entity | str) -> list[Entity]:
    if to == BROADCAST_CLAN:
        return _clan_members(world, sender)
    if to == BROADCAST_ALL:
        return [e for e in world.query(Agent, Inbox) if e != sender]
    if isinstance(to, int) and world.is_alive(to) and world.has(to, Inbox):
        return [to]
    return []


def _deliver(inbox: Inbox, message: dict, capacity: int) -> None:
    inbox.messages.append(message)
    if len(inbox.messages) > capacity:
        del inbox.messages[: len(inbox.messages) - capacity]


def run(world: World, rng: TickRng) -> None:
    """Deliver, never clear.

    Mail sits in the inbox until an agent explicitly consumes it, so a message that
    arrives while its recipient is mid-gather is still there when the agent is free.
    The capacity bound is the only thing that removes an unread message, dropping the
    oldest first.

    Every delivered message is recorded once in the MessageLog so the viewer can show
    speech bubbles and a social feed. The log is bounded; the recording is the same
    one-tick lag as delivery, so the message and the log entry share the same tick.
    """
    capacity = world.config.inbox_capacity
    message_log = log(world)

    for sender in world.query(Outbox):
        outbox = world.get(sender, Outbox)
        if not outbox.messages:
            continue
        for message in outbox.messages:
            recipients = _recipients(world, sender, message["to"])
            for recipient in recipients:
                _deliver(world.get(recipient, Inbox), message, capacity)
            if recipients and message_log is not None:
                _record_message(world, message_log, sender, message["to"], message)
        outbox.messages.clear()
