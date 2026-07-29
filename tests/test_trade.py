"""Resource transfer, and agents acting on the messages they receive."""

from __future__ import annotations

from src.world.components import (
    BROADCAST_CLAN,
    Agent,
    AgentState,
    Blackboard,
    Clan,
    ClanRef,
    Inbox,
    Inventory,
    MessageType,
    Needs,
    Outbox,
    Position,
    ResourceKind,
)
from src.world.config import WorldConfig
from src.world.ecs import Entity, World
from src.world.rng import TickRng
from src.world.systems import messaging, trade
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(seed=6, grid_width=32, grid_height=32, agent_count=30, resource_count=90)


def _agent(world: World, x: int, y: int, food: int = 0, hunger: int = 90, clan_id: int | None = None) -> Entity:
    entity = world.create_entity()
    world.add(entity, Agent(name=f"a{entity}"))
    world.add(entity, Position(x, y))
    world.add(entity, Needs(energy=90, hunger=hunger, social=100, safety=100))
    world.add(entity, Inventory(food=food))
    world.add(entity, ClanRef(clan_id=clan_id))
    world.add(entity, Inbox())
    world.add(entity, Outbox())
    return entity


def _clanned_pair(world: World, ax: int, ay: int, bx: int, by: int, donor_food: int, asker_hunger: int):
    clan = Clan(clan_id=1)
    world.add(world.create_entity(), clan)
    donor = _agent(world, ax, ay, food=donor_food, clan_id=1)
    asker = _agent(world, bx, by, food=0, hunger=asker_hunger, clan_id=1)
    clan.add(donor)
    clan.add(asker)
    return clan, donor, asker


def _tick(world: World) -> None:
    world.tick += 1
    messaging.run(world, TickRng(CONFIG.seed, world.tick, "messaging"))
    trade.run(world, TickRng(CONFIG.seed, world.tick, "trade"))


# --- transfer ---------------------------------------------------------------


def test_transfer_moves_the_resource():
    world = World(CONFIG)
    donor = _agent(world, 0, 0, food=4)
    receiver = _agent(world, 0, 1, food=0)

    moved = trade.transfer(world, donor, receiver, ResourceKind.FOOD, 3)

    assert moved == 3
    assert world.get(donor, Inventory).food == 1
    assert world.get(receiver, Inventory).food == 3


def test_transfer_conserves_total():
    world = World(CONFIG)
    donor = _agent(world, 0, 0, food=5)
    receiver = _agent(world, 0, 1, food=1)
    before = 5 + 1

    trade.transfer(world, donor, receiver, ResourceKind.FOOD, 5)

    after = world.get(donor, Inventory).food + world.get(receiver, Inventory).food
    assert after == before


def test_transfer_respects_receiver_capacity():
    world = World(CONFIG)
    donor = _agent(world, 0, 0, food=5)
    receiver = _agent(world, 0, 1, food=CONFIG.carry_capacity - 1)

    moved = trade.transfer(world, donor, receiver, ResourceKind.FOOD, 5)

    assert moved == 1
    assert world.get(receiver, Inventory).food == CONFIG.carry_capacity


def test_transfer_cannot_overdraw_the_donor():
    world = World(CONFIG)
    donor = _agent(world, 0, 0, food=2)
    receiver = _agent(world, 0, 1, food=0)

    moved = trade.transfer(world, donor, receiver, ResourceKind.FOOD, 5)

    assert moved == 2
    assert world.get(donor, Inventory).food == 0


def test_transfer_works_for_wood_too():
    world = World(CONFIG)
    donor = _agent(world, 0, 0)
    receiver = _agent(world, 0, 1)
    world.get(donor, Inventory).wood = 3

    moved = trade.transfer(world, donor, receiver, ResourceKind.WOOD, 3)

    assert moved == 3
    assert world.get(receiver, Inventory).wood == 3


def test_transfer_to_a_full_receiver_moves_nothing():
    world = World(CONFIG)
    donor = _agent(world, 0, 0, food=5)
    receiver = _agent(world, 0, 1, food=CONFIG.carry_capacity)

    assert trade.transfer(world, donor, receiver, ResourceKind.FOOD, 5) == 0
    assert world.get(donor, Inventory).food == 5


def test_transfer_to_self_is_a_noop():
    world = World(CONFIG)
    agent = _agent(world, 0, 0, food=3)
    assert trade.transfer(world, agent, agent, ResourceKind.FOOD, 2) == 0
    assert world.get(agent, Inventory).food == 3


def test_transfer_to_a_dead_agent_moves_nothing():
    world = World(CONFIG)
    donor = _agent(world, 0, 0, food=3)
    receiver = _agent(world, 0, 1)
    world.destroy_entity(receiver)

    assert trade.transfer(world, donor, receiver, ResourceKind.FOOD, 3) == 0
    assert world.get(donor, Inventory).food == 3


# --- acting on messages -----------------------------------------------------


def test_hungry_clanless_agent_does_not_beg():
    world = World(CONFIG)
    asker = _agent(world, 0, 0, food=0, hunger=5)
    _tick(world)
    assert world.get(asker, Outbox).messages == []


