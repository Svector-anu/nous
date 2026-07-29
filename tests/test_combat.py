"""Raiding: scarcity-driven, opportunistic, and paid for in energy."""

from __future__ import annotations

from src.world.components import (
    Agent,
    AgentState,
    Blackboard,
    Clan,
    ClanGoal,
    ClanRef,
    Inbox,
    Inventory,
    MessageType,
    Needs,
    Outbox,
    Position,
    ResourceKind,
    clan_goal_key,
)
from src.world.config import WorldConfig
from src.world.ecs import Entity, World
from src.world.rng import TickRng
from src.world.systems import combat, social
from src.world.tick import Simulation, create_world, state_hash

CONFIG = WorldConfig(seed=23, grid_width=32, grid_height=32, agent_count=25, resource_count=80)


def _world() -> World:
    world = World(CONFIG)
    world.add(world.create_entity(), Blackboard())
    return world


def _agent(world: World, x: int, y: int, *, food: int, energy: int, clan_id: int | None) -> Entity:
    entity = world.create_entity()
    world.add(entity, Agent(name=f"a{entity}"))
    world.add(entity, Position(x, y))
    world.add(entity, Needs(energy=energy, hunger=50, social=100, safety=100))
    world.add(entity, Inventory(food=food))
    world.add(entity, ClanRef(clan_id=clan_id))
    world.add(entity, Inbox())
    world.add(entity, Outbox())
    return entity


def _raiding_clan(world: World, clan_id: int = 1) -> Clan:
    clan = Clan(clan_id=clan_id, goal=ClanGoal.RAID.value)
    world.add(world.create_entity(), clan)
    board = world.get(world.first(Blackboard), Blackboard)
    board.write(clan_goal_key(clan_id), ClanGoal.RAID.value, 1, 0)
    return clan


def _tick_combat(world: World) -> None:
    world.tick += 1
    combat.run(world, TickRng(CONFIG.seed, world.tick, "combat"))


# --- when raiding happens ---------------------------------------------------


def test_healthy_world_never_raids():
    """The whole design intent: a world with enough food sees no violence."""
    simulation = Simulation(create_world(WorldConfig()))
    simulation.run(4000)
    world = simulation.world

    assert sum(world.get(e, Agent).raids_won for e in world.query(Agent)) == 0
    assert not [e for e in world.query(Clan) if world.get(e, Clan).goal == ClanGoal.RAID.value]


def test_genesis_hunger_does_not_trigger_raiding():
    """Nobody holds food at world start. That is startup conditions, not famine —
    keying the escalation to stores instead of hunger cost 47 agents."""
    simulation = Simulation(create_world(WorldConfig()))
    simulation.run(600)
    world = simulation.world

    raiding = [e for e in world.query(Clan) if world.get(e, Clan).goal == ClanGoal.RAID.value]
    assert not raiding, "clans raided during the opening, before any famine existed"


def test_desperate_clan_escalates_to_raiding():
    world = World(CONFIG)
    world.add(world.create_entity(), Blackboard())
    clan = Clan(clan_id=1, goal=ClanGoal.GATHER_FOOD.value)
    world.add(world.create_entity(), clan)
    for _ in range(2):
        entity = _agent(world, 5, 5, food=0, energy=80, clan_id=1)
        world.get(entity, Needs).hunger = 0
        clan.add(entity)

    assert social._choose_goal(world, clan) is ClanGoal.RAID


def test_desperate_clan_that_has_not_tried_gathering_gathers_first():
    world = World(CONFIG)
    world.add(world.create_entity(), Blackboard())
    clan = Clan(clan_id=1, goal=ClanGoal.RALLY.value)
    world.add(world.create_entity(), clan)
    for _ in range(2):
        entity = _agent(world, 5, 5, food=0, energy=80, clan_id=1)
        world.get(entity, Needs).hunger = 0
        clan.add(entity)

    assert social._choose_goal(world, clan) is ClanGoal.GATHER_FOOD


