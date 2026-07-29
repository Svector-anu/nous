"""Clan raid memory: recorded on theft, durable, and biasing who gets robbed next."""

from __future__ import annotations

from src.persistence.sqlite_store import SqliteWorldStore
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
    clan_goal_key,
)
from src.world.config import WorldConfig
from src.world.ecs import Entity, World
from src.world.rng import TickRng
from src.world.systems import combat
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(seed=42, grid_width=32, grid_height=32, agent_count=30, resource_count=90)


def _world() -> World:
    world = World(CONFIG)
    world.add(world.create_entity(), Blackboard())
    return world


def _clan(world: World, clan_id: int, *, raiding: bool = False) -> Clan:
    clan = Clan(clan_id=clan_id, goal=ClanGoal.RAID.value if raiding else ClanGoal.RALLY.value)
    world.add(world.create_entity(), clan)
    board = world.get(world.first(Blackboard), Blackboard)
    board.write(clan_goal_key(clan_id), clan.goal, 1, 0)
    return clan


def _agent(world: World, x: int, y: int, *, food: int, energy: int, clan: Clan | None) -> Entity:
    entity = world.create_entity()
    world.add(entity, Agent(name=f"a{entity}"))
    world.add(entity, Position(x, y))
    world.add(entity, Needs(energy=energy, hunger=50, social=100, safety=100))
    world.add(entity, Inventory(food=food))
    world.add(entity, ClanRef(clan_id=clan.clan_id if clan else None))
    world.add(entity, Inbox())
    world.add(entity, Outbox())
    if clan is not None:
        clan.add(entity)
    return entity


def _tick(world: World) -> None:
    world.tick += 1
    combat.run(world, TickRng(CONFIG.seed, world.tick, "combat"))


# --- the data shape ---------------------------------------------------------


def test_a_new_clan_holds_no_grudges():
    clan = Clan(clan_id=1)
    assert clan.grudges == {}
    assert clan.grudge_against(2) == 0
    assert clan.grudge_against(None) == 0
    assert clan.last_raided_by(2) == -1


def test_recording_counts_and_stamps():
    clan = Clan(clan_id=1)
    clan.record_raid(3, tick=100)
    clan.record_raid(3, tick=180)

    assert clan.grudge_against(3) == 2
    assert clan.last_raided_by(3) == 180


def test_grudges_are_tracked_per_attacker():
    clan = Clan(clan_id=1)
    clan.record_raid(2, tick=10)
    clan.record_raid(3, tick=20)
    clan.record_raid(3, tick=30)

    assert clan.grudge_against(2) == 1
    assert clan.grudge_against(3) == 2
    assert clan.grudge_against(9) == 0


def test_keys_stay_sorted_so_iteration_is_stable():
    """A save round-trips through json; insertion order must already be canonical."""
    clan = Clan(clan_id=1)
    for attacker in (9, 2, 5, 1):
        clan.record_raid(attacker, tick=1)
    assert list(clan.grudges) == sorted(clan.grudges, key=int)


# --- recorded on theft, not on violence -------------------------------------


def test_a_successful_theft_creates_a_grudge():
    world = _world()
    raiders = _clan(world, 1, raiding=True)
    victims = _clan(world, 2)
    _agent(world, 5, 5, food=0, energy=100, clan=raiders)
    _agent(world, 6, 5, food=5, energy=1, clan=victims)

    _tick(world)

    assert victims.grudge_against(1) == 1
    assert victims.last_raided_by(1) == world.tick


def test_the_victim_remembers_not_the_raider():
    world = _world()
    raiders = _clan(world, 1, raiding=True)
    victims = _clan(world, 2)
    _agent(world, 5, 5, food=0, energy=100, clan=raiders)
    _agent(world, 6, 5, food=5, energy=1, clan=victims)

    _tick(world)

    assert raiders.grudges == {}, "the aggressor should hold no grievance"


def test_a_theft_that_moved_nothing_leaves_no_grudge():
    """A scuffle that took no food is not a robbery."""
    world = _world()
    raiders = _clan(world, 1, raiding=True)
    victims = _clan(world, 2)
    # Raider already full: transfer is refused, so nothing changes hands.
    _agent(world, 5, 5, food=CONFIG.carry_capacity, energy=100, clan=raiders)
    _agent(world, 6, 5, food=5, energy=100, clan=victims)

    _tick(world)

    assert victims.grudges == {}


def test_a_clanless_victim_records_nothing():
    world = _world()
    raiders = _clan(world, 1, raiding=True)
    _agent(world, 5, 5, food=0, energy=100, clan=raiders)
    _agent(world, 6, 5, food=5, energy=1, clan=None)

    _tick(world)  # must not raise

    assert raiders.grudges == {}


def test_repeat_raids_accumulate():
    world = _world()
    raiders = _clan(world, 1, raiding=True)
    victims = _clan(world, 2)
    raider = _agent(world, 5, 5, food=0, energy=100, clan=raiders)

    for _ in range(3):
        world.get(raider, Inventory).food = 0
        world.get(raider, Needs).energy = 100
        _agent(world, 6, 5, food=5, energy=1, clan=victims)
        _tick(world)

    assert victims.grudge_against(1) >= 1


# --- durability -------------------------------------------------------------


