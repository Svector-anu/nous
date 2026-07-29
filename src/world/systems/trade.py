"""Reading the inbox and acting on it.

Runs between needs and fsm: hunger is already fresh, and any food that changes hands
here is in the inventory before the agent decides what to do this tick.

The protocol is two hops and uses three of the four message types:

    request  routine "I could use food"      -> a clanmate in range hands some over,
                                                one out of range replies with an offer
    alert    "I am starving"                 -> donors dip into their reserve
    offer    "come to me, I have spare"      -> the asker walks over in MEET, and once
                                                adjacent its next request is fulfilled

Messages an agent cannot act on right now stay in its inbox. Only `info` is always
consumed, because rally chatter must never push a real request out of a full inbox.
"""

from __future__ import annotations

from ..components import (
    BROADCAST_CLAN,
    Agent,
    AgentState,
    ClanRef,
    Inbox,
    Inventory,
    MessageType,
    Needs,
    Position,
    ResourceKind,
)
from ..config import WorldConfig
from ..ecs import Entity, World
from ..rng import TickRng
from .messaging import make_message, send
from .needs import is_starving


def transfer(
    world: World,
    donor: Entity,
    receiver: Entity,
    kind: ResourceKind,
    amount: int,
) -> int:
    """Move up to `amount` of `kind` from donor to receiver. Returns what actually moved.

    Bounded on both sides: never more than the donor holds, never past the receiver's
    carry_capacity. Total resource in the world is unchanged.
    """
    if amount <= 0 or donor == receiver:
        return 0
    if not (world.is_alive(donor) and world.is_alive(receiver)):
        return 0

    donor_inventory = world.try_get(donor, Inventory)
    receiver_inventory = world.try_get(receiver, Inventory)
    if donor_inventory is None or receiver_inventory is None:
        return 0

    capacity = world.config.carry_capacity
    if kind is ResourceKind.FOOD:
        moved = min(amount, donor_inventory.food, capacity - receiver_inventory.food)
        if moved <= 0:
            return 0
        donor_inventory.food -= moved
        receiver_inventory.food += moved
    else:
        moved = min(amount, donor_inventory.wood, capacity - receiver_inventory.wood)
        if moved <= 0:
            return 0
        donor_inventory.wood -= moved
        receiver_inventory.wood += moved

    return moved


def in_range(world: World, a: Entity, b: Entity) -> bool:
    pa = world.try_get(a, Position)
    pb = world.try_get(b, Position)
    if pa is None or pb is None:
        return False
    radius = world.config.transfer_radius
    return abs(pa.x - pb.x) <= radius and abs(pa.y - pb.y) <= radius


def _spare_food(inventory: Inventory, config: WorldConfig, emergency: bool) -> int:
    """How much an agent is willing to give. An alert overrides the reserve it keeps
    for itself — that is the whole difference between a request and an alert."""
    keep = 0 if emergency else config.surplus_reserve
    return max(0, inventory.food - keep)


def _same_clan(world: World, a: Entity, b: Entity) -> bool:
    ref_a = world.try_get(a, ClanRef)
    ref_b = world.try_get(b, ClanRef)
    return (
        ref_a is not None
        and ref_b is not None
        and ref_a.clan_id is not None
        and ref_a.clan_id == ref_b.clan_id
    )


def _handle_request(
    world: World, entity: Entity, message: dict, emergency: bool
) -> bool:
    """Returns True when the message has been dealt with and can leave the inbox."""
    asker = message["from"]
    if not world.is_alive(asker) or not _same_clan(world, entity, asker):
        return True

    inventory = world.get(entity, Inventory)
    spare = _spare_food(inventory, world.config, emergency)
    if spare <= 0:
        return False

    if in_range(world, entity, asker):
        moved = transfer(world, entity, asker, ResourceKind.FOOD, spare)
        if moved > 0:
            world.get(asker, Agent).received += moved
        return True

    position = world.get(entity, Position)
    send(
        world,
        entity,
        make_message(
            entity,
            asker,
            MessageType.OFFER,
            {"resource": ResourceKind.FOOD.value, "at": [position.x, position.y]},
        ),
    )
    return True


def _handle_offer(world: World, entity: Entity, message: dict) -> bool:
    agent = world.get(entity, Agent)
    inventory = world.get(entity, Inventory)
    needs = world.get(entity, Needs)

    if inventory.food >= world.config.carry_capacity:
        return True
    if needs.hunger >= world.config.need_threshold and inventory.food > 0:
        return True

    at = message.get("content", {}).get("at")
    if not isinstance(at, list) or len(at) != 2:
        return True

    if agent.state in (AgentState.GATHER, AgentState.BUILD):
        return False

    agent.clear_target()
    agent.target_x, agent.target_y = int(at[0]), int(at[1])
    agent.state = AgentState.MEET
    return True


def _consume_inbox(world: World, entity: Entity) -> None:
    inbox = world.get(entity, Inbox)
    if not inbox.messages:
        return

    keep: list[dict] = []
    for message in inbox.messages:
        kind = message.get("type")
        if kind == MessageType.INFO.value:
            continue
        if kind in (MessageType.REQUEST.value, MessageType.ALERT.value):
            done = _handle_request(
                world, entity, message, emergency=kind == MessageType.ALERT.value
            )
        elif kind == MessageType.OFFER.value:
            done = _handle_offer(world, entity, message)
        else:
            done = True
        if not done:
            keep.append(message)

    inbox.messages[:] = keep


def _ask_for_help(world: World, entity: Entity) -> None:
    config = world.config
    agent = world.get(entity, Agent)
    needs = world.get(entity, Needs)
    inventory = world.get(entity, Inventory)

    reference = world.try_get(entity, ClanRef)
    if reference is None or reference.clan_id is None:
        return
    if inventory.food > 0 or needs.hunger >= config.need_threshold:
        return
    if agent.last_request_tick >= 0 and world.tick - agent.last_request_tick < config.request_cooldown_ticks:
        return

    agent.last_request_tick = world.tick
    message_type = MessageType.ALERT if is_starving(needs) else MessageType.REQUEST
    send(
        world,
        entity,
        make_message(
            entity, BROADCAST_CLAN, message_type, {"resource": ResourceKind.FOOD.value}
        ),
    )


def run(world: World, rng: TickRng) -> None:
    for entity in world.query(Agent, Inbox, Inventory, Needs, Position):
        _consume_inbox(world, entity)

    for entity in world.query(Agent, Inbox, Inventory, Needs, Position):
        _ask_for_help(world, entity)
