"""Clan goals, soft influence, and the member bias they exert."""

from __future__ import annotations

from collections import Counter

from src.world.components import (
    Agent,
    AgentState,
    Blackboard,
    Clan,
    ClanGoal,
    ClanRef,
    Inbox,
    Inventory,
    Needs,
    Outbox,
    Position,
    clan_centre_key,
    clan_goal_key,
)
from src.world.config import WorldConfig
from src.world.ecs import Entity, World
from src.world.rng import TickRng
from src.world.systems import social
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(seed=19, grid_width=32, grid_height=32, agent_count=30, resource_count=90)


def _world_with_clan(spots: list[tuple[int, int]], food: int = 5, hunger: int = 90):
    world = World(CONFIG)
    world.add(world.create_entity(), Blackboard())
    clan = Clan(clan_id=1)
    world.add(world.create_entity(), clan)

    members = []
    for x, y in spots:
        entity = world.create_entity()
        world.add(entity, Agent(name=f"a{entity}"))
        world.add(entity, Position(x, y))
        world.add(entity, Needs(energy=90, hunger=hunger, social=100, safety=100))
        world.add(entity, Inventory(food=food))
        world.add(entity, ClanRef(clan_id=1))
        world.add(entity, Inbox())
        world.add(entity, Outbox())
        clan.add(entity)
        members.append(entity)
    return world, clan, members


def _tick_social(world: World, ticks: int = 1) -> None:
    for _ in range(ticks):
        world.tick += 1
        social.run(world, TickRng(CONFIG.seed, world.tick, "social"))


def _board(world: World) -> Blackboard:
    return world.get(world.query(Blackboard)[0], Blackboard)


# --- influence --------------------------------------------------------------


def test_centre_is_the_mean_of_member_positions():
    world, clan, _ = _world_with_clan([(4, 4), (6, 4), (5, 7)])
    _tick_social(world)
    assert clan.centre == [5, 5]


def test_centre_is_none_when_every_member_dies():
    world, clan, members = _world_with_clan([(4, 4), (6, 4)])
    _tick_social(world)
    for member in members:
        world.destroy_entity(member)
    _tick_social(world)
    assert world.query(Clan) == []


def test_influence_radius_grows_with_membership():
    small, small_clan, _ = _world_with_clan([(5, 5), (6, 5)])
    big, big_clan, _ = _world_with_clan([(5, 5), (6, 5), (5, 6), (6, 6), (7, 5)])
    _tick_social(small)
    _tick_social(big)
    assert big_clan.influence_radius > small_clan.influence_radius


def test_influence_falls_off_with_distance():
    world, clan, _ = _world_with_clan([(10, 10), (10, 10)])
    _tick_social(world)

    at_centre = social.influence_at(clan, 10, 10)
    nearby = social.influence_at(clan, 12, 10)
    far = social.influence_at(clan, 30, 30)

    assert at_centre == 1.0
    assert 0 < nearby < at_centre
    assert far == 0.0


def test_centre_is_published_to_the_blackboard():
    world, clan, _ = _world_with_clan([(4, 4), (6, 6)])
    _tick_social(world)

    published = _board(world).read(clan_centre_key(clan.clan_id))
    assert published == [clan.centre[0], clan.centre[1], clan.influence_radius]


# --- goals ------------------------------------------------------------------


def test_hungry_clan_chooses_to_gather_food():
    world, clan, _ = _world_with_clan([(5, 5), (6, 5)], food=0, hunger=10)
    _tick_social(world)
    assert clan.goal == ClanGoal.GATHER_FOOD.value


def test_well_fed_built_out_clan_falls_back_to_rally():
    world, clan, members = _world_with_clan([(5, 5), (6, 5)], food=5, hunger=90)
    for member in members:
        world.get(member, Agent).huts_owned = CONFIG.max_huts_per_agent
    _tick_social(world)
    assert clan.goal == ClanGoal.RALLY.value


