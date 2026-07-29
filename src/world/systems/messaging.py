"""Structured message delivery.

Runs first each tick: everything queued in an Outbox last tick lands in the recipients'
Inboxes now. That one-tick lag is deliberate — it is the same delay the blackboard has,
so all social information travels at one speed and no agent can react to something in
the same tick it was said.

Envelope, exactly as specified:

    {"from": agent_id, "to": agent_id | "clan" | "all",
     "type": "info" | "request" | "offer" | "alert", "content": {...}}
"""

from __future__ import annotations

from ..components import (
    BROADCAST_ALL,
    BROADCAST_CLAN,
    Agent,
    Clan,
    ClanRef,
    Inbox,
    MessageType,
    Outbox,
)
from ..ecs import Entity, World
from ..rng import TickRng


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
    capacity = world.config.inbox_capacity

    for entity in world.query(Inbox):
        world.get(entity, Inbox).messages.clear()

    for sender in world.query(Outbox):
        outbox = world.get(sender, Outbox)
        if not outbox.messages:
            continue
        for message in outbox.messages:
            for recipient in _recipients(world, sender, message["to"]):
                _deliver(world.get(recipient, Inbox), message, capacity)
        outbox.messages.clear()
