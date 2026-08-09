"""Huts falling down.

Buildings were permanent, which is why this world had no size it could settle at: a low
cap left every agent with nothing to build and the state machine put them to sleep, and a
high one covered the map until the agents were invisible under the roofs. Both were
observed on the live world within a day of each other.
"""

from __future__ import annotations

from dataclasses import replace

from src.world.components import Agent, Building, Position
from src.world.config import WorldConfig
from src.world.systems import decay
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(seed=5, agent_count=4, resource_count=8, hut_decay_ticks=10)


def _hut(world, owner=None):
    entity = world.create_entity()
    world.add(entity, Position(1, 1))
    world.add(entity, Building(kind="hut", owner=owner))
    return entity


def test_a_hut_whose_owner_is_gone_falls_down():
    world = create_world(CONFIG)
    ghost = world.create_entity()
    world.add(ghost, Agent(name="ghost", spawn_tick=0))
    hut = _hut(world, owner=ghost)
    world.destroy_entity(ghost)

    Simulation(world).run(CONFIG.hut_decay_ticks + 2)
    assert not world.is_alive(hut)


def test_a_hut_someone_lives_in_never_ages():
    """An owner who is alive keeps theirs standing however long they live. This thins the
    sprawl the dead left behind, not the town people are living in."""
    world = create_world(CONFIG)
    owner = next(iter(world.query(Agent)))
    hut = _hut(world, owner=owner)

    Simulation(world).run(CONFIG.hut_decay_ticks * 5)
    assert world.is_alive(hut)
    assert world.get(hut, Building).decay == 0


def test_decay_is_off_when_the_limit_is_zero():
    """Every world saved before this existed keeps its buildings exactly as they were."""
    world = create_world(replace(CONFIG, hut_decay_ticks=0))
    hut = _hut(world, owner=None)

    Simulation(world).run(200)
    assert world.is_alive(hut)


def test_an_ownerless_hut_falls_too():
    """A hut with no owner at all has nobody keeping it up either."""
    world = create_world(CONFIG)
    hut = _hut(world, owner=None)

    Simulation(world).run(CONFIG.hut_decay_ticks + 2)
    assert not world.is_alive(hut)


def test_the_work_per_tick_is_bounded():
    """A world with thousands of huts must not spend a whole tick walking all of them."""
    world = create_world(CONFIG)
    for _ in range(decay.MAX_CHECKED_PER_TICK * 3):
        _hut(world, owner=None)

    before = len(list(world.query(Building)))
    decay.run(world, None)
    after = sum(1 for e in world.query(Building) if world.get(e, Building).decay > 0)
    assert after <= decay.MAX_CHECKED_PER_TICK
    assert before == len(list(world.query(Building))), "nothing should fall on the first tick"


def test_a_world_thins_rather_than_filling_forever():
    """The property this exists for: abandoned huts stop accumulating."""
    world = create_world(CONFIG)
    for _ in range(30):
        _hut(world, owner=None)
    start = len(list(world.query(Building)))

    Simulation(world).run(CONFIG.hut_decay_ticks * 2)
    assert len(list(world.query(Building))) < start
