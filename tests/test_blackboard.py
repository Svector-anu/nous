"""Shared blackboard: deterministic writes, tick-stamped reads, TTL expiry."""

from __future__ import annotations

from src.world.components import (
    KEY_FOOD_LOCATIONS,
    Agent,
    AgentState,
    Blackboard,
    Position,
    ResourceKind,
    ResourceNode,
    clan_rally_key,
)
from src.world.config import WorldConfig
from src.world.ecs import World
from src.world.rng import TickRng
from src.world.systems import blackboard as blackboard_system
from src.world.systems import social
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(seed=5, grid_width=32, grid_height=32, agent_count=25, resource_count=90)


def _board_world() -> tuple[World, Blackboard]:
    world = World(CONFIG)
    board = Blackboard()
    world.add(world.create_entity(), board)
    return world, board


def _tick_expiry(world: World) -> None:
    blackboard_system.run(world, TickRng(CONFIG.seed, world.tick, "blackboard"))


def test_write_then_read():
    _, board = _board_world()
    board.write("danger", [4, 9], written_by=7, tick=12)
    assert board.read("danger") == [4, 9]
    assert board.written_by("danger") == 7


def test_read_missing_key_is_none():
    _, board = _board_world()
    assert board.read("nothing_here") is None
    assert board.age("nothing_here", 0) is None


def test_age_tracks_ticks_since_write():
    _, board = _board_world()
    board.write("best_wood_area", [1, 1], written_by=2, tick=100)
    assert board.age("best_wood_area", 140) == 40


def test_overwrite_refreshes_the_stamp():
    _, board = _board_world()
    board.write("food_locations", [[1, 1]], written_by=2, tick=10)
    board.write("food_locations", [[2, 2]], written_by=3, tick=90)
    assert board.read("food_locations") == [[2, 2]]
    assert board.age("food_locations", 90) == 0


def test_entries_expire_after_ttl():
    world, board = _board_world()
    board.write("stale", "x", written_by=1, tick=0)
    board.write("fresh", "y", written_by=1, tick=CONFIG.blackboard_ttl_ticks)

    world.tick = CONFIG.blackboard_ttl_ticks + 1
    _tick_expiry(world)

    assert board.read("stale") is None
    assert board.read("fresh") == "y"


def test_expiry_is_exact_at_the_boundary():
    world, board = _board_world()
    board.write("edge", "v", written_by=1, tick=0)

    world.tick = CONFIG.blackboard_ttl_ticks
    _tick_expiry(world)
    assert board.read("edge") == "v", "expired one tick early"

    world.tick = CONFIG.blackboard_ttl_ticks + 1
    _tick_expiry(world)
    assert board.read("edge") is None


def test_keys_stay_sorted_so_iteration_is_stable():
    """A save round-trips through json; insertion order must already be canonical."""
    _, board = _board_world()
    board.write("zebra", 1, written_by=1, tick=0)
    board.write("alpha", 2, written_by=1, tick=0)
    board.write("middle", 3, written_by=1, tick=0)
    assert list(board.entries) == sorted(board.entries)


def test_agents_report_sightings_to_the_board():
    simulation = Simulation(create_world(CONFIG))
    simulation.run(200)
    world = simulation.world

    board = world.get(world.query(Blackboard)[0], Blackboard)
    reported = board.read(KEY_FOOD_LOCATIONS)
    assert isinstance(reported, list) and reported, "no food locations were ever shared"
    for coordinate in reported:
        assert len(coordinate) == 2


def test_sightings_are_capped():
    simulation = Simulation(create_world(CONFIG))
    simulation.run(300)
    world = simulation.world
    board = world.get(world.query(Blackboard)[0], Blackboard)

    for key in (KEY_FOOD_LOCATIONS, "wood_locations"):
        reported = board.read(key)
        if isinstance(reported, list):
            assert len(reported) <= CONFIG.blackboard_max_locations


def test_leaders_publish_a_rally_point():
    simulation = Simulation(create_world(CONFIG))
    simulation.run(400)
    world = simulation.world
    board = world.get(world.query(Blackboard)[0], Blackboard)

    rallies = [k for k in board.entries if k.endswith(":rally")]
    assert rallies, "no clan ever published a rally point"
    for key in rallies:
        rally = board.read(key)
        assert isinstance(rally, list) and len(rally) == 2


def test_rally_is_a_meeting_point_not_a_food_node():
    """Pointing a clan at food made all members strip one node and starve together."""
    simulation = Simulation(create_world(CONFIG))
    simulation.run(400)
    world = simulation.world
    board = world.get(world.query(Blackboard)[0], Blackboard)

    for entity in world.query(social.Clan):
        clan = world.get(entity, social.Clan)
        rally = board.read(clan_rally_key(clan.clan_id))
        if rally is None or clan.leader is None or not world.is_alive(clan.leader):
            continue
        leader_position = world.get(clan.leader, Position)
        assert rally == [leader_position.x, leader_position.y]


def test_board_survives_with_no_agents_left():
    world, board = _board_world()
    board.write("orphan", 1, written_by=99, tick=0)
    world.tick = 10
    _tick_expiry(world)
    assert board.read("orphan") == 1
