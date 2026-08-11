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


def test_huts_beyond_what_an_owner_can_keep_up_fall():
    """The first version aged only the huts of the dead. With ninety-seven living agents
    holding twenty-four each it removed almost nothing while the map stayed buried."""
    world = create_world(replace(CONFIG, max_huts_per_agent=2, hut_decay_ticks=5))
    owner = next(iter(world.query(Agent)))
    huts = [_hut(world, owner=owner) for _ in range(8)]

    Simulation(world).run(60)
    left = [h for h in huts if world.is_alive(h)]
    assert len(left) <= 2, f"{len(left)} huts survived a cap of 2"
    assert len(left) >= 1, "an owner within their allowance should keep some"


def test_an_owner_stops_losing_huts_once_back_within_the_cap():
    """Counted against everything the owner ends up holding, not just the huts placed
    here — it is a live world and the agent keeps building, so the originals are not the
    whole story."""
    world = create_world(replace(CONFIG, max_huts_per_agent=3, hut_decay_ticks=5))
    owner = next(iter(world.query(Agent)))
    for _ in range(5):
        _hut(world, owner=owner)

    Simulation(world).run(200)
    held = sum(
        1
        for entity in world.query(Building)
        if world.get(entity, Building).owner == owner
    )
    assert held <= 3, f"owner kept {held} huts against a cap of 3"
    assert held >= 1, "decay should stop, not strip an owner bare"


def test_a_fallen_hut_gives_its_owner_the_allowance_back():
    """`huts_owned` is a counter, not a query. Left standing while the hut falls, an agent
    believes it is at its cap while owning nothing — it stops building for good and the
    world goes quiet, which is the failure this whole system exists to prevent."""
    world = create_world(replace(CONFIG, max_huts_per_agent=2, hut_decay_ticks=5))
    owner = next(iter(world.query(Agent)))
    agent = world.get(owner, Agent)
    for _ in range(6):
        _hut(world, owner=owner)
    agent.huts_owned = 6

    Simulation(world).run(80)
    standing = sum(
        1 for e in world.query(Building) if world.get(e, Building).owner == owner
    )
    assert agent.huts_owned == standing, (
        f"counter says {agent.huts_owned}, {standing} huts actually stand"
    )


# --- a world at its cap still has work in it ---------------------------------------
#
# The second dead end. Decay gave the world a size it could settle at, and then the
# settling itself became the problem: once every agent held its six, nothing could fall,
# so nothing could be rebuilt. No wood worth gathering, nothing to build, not hungry
# enough to eat. On the live world two thirds of the agents sat in REST, and every one of
# those decisions was individually correct.


def test_a_hut_somebody_lives_in_eventually_falls():
    world = create_world(replace(CONFIG, hut_upkeep_ticks=40, max_huts_per_agent=6))
    owner = next(iter(world.query(Agent)))
    hut = _hut(world, owner=owner)

    Simulation(world).run(60)
    assert not world.is_alive(hut)


def test_a_lived_in_hut_outlasts_an_abandoned_one():
    """The whole point of two numbers. A home is not a ruin, it just is not permanent."""
    world = create_world(replace(CONFIG, hut_decay_ticks=5, hut_upkeep_ticks=200))
    owner = next(iter(world.query(Agent)))
    mine = _hut(world, owner=owner)
    theirs = _hut(world, owner=None)

    Simulation(world).run(30)
    assert not world.is_alive(theirs), "an abandoned hut should be long gone"
    assert world.is_alive(mine), "a home fell as fast as a ruin"


def test_upkeep_of_zero_keeps_the_old_behaviour():
    """Every world saved before this existed keeps its homes standing exactly as it did."""
    world = create_world(replace(CONFIG, hut_upkeep_ticks=0, max_huts_per_agent=6))
    owner = next(iter(world.query(Agent)))
    hut = _hut(world, owner=owner)

    Simulation(world).run(300)
    assert world.is_alive(hut)


def test_a_world_at_its_cap_still_builds_something():
    """The property this exists for.

    Every agent starts holding its full allowance, which is exactly the state the live
    world reached: 606 huts, 94 agents, a cap of six. With permanent huts nothing can
    fall, so nothing is ever built again — no wood worth gathering, nothing to build, not
    hungry enough to eat, and two thirds of the world sits in REST.

    New buildings are the sharp signal. Counting how many agents look busy is not: a
    hungry agent walking to a berry looks identical either way, which is why the first
    version of this test passed against the very bug it was written for.
    """
    config = replace(
        CONFIG, agent_count=8, resource_count=60, max_huts_per_agent=3, hut_upkeep_ticks=60
    )
    world = create_world(config)
    for entity in world.query(Agent):
        world.get(entity, Agent).huts_owned = config.max_huts_per_agent
        for _ in range(config.max_huts_per_agent):
            _hut(world, owner=entity)
    before = {e for e in world.query(Building)}

    Simulation(world).run(900)
    built = {e for e in world.query(Building)} - before

    assert built, "not one hut was raised in 900 ticks — the world had finished"


def test_the_number_of_huts_stays_near_the_cap():
    """Upkeep must not empty the world either. Falling and rebuilding should hold the
    count roughly where the cap puts it, not trend to zero."""
    config = replace(
        CONFIG, agent_count=6, resource_count=60, max_huts_per_agent=3, hut_upkeep_ticks=80
    )
    world = create_world(config)
    for entity in world.query(Agent):
        world.get(entity, Agent).huts_owned = config.max_huts_per_agent
        for _ in range(config.max_huts_per_agent):
            _hut(world, owner=entity)
    start = len(list(world.query(Building)))

    Simulation(world).run(600)
    left = len(list(world.query(Building)))

    assert left > 0, "the world emptied out"
    assert left <= start * 1.5, f"{left} huts from {start} — it is growing again"