# --- resolution -------------------------------------------------------------


def test_a_won_raid_moves_food():
    """Outcome is energy-weighted and rng-decided, so this drives several encounters
    rather than betting on one draw."""
    world = _world()
    clan = _raiding_clan(world)
    raider = _agent(world, 5, 5, food=0, energy=100, clan_id=1)
    clan.add(raider)

    taken = 0
    for _ in range(12):
        world.get(raider, Needs).energy = 100
        victim = _agent(world, 6, 5, food=5, energy=40, clan_id=None)
        _tick_combat(world)
        taken = world.get(raider, Inventory).food
        if taken:
            break
        if world.is_alive(victim):
            world.destroy_entity(victim)

    assert taken > 0, "twelve raids and never once took anything"


def test_a_fight_costs_both_sides_energy():
    world = _world()
    clan = _raiding_clan(world)
    raider = _agent(world, 5, 5, food=0, energy=90, clan_id=1)
    clan.add(raider)
    victim = _agent(world, 6, 5, food=3, energy=90, clan_id=None)

    _tick_combat(world)

    survivors = [e for e in (raider, victim) if world.is_alive(e)]
    assert all(world.get(e, Needs).energy < 90 for e in survivors)


def test_food_is_conserved_when_both_survive():
    """A raid moves food, it never mints or burns it. Note that death is different: an
    agent's carry dies with it, as it always has for starvation."""
    world = _world()
    clan = _raiding_clan(world)
    raider = _agent(world, 5, 5, food=1, energy=100, clan_id=1)
    clan.add(raider)
    victim = _agent(world, 6, 5, food=4, energy=100, clan_id=None)

    _tick_combat(world)

    assert world.is_alive(raider) and world.is_alive(victim)
    assert world.get(raider, Inventory).food + world.get(victim, Inventory).food == 5


def test_combat_kills_through_the_energy_rule():
    world = _world()
    clan = _raiding_clan(world)
    raider = _agent(world, 5, 5, food=0, energy=100, clan_id=1)
    clan.add(raider)
    victim = _agent(world, 6, 5, food=3, energy=1, clan_id=None)

    _tick_combat(world)

    assert not world.is_alive(victim), "a fight should be able to kill an exhausted agent"


def test_loser_flees():
    world = _world()
    clan = _raiding_clan(world)
    raider = _agent(world, 5, 5, food=0, energy=100, clan_id=1)
    clan.add(raider)
    victim = _agent(world, 6, 5, food=3, energy=60, clan_id=None)

    _tick_combat(world)

    if world.is_alive(victim):
        agent = world.get(victim, Agent)
        assert agent.state is AgentState.FLEE
        assert agent.target_x is not None


def test_flight_heads_away_from_the_winner():
    world = _world()
    clan = _raiding_clan(world)
    raider = _agent(world, 5, 5, food=0, energy=100, clan_id=1)
    clan.add(raider)
    victim = _agent(world, 6, 5, food=3, energy=60, clan_id=None)

    _tick_combat(world)

    if world.is_alive(victim) and world.get(victim, Agent).state is AgentState.FLEE:
        assert world.get(victim, Agent).target_x > 6, "fled toward the attacker"


def test_flight_terminates():
    """A flight targets a fixed point, not a moving pursuer, so it always ends."""
    simulation = Simulation(create_world(CONFIG))
    world = simulation.world
    entity = world.query(Agent)[0]
    agent = world.get(entity, Agent)
    agent.state = AgentState.FLEE
    agent.target_x, agent.target_y = 0, 0
    world.get(entity, Position).x = 6
    world.get(entity, Position).y = 6

    simulation.run(40)

    assert not world.is_alive(entity) or world.get(entity, Agent).state is not AgentState.FLEE


