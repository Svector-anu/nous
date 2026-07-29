"""Truces: mutual, durable, and the one thing that outranks a grudge."""

from __future__ import annotations

from src.persistence.sqlite_store import SqliteWorldStore
from src.world.components import (
    Agent,
    Blackboard,
    Clan,
    ClanGoal,
    ClanRef,
    Inbox,
    Inventory,
    Needs,
    Outbox,
    Position,
    clan_goal_key,
)
from src.world.config import WorldConfig
from src.world.ecs import Entity, World
from src.world.rng import TickRng
from src.world.systems import combat, social
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(seed=51, grid_width=32, grid_height=32, agent_count=30, resource_count=90)
FED = 90


def _world(tick: int = 5000) -> World:
    world = World(CONFIG)
    world.add(world.create_entity(), Blackboard())
    world.tick = tick
    return world


def _clan(world: World, clan_id: int, *, at: tuple[int, int], raiding: bool = False) -> Clan:
    clan = Clan(clan_id=clan_id, goal=ClanGoal.RAID.value if raiding else ClanGoal.RALLY.value)
    world.add(world.create_entity(), clan)
    board = world.get(world.first(Blackboard), Blackboard)
    board.write(clan_goal_key(clan_id), clan.goal, 1, 0)

    for offset in range(2):
        entity = world.create_entity()
        world.add(entity, Agent(name=f"c{clan_id}a{offset}"))
        world.add(entity, Position(at[0] + offset, at[1]))
        world.add(entity, Needs(energy=100, hunger=FED, social=100, safety=100))
        world.add(entity, Inventory(food=3))
        world.add(entity, ClanRef(clan_id=clan_id))
        world.add(entity, Inbox())
        world.add(entity, Outbox())
        clan.add(entity)

    clan.centre = list(at)
    clan.influence_radius = 6
    return clan


def _feuding_pair(world: World, *, last_blow: int = 0) -> tuple[Clan, Clan]:
    a = _clan(world, 1, at=(10, 10))
    b = _clan(world, 2, at=(16, 10))
    a.record_raid(2, tick=last_blow)
    b.record_raid(1, tick=last_blow)
    return a, b


def _try_truce(world: World) -> None:
    social._form_truces(world)


# --- the data shape ---------------------------------------------------------


def test_a_new_clan_has_no_allies():
    clan = Clan(clan_id=1)
    assert clan.allies == []
    assert clan.is_allied(2) is False
    assert clan.is_allied(None) is False


def test_adding_an_ally_is_idempotent_and_sorted():
    clan = Clan(clan_id=1)
    for other in (5, 2, 5, 9, 2):
        clan.add_ally(other)
    assert clan.allies == [2, 5, 9]


# --- formation --------------------------------------------------------------


def test_a_feud_that_has_cooled_becomes_a_truce():
    world = _world()
    a, b = _feuding_pair(world, last_blow=0)  # long ago relative to tick 5000

    _try_truce(world)

    assert a.is_allied(2) and b.is_allied(1), "a truce must be recorded on both sides"


def test_no_truce_without_a_mutual_grudge():
    """A one-sided raid is not a feud."""
    world = _world()
    a = _clan(world, 1, at=(10, 10))
    b = _clan(world, 2, at=(16, 10))
    a.record_raid(2, tick=0)  # only one side was robbed

    _try_truce(world)

    assert a.allies == [] and b.allies == []


def test_no_truce_between_strangers():
    world = _world()
    a = _clan(world, 1, at=(10, 10))
    b = _clan(world, 2, at=(16, 10))

    _try_truce(world)

    assert a.allies == [] and b.allies == []


def test_no_truce_while_a_clan_is_desperate():
    world = _world()
    a, b = _feuding_pair(world, last_blow=0)
    for member in a.members:
        world.get(member, Needs).hunger = 0

    _try_truce(world)

    assert a.allies == [], "a starving clan should not be making peace"


def test_no_truce_while_the_fighting_is_recent():
    world = _world(tick=100)
    a, b = _feuding_pair(world, last_blow=50)  # only 50 ticks of quiet

    _try_truce(world)

    assert a.allies == []


def test_a_truce_forms_once_the_peace_has_held():
    world = _world(tick=100)
    a, b = _feuding_pair(world, last_blow=50)
    _try_truce(world)
    assert a.allies == []

    world.tick = 50 + CONFIG.truce_peace_ticks + 1
    _try_truce(world)
    assert a.is_allied(2)


def test_no_truce_between_distant_clans():
    world = _world()
    a = _clan(world, 1, at=(2, 2))
    b = _clan(world, 2, at=(30, 30))
    a.record_raid(2, tick=0)
    b.record_raid(1, tick=0)

    _try_truce(world)

    assert a.allies == [], "clans with no contact have nothing to negotiate"