def test_fed_clan_without_huts_expands_or_gathers_wood():
    world, clan, _ = _world_with_clan([(5, 5), (6, 5)], food=5, hunger=90)
    _tick_social(world)
    assert clan.goal in (ClanGoal.EXPAND.value, ClanGoal.GATHER_WOOD.value)


def test_clan_with_wood_in_hand_expands_rather_than_gathering():
    world, clan, members = _world_with_clan([(5, 5), (6, 5)], food=5, hunger=90)
    for member in members:
        world.get(member, Inventory).wood = CONFIG.carry_capacity
    _tick_social(world)
    assert clan.goal == ClanGoal.EXPAND.value


def test_goal_is_published_to_the_blackboard():
    world, clan, _ = _world_with_clan([(5, 5), (6, 5)], food=0, hunger=10)
    _tick_social(world)
    assert _board(world).read(clan_goal_key(clan.clan_id)) == clan.goal


def test_goal_is_not_rechosen_every_tick():
    world, clan, members = _world_with_clan([(5, 5), (6, 5)], food=0, hunger=10)
    _tick_social(world)
    assert clan.goal == ClanGoal.GATHER_FOOD.value
    set_at = clan.goal_set_tick

    for member in members:
        world.get(member, Inventory).food = CONFIG.carry_capacity
        world.get(member, Needs).hunger = 100
        world.get(member, Agent).huts_owned = CONFIG.max_huts_per_agent
    _tick_social(world, 5)

    assert clan.goal == ClanGoal.GATHER_FOOD.value, "goal churned before its review window"
    assert clan.goal_set_tick == set_at


def test_goal_changes_once_the_review_window_passes():
    world, clan, members = _world_with_clan([(5, 5), (6, 5)], food=0, hunger=10)
    _tick_social(world)
    assert clan.goal == ClanGoal.GATHER_FOOD.value

    for member in members:
        world.get(member, Inventory).food = CONFIG.carry_capacity
        world.get(member, Needs).hunger = 100
        world.get(member, Agent).huts_owned = CONFIG.max_huts_per_agent
    _tick_social(world, CONFIG.goal_review_ticks + 1)

    assert clan.goal == ClanGoal.RALLY.value


# --- bias -------------------------------------------------------------------


def test_survival_still_outranks_the_clan_goal():
    """A starving member of a gather_wood clan must still go after food."""
    from src.world.systems import fsm

    world, clan, members = _world_with_clan([(5, 5), (6, 5)], food=0, hunger=0)
    clan.goal = ClanGoal.GATHER_WOOD.value
    _board(world).write(clan_goal_key(1), clan.goal, members[0], 0)

    agent = world.get(members[0], Agent)
    agent.state = AgentState.IDLE
    world.tick += 1
    fsm.run(world, TickRng(CONFIG.seed, world.tick, "fsm"))

    assert agent.state is not AgentState.REST
    assert agent.wants.value == "FOOD"


def test_goal_biases_what_a_stocked_member_forages():
    simulation = Simulation(create_world(WorldConfig()))
    simulation.run(300)
    world = simulation.world

    goals = Counter(world.get(e, Clan).goal for e in world.query(Clan))
    assert goals, "no clans formed"
    assert len(goals) >= 2, f"every clan chose the same goal: {dict(goals)}"


def test_all_four_goals_are_reachable_in_a_real_world():
    simulation = Simulation(create_world(WorldConfig()))
    seen: set[str] = set()
    for _ in range(600):
        simulation.step()
        seen.update(
            simulation.world.get(e, Clan).goal for e in simulation.world.query(Clan)
        )
    assert seen >= {g.value for g in ClanGoal}, f"never reached: {
        {g.value for g in ClanGoal} - seen
    }"


def test_clans_keep_distinct_goals_at_steady_state():
    simulation = Simulation(create_world(WorldConfig()))
    simulation.run(8000)
    goals = Counter(
        simulation.world.get(e, Clan).goal for e in simulation.world.query(Clan)
    )
    assert len(goals) >= 2, f"all clans converged on one goal: {dict(goals)}"