def test_victim_raises_an_alert():
    world = _world()
    clan = _raiding_clan(world)
    raider = _agent(world, 5, 5, food=0, energy=100, clan_id=1)
    clan.add(raider)
    victim_clan = Clan(clan_id=2)
    world.add(world.create_entity(), victim_clan)
    victim = _agent(world, 6, 5, food=3, energy=90, clan_id=2)
    victim_clan.add(victim)

    _tick_combat(world)

    if world.is_alive(victim):
        alerts = [
            m for m in world.get(victim, Outbox).messages
            if m["type"] == MessageType.ALERT.value
        ]
        assert alerts
        assert alerts[0]["content"]["reason"] == "raid"


# --- what raiders will not do -----------------------------------------------


def test_clanmates_are_never_robbed():
    world = _world()
    clan = _raiding_clan(world)
    raider = _agent(world, 5, 5, food=0, energy=100, clan_id=1)
    mate = _agent(world, 6, 5, food=5, energy=100, clan_id=1)
    clan.add(raider)
    clan.add(mate)

    _tick_combat(world)

    assert world.get(mate, Inventory).food == 5
    assert world.get(raider, Inventory).food == 0


def test_agents_without_food_are_not_attacked():
    world = _world()
    clan = _raiding_clan(world)
    raider = _agent(world, 5, 5, food=0, energy=100, clan_id=1)
    clan.add(raider)
    bystander = _agent(world, 6, 5, food=0, energy=100, clan_id=None)

    _tick_combat(world)

    assert world.get(bystander, Needs).energy == 100, "attacked someone carrying nothing"


def test_raiders_do_not_pursue():
    """No chase, by construction — this is what makes the obvious livelock impossible."""
    world = _world()
    clan = _raiding_clan(world)
    raider = _agent(world, 0, 0, food=0, energy=100, clan_id=1)
    clan.add(raider)
    distant = _agent(world, 25, 25, food=5, energy=100, clan_id=None)

    _tick_combat(world)

    agent = world.get(raider, Agent)
    assert world.get(distant, Inventory).food == 5
    assert (agent.target_x, agent.target_y) == (None, None), "raider set off after a target"


def test_non_raiding_clans_do_not_rob():
    world = _world()
    clan = Clan(clan_id=1, goal=ClanGoal.RALLY.value)
    world.add(world.create_entity(), clan)
    board = world.get(world.first(Blackboard), Blackboard)
    board.write(clan_goal_key(1), ClanGoal.RALLY.value, 1, 0)
    peaceful = _agent(world, 5, 5, food=0, energy=100, clan_id=1)
    clan.add(peaceful)
    neighbour = _agent(world, 6, 5, food=5, energy=100, clan_id=None)

    _tick_combat(world)

    assert world.get(neighbour, Inventory).food == 5


def test_clanless_agents_do_not_raid():
    world = _world()
    loner = _agent(world, 5, 5, food=0, energy=100, clan_id=None)
    neighbour = _agent(world, 6, 5, food=5, energy=100, clan_id=None)

    _tick_combat(world)

    assert world.get(neighbour, Inventory).food == 5


def test_a_full_raider_stops_robbing():
    world = _world()
    clan = _raiding_clan(world)
    raider = _agent(world, 5, 5, food=CONFIG.carry_capacity, energy=100, clan_id=1)
    clan.add(raider)
    victim = _agent(world, 6, 5, food=5, energy=100, clan_id=None)

    _tick_combat(world)

    assert world.get(victim, Inventory).food == 5


# --- determinism ------------------------------------------------------------


def test_raiding_is_deterministic():
    from src.world.components import ResourceNode

    def run() -> str:
        simulation = Simulation(create_world(WorldConfig()))
        world = simulation.world
        food = [
            e for e in world.query(ResourceNode)
            if world.get(e, ResourceNode).kind is ResourceKind.FOOD
        ]
        simulation.run(1500)
        while world.tick < 2100:
            simulation.step()
            for entity in food[: int(len(food) * 0.8)]:
                if world.is_alive(entity):
                    world.get(entity, ResourceNode).amount = 0
        return state_hash(world)

    assert run() == run()