def test_forming_a_truce_is_idempotent():
    world = _world()
    a, b = _feuding_pair(world, last_blow=0)
    for _ in range(5):
        _try_truce(world)

    assert a.allies == [2] and b.allies == [1]


def test_truce_formation_is_deterministic():
    def run() -> list[tuple[int, list[int]]]:
        world = _world()
        _feuding_pair(world, last_blow=0)
        c = _clan(world, 3, at=(13, 14))
        c.record_raid(1, tick=0)
        world.get(world.query(Clan)[0], Clan).record_raid(3, tick=0)
        _try_truce(world)
        return sorted(
            (world.get(e, Clan).clan_id, list(world.get(e, Clan).allies))
            for e in world.query(Clan)
        )

    assert run() == run()


def test_grudges_are_kept_after_a_truce():
    """The memory of the raid outlives the fighting."""
    world = _world()
    a, b = _feuding_pair(world, last_blow=0)
    _try_truce(world)

    assert a.grudge_against(2) == 1
    assert b.grudge_against(1) == 1


# --- durability -------------------------------------------------------------


def test_allies_survive_save_and_reload(tmp_path):
    store = SqliteWorldStore(tmp_path / "truce.db")
    simulation = Simulation(create_world(CONFIG))
    simulation.run(300)
    world = simulation.world

    clans = [world.get(e, Clan) for e in world.query(Clan)]
    assert len(clans) >= 2
    clans[0].add_ally(clans[1].clan_id)
    clans[1].add_ally(clans[0].clan_id)
    expected = {c.clan_id: list(c.allies) for c in clans}

    store.save(world)
    store.close()

    loaded = SqliteWorldStore(tmp_path / "truce.db").load()
    actual = {
        loaded.get(e, Clan).clan_id: list(loaded.get(e, Clan).allies)
        for e in loaded.query(Clan)
    }
    assert actual == expected


# --- the behavioural effect -------------------------------------------------


def test_allies_are_never_raided():
    world = _world()
    raiders = _clan(world, 1, at=(10, 10), raiding=True)
    ally = _clan(world, 2, at=(11, 10))
    raiders.add_ally(2)
    ally.add_ally(1)

    raider = raiders.members[0]
    world.get(raider, Inventory).food = 0

    positions = {e: world.get(e, Position) for e in world.query(Agent, Position)}
    assert combat._victim(world, raider, positions) is None


def test_a_truce_outranks_a_grudge():
    """Allied *and* still resented: the truce wins, and a stranger is robbed instead."""
    world = _world()
    raiders = _clan(world, 1, at=(10, 10), raiding=True)
    resented_ally = _clan(world, 2, at=(11, 10))
    stranger = _clan(world, 3, at=(12, 10))

    for _ in range(5):
        raiders.record_raid(2, tick=0)  # deep grudge against the ally
    raiders.add_ally(2)
    resented_ally.add_ally(1)

    raider = raiders.members[0]
    world.get(raider, Inventory).food = 0

    positions = {e: world.get(e, Position) for e in world.query(Agent, Position)}
    victim = combat._victim(world, raider, positions)

    assert victim is not None
    assert world.get(victim, ClanRef).clan_id == 3


def test_grudge_targeting_still_applies_to_non_allies():
    world = _world()
    raiders = _clan(world, 1, at=(10, 10), raiding=True)
    ally = _clan(world, 2, at=(11, 10))
    enemy = _clan(world, 3, at=(12, 10))
    raiders.add_ally(2)
    ally.add_ally(1)
    raiders.record_raid(3, tick=0)

    raider = raiders.members[0]
    world.get(raider, Inventory).food = 0

    positions = {e: world.get(e, Position) for e in world.query(Agent, Position)}
    victim = combat._victim(world, raider, positions)

    assert world.get(victim, ClanRef).clan_id == 3


def test_a_truce_does_not_stop_a_clan_raiding_anyone_else():
    world = _world()
    raiders = _clan(world, 1, at=(10, 10), raiding=True)
    ally = _clan(world, 2, at=(11, 10))
    outsider = _clan(world, 3, at=(11, 11))
    raiders.add_ally(2)
    ally.add_ally(1)

    raider = raiders.members[0]
    world.get(raider, Inventory).food = 0

    positions = {e: world.get(e, Position) for e in world.query(Agent, Position)}
    victim = combat._victim(world, raider, positions)

    assert victim is not None
    assert world.get(victim, ClanRef).clan_id == 3


def test_survival_is_untouched_by_alliances():
    simulation = Simulation(create_world(CONFIG))
    simulation.run(600)
    world = simulation.world

    assert world.query(Agent), "the world should still be alive"
    for entity in world.query(Agent, Needs):
        needs = world.get(entity, Needs)
        assert 0 < needs.energy <= CONFIG.need_max
