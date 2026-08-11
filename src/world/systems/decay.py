"""Huts falling down.

Buildings were permanent, and that was the one thing keeping this world from finding its
own size. With a low cap every agent built its share and then had nothing to want, so the
state machine put them all to sleep; with a high one they covered the map until the agents
were invisible beneath the roofs. Neither is a world — one has finished and the other has
suffocated.

A hut whose owner is dead has nobody to keep it up, and after `hut_decay_ticks` it falls.
A hut somebody lives in lasts far longer — `hut_upkeep_ticks` — but not forever.

Forever was the second dead end. Once every agent held its six the cap meant nothing could
fall, so nothing could be rebuilt: no wood worth gathering, nothing to build, not hungry
enough to eat. Two thirds of the live world sat in REST, and each of those decisions was
individually correct. A world with a stable population of huts and no reason to touch any
of them has finished just as surely as one that ran out of room.

Runs after `build` so a hut raised this tick is not judged on the same one.
"""

from __future__ import annotations

from ..components import Agent, Building
from ..ecs import World
from ..rng import TickRng

# Bounded work per tick. A world with thousands of huts must not spend a whole tick
# walking all of them, and there is no hurry — a building falling a few ticks late is
# indistinguishable from one falling on time.
MAX_CHECKED_PER_TICK = 200


def run(world: World, rng: TickRng) -> None:
    limit = getattr(world.config, "hut_decay_ticks", 0)
    if limit <= 0:
        return
    keep = max(0, getattr(world.config, "max_huts_per_agent", 0))
    upkeep = max(0, getattr(world.config, "hut_upkeep_ticks", 0))

    # How many each living owner holds. A hut only counts as lived-in while its owner is
    # within their allowance — the first version aged only the huts of the dead, and with
    # ninety-seven living agents holding twenty-four each it would have removed almost
    # nothing while the map stayed buried.
    held: dict[int, int] = {}
    for entity in world.query(Building):
        owner = world.get(entity, Building).owner
        if owner is not None and world.is_alive(owner):
            held[owner] = held.get(owner, 0) + 1

    # A rotating window, not the first N. `world.query` yields the same order every tick,
    # so breaking after two hundred meant the same two hundred were examined forever and
    # every building past them was immortal — with 622 huts on the live world, two thirds
    # of them could never fall however long they stood.
    buildings = list(world.query(Building))
    total = len(buildings)
    if total == 0:
        return
    window = min(MAX_CHECKED_PER_TICK, total)
    # How many ticks pass between visits to any one building. Ageing by that much per
    # visit keeps `decay` meaning ticks of neglect rather than "times we happened to
    # look", so the same configured lifetime holds whatever the window is.
    stride = -(-total // window)
    start = (world.tick * window) % total

    doomed: list[int] = []
    for offset in range(window):
        entity = buildings[(start + offset) % total]
        building = world.get(entity, Building)
        owner = building.owner
        alive = owner is not None and world.is_alive(owner)
        lived_in = alive and held.get(owner, 0) <= keep
        if lived_in and upkeep <= 0:
            # Someone lives here, it is within what they can keep up, and this world does
            # not age those. Kept for worlds saved before upkeep existed.
            building.decay = 0
            continue

        building.decay += stride
        # A lived-in hut lasts far longer than an abandoned one, but not forever. Forever
        # is what put two thirds of the world to sleep: at the cap nothing could fall, so
        # nothing could be rebuilt, and there was no work left to do.
        falls_at = upkeep if lived_in else limit
        if building.decay >= falls_at:
            doomed.append(entity)
            if alive:
                # Their surplus shrinks as it falls, so an owner stops losing huts the
                # moment they are back within their allowance.
                held[owner] = held.get(owner, 1) - 1

    for entity in doomed:
        # Give the owner its allowance back. `huts_owned` is a counter, not a query, and
        # leaving it standing while the hut falls tells an agent it is at its cap when it
        # owns nothing — it stops building for good and the world goes quiet again, which
        # is the failure this whole system exists to prevent.
        owner = world.get(entity, Building).owner
        if owner is not None and world.is_alive(owner):
            agent = world.try_get(owner, Agent)
            if agent is not None:
                agent.huts_owned = max(0, agent.huts_owned - 1)
        world.destroy_entity(entity)
