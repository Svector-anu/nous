"""Clan formation, size caps, leadership succession, and clustering."""

from __future__ import annotations

import statistics

from src.world.components import Agent, AgentState, Clan, ClanRef, Inventory, Needs, Position
from src.world.config import WorldConfig
from src.world.ecs import Entity, World
from src.world.rng import TickRng
from src.world.systems import social
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(seed=12, grid_width=32, grid_height=32, agent_count=30, resource_count=90)


def _idle_agents_at(world: World, spots: list[tuple[int, int]]) -> list[Entity]:
    entities = []
    for index, (x, y) in enumerate(spots):
        entity = world.create_entity()
        world.add(entity, Agent(name=f"a{index}", state=AgentState.IDLE))
        world.add(entity, Position(x, y))
        world.add(entity, ClanRef())
        world.add(entity, Needs(energy=90, hunger=90, social=100, safety=100))
        world.add(entity, Inventory())
        entities.append(entity)
    return entities


def _run_social(world: World, ticks: int) -> None:
    for _ in range(ticks):
        world.tick += 1
        social.run(world, TickRng(CONFIG.seed, world.tick, "social"))


def test_two_nearby_idle_agents_form_a_clan():
    world = World(CONFIG)
    a, b = _idle_agents_at(world, [(5, 5), (6, 5)])
    _run_social(world, 60)

    assert world.get(a, ClanRef).clan_id is not None
    assert world.get(a, ClanRef).clan_id == world.get(b, ClanRef).clan_id


def test_distant_agents_do_not_form_a_clan():
    world = World(CONFIG)
    a, b = _idle_agents_at(world, [(0, 0), (30, 30)])
    _run_social(world, 60)

    assert world.get(a, ClanRef).clan_id is None
    assert world.get(b, ClanRef).clan_id is None


def test_clan_records_its_members_and_a_leader():
    world = World(CONFIG)
    a, b = _idle_agents_at(world, [(5, 5), (6, 5)])
    _run_social(world, 60)

    clan = world.get(world.query(Clan)[0], Clan)
    assert sorted(clan.members) == sorted([a, b])
    assert clan.leader in clan.members


def test_clan_size_is_capped():
    world = World(CONFIG)
    _idle_agents_at(world, [(5 + i % 3, 5 + i // 3) for i in range(20)])
    _run_social(world, 200)

    for entity in world.query(Clan):
        assert len(world.get(entity, Clan).members) <= CONFIG.max_clan_size


def test_membership_is_recorded_on_the_agent():
    simulation = Simulation(create_world(CONFIG))
    simulation.run(500)
    world = simulation.world

    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        for member in clan.members:
            assert world.get(member, ClanRef).clan_id == clan.clan_id


def test_no_agent_belongs_to_two_clans():
    simulation = Simulation(create_world(CONFIG))
    simulation.run(500)
    world = simulation.world

    seen: set[Entity] = set()
    for entity in world.query(Clan):
        for member in world.get(entity, Clan).members:
            assert member not in seen, f"agent {member} is in two clans"
            seen.add(member)


def test_leadership_passes_to_the_lowest_surviving_id():
    world = World(CONFIG)
    a, b, c = _idle_agents_at(world, [(5, 5), (6, 5), (5, 6)])
    clan = Clan(clan_id=1)
    world.add(world.create_entity(), clan)
    for member in (a, b, c):
        clan.add(member)
        world.get(member, ClanRef).clan_id = 1
    assert clan.leader == a

    world.destroy_entity(a)
    _run_social(world, 1)

    assert clan.leader == b
    assert a not in clan.members


def test_empty_clans_are_cleaned_up():
    world = World(CONFIG)
    a, b = _idle_agents_at(world, [(5, 5), (6, 5)])
    clan = Clan(clan_id=1)
    world.add(world.create_entity(), clan)
    for member in (a, b):
        clan.add(member)
        world.get(member, ClanRef).clan_id = 1

    world.destroy_entity(a)
    world.destroy_entity(b)
    _run_social(world, 1)

    assert world.query(Clan) == []


def test_clans_actually_form_in_a_running_world():
    simulation = Simulation(create_world(CONFIG))
    simulation.run(600)
    world = simulation.world

    clans = world.query(Clan)
    assert clans, "no clans formed in 600 ticks"
    assert any(len(world.get(e, Clan).members) >= 2 for e in clans)


def test_clan_members_cluster_together():
    """The visible payoff: members end up near each other, not scattered."""
    simulation = Simulation(create_world(WorldConfig()))
    simulation.run(3000)
    world = simulation.world

    spreads = []
    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        points = [
            (world.get(m, Position).x, world.get(m, Position).y)
            for m in clan.members
            if world.is_alive(m)
        ]
        if len(points) < 2:
            continue
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        spreads.append(
            statistics.mean(((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5 for p in points)
        )

    assert spreads, "no multi-member clans to measure"
    scattered = WorldConfig().grid_width * 0.38
    assert statistics.mean(spreads) < scattered / 3, "clan members are no closer than random"