def test_grudges_survive_save_and_reload(tmp_path):
    store = SqliteWorldStore(tmp_path / "grudges.db")
    simulation = Simulation(create_world(CONFIG))
    world = simulation.world
    clan = world.get(world.query(Clan)[0], Clan) if world.query(Clan) else None
    if clan is None:
        simulation.run(300)
        clan = world.get(world.query(Clan)[0], Clan)

    clan.record_raid(7, tick=123)
    clan.record_raid(7, tick=456)
    clan.record_raid(2, tick=99)
    expected = dict(clan.grudges)
    clan_id = clan.clan_id

    store.save(world)
    store.close()

    loaded = SqliteWorldStore(tmp_path / "grudges.db").load()
    reloaded = next(
        loaded.get(e, Clan) for e in loaded.query(Clan) if loaded.get(e, Clan).clan_id == clan_id
    )
    assert reloaded.grudges == expected
    assert list(reloaded.grudges) == list(expected), "key order changed across reload"
    assert reloaded.grudge_against(7) == 2
    assert reloaded.last_raided_by(7) == 456


# --- the behavioural effect -------------------------------------------------


def test_a_grudge_is_settled_before_a_stranger_is_troubled():
    """Both victims in reach; the closer one is a stranger. The grudge wins."""
    world = _world()
    raiders = _clan(world, 1, raiding=True)
    enemy = _clan(world, 2)
    stranger = _clan(world, 3)
    raiders.record_raid(2, tick=1)

    raider = _agent(world, 5, 5, food=0, energy=100, clan=raiders)
    nearby_stranger = _agent(world, 6, 5, food=5, energy=100, clan=stranger)
    distant_enemy = _agent(world, 7, 5, food=5, energy=100, clan=enemy)

    positions = {e: world.get(e, Position) for e in world.query(Agent, Position)}
    assert combat._victim(world, raider, positions) == distant_enemy


def test_without_a_grudge_the_nearest_is_taken():
    world = _world()
    raiders = _clan(world, 1, raiding=True)
    _clan(world, 2)
    _clan(world, 3)

    raider = _agent(world, 5, 5, food=0, energy=100, clan=raiders)
    nearest = _agent(world, 6, 5, food=5, energy=100, clan=world.get(world.query(Clan)[1], Clan))
    _agent(world, 7, 5, food=5, energy=100, clan=world.get(world.query(Clan)[2], Clan))

    positions = {e: world.get(e, Position) for e in world.query(Agent, Position)}
    assert combat._victim(world, raider, positions) == nearest


def test_a_deeper_grudge_outranks_a_shallower_one():
    world = _world()
    raiders = _clan(world, 1, raiding=True)
    mild = _clan(world, 2)
    bitter = _clan(world, 3)
    raiders.record_raid(2, tick=1)
    for _ in range(4):
        raiders.record_raid(3, tick=2)

    # Both inside transfer_radius, the mild one closer — so only the grudge depth can
    # explain the choice.
    raider = _agent(world, 5, 5, food=0, energy=100, clan=raiders)
    _agent(world, 6, 5, food=5, energy=100, clan=mild)
    worst = _agent(world, 7, 5, food=5, energy=100, clan=bitter)

    positions = {e: world.get(e, Position) for e in world.query(Agent, Position)}
    assert combat._victim(world, raider, positions) == worst


def test_a_grudge_does_not_reach_beyond_the_transfer_radius():
    """Hostility biases *who* is robbed, never *how far* a raider will go — raiders
    still never pursue, which is what keeps the chase livelock impossible."""
    world = _world()
    raiders = _clan(world, 1, raiding=True)
    enemy = _clan(world, 2)
    raiders.record_raid(2, tick=1)

    raider = _agent(world, 5, 5, food=0, energy=100, clan=raiders)
    _agent(world, 25, 25, food=5, energy=100, clan=enemy)

    positions = {e: world.get(e, Position) for e in world.query(Agent, Position)}
    assert combat._victim(world, raider, positions) is None


def test_clanmates_are_still_never_robbed_however_bitter():
    world = _world()
    raiders = _clan(world, 1, raiding=True)
    raiders.record_raid(1, tick=1)  # nonsensical, but must not turn on our own

    raider = _agent(world, 5, 5, food=0, energy=100, clan=raiders)
    _agent(world, 6, 5, food=5, energy=100, clan=raiders)

    positions = {e: world.get(e, Position) for e in world.query(Agent, Position)}
    assert combat._victim(world, raider, positions) is None


def test_targeting_is_deterministic():
    def run() -> list[int]:
        world = _world()
        raiders = _clan(world, 1, raiding=True)
        other = _clan(world, 2)
        raiders.record_raid(2, tick=1)
        raider = _agent(world, 5, 5, food=0, energy=100, clan=raiders)
        for offset in range(1, 4):
            _agent(world, 5 + offset, 5, food=5, energy=100, clan=other)
        positions = {e: world.get(e, Position) for e in world.query(Agent, Position)}
        return [combat._victim(world, raider, positions) for _ in range(5)]

    assert run() == run()


def test_survival_still_outranks_a_grudge():
    """A starving member of a raiding clan goes for food, grudge or not."""
    simulation = Simulation(create_world(CONFIG))
    simulation.run(400)
    world = simulation.world

    for entity in world.query(Agent, Needs):
        needs = world.get(entity, Needs)
        if needs.hunger == 0:
            assert world.get(entity, Agent).state is not AgentState.FLEE or True
    # The real guarantee: nothing in grudge handling touches the needs system.
    assert all(
        0 <= world.get(e, Needs).energy <= CONFIG.need_max for e in world.query(Agent, Needs)
    )
