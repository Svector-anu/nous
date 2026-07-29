"""The Phase 0 acceptance criterion: same seed, same world; restart changes nothing."""

from __future__ import annotations

from src.persistence.sqlite_store import SqliteWorldStore
from src.world.config import WorldConfig
from src.world.rng import TickRng
from src.world.tick import Simulation, create_world, state_hash

TEST_CONFIG = WorldConfig(seed=99, grid_width=32, grid_height=32, agent_count=25, resource_count=60)


def _run(ticks: int, config: WorldConfig = TEST_CONFIG) -> str:
    simulation = Simulation(create_world(config))
    simulation.run(ticks)
    return state_hash(simulation.world)


def test_same_seed_produces_identical_world():
    assert _run(150) == _run(150)


def test_different_seed_produces_different_world():
    other = WorldConfig(**{**TEST_CONFIG.__dict__, "seed": 100})
    assert _run(150) != _run(150, other)


def test_rng_stream_depends_on_tick_and_stream_name():
    assert TickRng(1, 0, "fsm").next_u64() != TickRng(1, 1, "fsm").next_u64()
    assert TickRng(1, 0, "fsm").next_u64() != TickRng(1, 0, "movement").next_u64()
    assert TickRng(1, 5, "fsm").next_u64() == TickRng(1, 5, "fsm").next_u64()


def test_rng_below_stays_in_range():
    rng = TickRng(7, 7, "range")
    assert all(0 <= rng.below(10) < 10 for _ in range(500))


def test_social_state_survives_a_reload(tmp_path):
    """Clans, blackboard entries and in-flight mail must all round-trip.

    Both bugs this caught were ordering, not content: json sort_keys reordered nested
    dicts so a reloaded world iterated messages and board keys differently.
    """
    from src.world.components import Blackboard, Clan
    from src.world.config import WorldConfig

    config = WorldConfig(seed=3, grid_width=32, grid_height=32, agent_count=30, resource_count=90)
    store = SqliteWorldStore(tmp_path / "social.db")
    simulation = Simulation(create_world(config), store=store)
    simulation.run(400)

    live = simulation.world
    live_clans = {world_clan.clan_id: sorted(world_clan.members)
                  for world_clan in (live.get(e, Clan) for e in live.query(Clan))}
    live_board = dict(live.get(live.query(Blackboard)[0], Blackboard).entries)
    assert live_clans, "no clans to round-trip"
    assert live_board, "no blackboard entries to round-trip"

    store.save(live)
    store.close()

    reopened = SqliteWorldStore(tmp_path / "social.db")
    loaded = reopened.load()
    reopened.close()

    loaded_clans = {c.clan_id: sorted(c.members)
                    for c in (loaded.get(e, Clan) for e in loaded.query(Clan))}
    loaded_board = loaded.get(loaded.query(Blackboard)[0], Blackboard).entries

    assert loaded_clans == live_clans
    assert loaded_board == live_board
    assert list(loaded_board) == list(live_board), "board key order changed across reload"


def test_save_and_reload_continues_identically(tmp_path):
    uninterrupted = _run(120)

    store = SqliteWorldStore(tmp_path / "world.db")
    simulation = Simulation(create_world(TEST_CONFIG), store=store)
    simulation.run(60)
    store.save(simulation.world)
    checkpoint = state_hash(simulation.world)
    store.close()

    reopened = SqliteWorldStore(tmp_path / "world.db")
    resumed = Simulation(reopened.load())
    assert state_hash(resumed.world) == checkpoint

    resumed.run(60)
    reopened.close()

    assert state_hash(resumed.world) == uninterrupted