def test_hungry_agent_requests_from_its_clan():
    world = World(CONFIG)
    _, _, asker = _clanned_pair(world, 0, 0, 10, 10, donor_food=5, asker_hunger=5)

    world.tick += 1
    trade.run(world, TickRng(CONFIG.seed, world.tick, "trade"))

    sent = world.get(asker, Outbox).messages
    assert len(sent) == 1
    assert sent[0]["to"] == BROADCAST_CLAN
    assert sent[0]["type"] in (MessageType.REQUEST.value, MessageType.ALERT.value)


def test_starving_agent_raises_an_alert_not_a_request():
    world = World(CONFIG)
    _, _, asker = _clanned_pair(world, 0, 0, 10, 10, donor_food=5, asker_hunger=0)

    world.tick += 1
    trade.run(world, TickRng(CONFIG.seed, world.tick, "trade"))

    assert world.get(asker, Outbox).messages[0]["type"] == MessageType.ALERT.value


def test_nearby_clanmate_hands_food_over():
    world = World(CONFIG)
    _, donor, asker = _clanned_pair(world, 5, 5, 6, 5, donor_food=5, asker_hunger=5)

    _tick(world)  # asker requests
    _tick(world)  # donor reads and gives

    assert world.get(asker, Inventory).food > 0
    assert world.get(donor, Inventory).food < 5
    assert world.get(asker, Agent).received > 0


def test_donor_keeps_its_reserve_for_a_plain_request():
    world = World(CONFIG)
    _, donor, _ = _clanned_pair(world, 5, 5, 6, 5, donor_food=5, asker_hunger=5)

    _tick(world)
    _tick(world)

    assert world.get(donor, Inventory).food >= CONFIG.surplus_reserve


def test_alert_makes_a_donor_dip_into_its_reserve():
    world = World(CONFIG)
    _, donor, _ = _clanned_pair(world, 5, 5, 6, 5, donor_food=CONFIG.surplus_reserve, asker_hunger=0)

    _tick(world)
    _tick(world)

    assert world.get(donor, Inventory).food < CONFIG.surplus_reserve


def test_distant_clanmate_replies_with_an_offer():
    world = World(CONFIG)
    _, donor, asker = _clanned_pair(world, 0, 0, 20, 20, donor_food=5, asker_hunger=5)

    _tick(world)
    _tick(world)

    offers = [m for m in world.get(donor, Outbox).messages if m["type"] == MessageType.OFFER.value]
    assert offers, "a far-away donor should offer rather than teleport food"
    assert offers[0]["content"]["at"] == [0, 0]


def test_an_offer_sends_the_asker_walking():
    world = World(CONFIG)
    _, _, asker = _clanned_pair(world, 0, 0, 20, 20, donor_food=5, asker_hunger=5)

    for _ in range(4):
        _tick(world)

    agent = world.get(asker, Agent)
    assert agent.state is AgentState.MEET
    assert (agent.target_x, agent.target_y) == (0, 0)


def test_outsiders_are_ignored():
    world = World(CONFIG)
    donor = _agent(world, 5, 5, food=5, clan_id=1)
    clan = Clan(clan_id=1)
    world.add(world.create_entity(), clan)
    clan.add(donor)
    outsider = _agent(world, 6, 5, food=0, hunger=0, clan_id=None)

    messaging.send(
        world,
        outsider,
        messaging.make_message(outsider, donor, MessageType.ALERT, {"resource": "FOOD"}),
    )
    _tick(world)

    assert world.get(outsider, Inventory).food == 0
    assert world.get(donor, Inventory).food == 5


def test_requests_are_rate_limited():
    world = World(CONFIG)
    _, _, asker = _clanned_pair(world, 0, 0, 20, 20, donor_food=0, asker_hunger=5)

    for _ in range(CONFIG.request_cooldown_ticks - 1):
        world.tick += 1
        trade.run(world, TickRng(CONFIG.seed, world.tick, "trade"))

    asks = [m for m in world.get(asker, Outbox).messages if m["type"] != MessageType.OFFER.value]
    assert len(asks) == 1, f"spammed {len(asks)} requests inside the cooldown"


def test_unactionable_message_stays_in_the_inbox():
    """A donor with nothing to give keeps the request for when it has food again."""
    world = World(CONFIG)
    _, donor, _ = _clanned_pair(world, 5, 5, 6, 5, donor_food=0, asker_hunger=5)

    _tick(world)
    _tick(world)

    assert world.get(donor, Inbox).messages, "request was thrown away instead of held"


def test_food_changes_hands_in_a_running_world():
    simulation = Simulation(create_world(WorldConfig()))
    simulation.run(2000)
    world = simulation.world

    receivers = [e for e in world.query(Agent) if world.get(e, Agent).received > 0]
    assert receivers, "no food was ever shared between agents"


def test_sharing_does_not_create_or_destroy_food():
    """Total food in the world may only change through gathering and eating."""
    world = World(CONFIG)
    _, donor, asker = _clanned_pair(world, 5, 5, 6, 5, donor_food=5, asker_hunger=90)
    before = world.get(donor, Inventory).food + world.get(asker, Inventory).food

    for _ in range(10):
        _tick(world)

    after = world.get(donor, Inventory).food + world.get(asker, Inventory).food
    assert after == before
