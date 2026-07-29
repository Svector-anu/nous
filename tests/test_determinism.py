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
