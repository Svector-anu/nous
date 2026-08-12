"""Two containers, one world file.

Railway overlaps containers across a deploy: for a few seconds the old process and the new
one both have the volume mounted and both are ticking the same world. A save rewrites the
whole thing, so whichever writes last wins — and when that is the older container, the
world rewinds.

It has happened twice on the live world. Once it took a user's agent with it, which is the
only way real data has ever been lost here. Everything else this project can get wrong
costs a redeploy; this costs somebody the thing they made.
"""

from __future__ import annotations

import pytest

from src.persistence.sqlite_store import SqliteWorldStore
from src.world.config import WorldConfig
from src.world.errors import StaleWorldError
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(seed=2, agent_count=4, resource_count=8, save_every_ticks=5)


def _world(tick: int):
    world = create_world(CONFIG)
    world.tick = tick
    return world


def test_an_older_process_cannot_overwrite_a_newer_world(tmp_path):
    """The bug itself. The old container is behind, saves, and the world goes backwards."""
    path = tmp_path / "w.db"
    ahead = SqliteWorldStore(path)
    ahead.save(_world(5000))

    behind = SqliteWorldStore(path)
    with pytest.raises(StaleWorldError):
        behind.save(_world(4000))

    assert SqliteWorldStore(path).stored_tick() == 5000


def test_the_newer_process_still_saves_normally(tmp_path):
    """The guard must not stop the container that legitimately owns the world."""
    path = tmp_path / "w.db"
    store = SqliteWorldStore(path)
    store.save(_world(100))
    store.save(_world(200))

    assert SqliteWorldStore(path).stored_tick() == 200


def test_saving_the_same_tick_again_is_allowed(tmp_path):
    """A shutdown save often lands on the tick already written. Refusing it would turn a
    normal restart into an error every single time."""
    path = tmp_path / "w.db"
    store = SqliteWorldStore(path)
    store.save(_world(300))
    store.save(_world(300))  # must not raise

    assert SqliteWorldStore(path).stored_tick() == 300


def test_a_fresh_world_saves_into_an_empty_file(tmp_path):
    store = SqliteWorldStore(tmp_path / "new.db")
    assert store.stored_tick() is None
    store.save(_world(0))

    assert store.stored_tick() == 0


def test_a_stale_simulation_stops_saving_rather_than_dying(tmp_path):
    """The leftover container keeps serving http and keeps ticking in memory. What it must
    never do is write again — it would rewind the world the moment it caught up.

    So the loop drops its store instead of raising: a crash here would take down a process
    that is still answering requests, and this is a deploy, not an emergency.
    """
    path = tmp_path / "w.db"
    SqliteWorldStore(path).save(_world(9000))

    world = create_world(CONFIG)
    simulation = Simulation(world, store=SqliteWorldStore(path))
    simulation.run(20)  # must not raise

    assert simulation.store is None, "the stale process kept its store"
    assert getattr(simulation, "stale", False) is True
    assert SqliteWorldStore(path).stored_tick() == 9000, "the world was rewound"


def test_the_world_it_owns_is_never_left_half_written(tmp_path):
    """A refused save must leave the file exactly as it was. The write is a delete of
    everything followed by a rebuild, so a guard that fired mid-transaction would be worse
    than the rewind it prevents."""
    path = tmp_path / "w.db"
    ahead = SqliteWorldStore(path)
    good = _world(7000)
    ahead.save(good)
    before = SqliteWorldStore(path).load()

    behind = SqliteWorldStore(path)
    with pytest.raises(StaleWorldError):
        behind.save(_world(10))

    after = SqliteWorldStore(path).load()
    assert after.tick == before.tick
    assert len(list(after.query())) == len(list(before.query()))
