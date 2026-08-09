"""Huts falling down.

Buildings were permanent, and that was the one thing keeping this world from finding its
own size. With a low cap every agent built its share and then had nothing to want, so the
state machine put them all to sleep; with a high one they covered the map until the agents
were invisible beneath the roofs. Neither is a world — one has finished and the other has
suffocated.

A hut whose owner is dead has nobody to keep it up, and after `hut_decay_ticks` it falls.
A hut whose owner is alive never decays, however long that agent lives, so this thins the
sprawl the dead left behind rather than the town people are living in.

Runs after `build` so a hut raised this tick is not judged on the same one.
"""

from __future__ import annotations

from ..components import Building
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

    # How many each living owner holds. A hut only counts as lived-in while its owner is
    # within their allowance — the first version aged only the huts of the dead, and with
    # ninety-seven living agents holding twenty-four each it would have removed almost
    # nothing while the map stayed buried.
    held: dict[int, int] = {}
    for entity in world.query(Building):
        owner = world.get(entity, Building).owner
        if owner is not None and world.is_alive(owner):
            held[owner] = held.get(owner, 0) + 1

    doomed: list[int] = []
    checked = 0
    for entity in world.query(Building):
        if checked >= MAX_CHECKED_PER_TICK:
            break
        checked += 1
        building = world.get(entity, Building)
        owner = building.owner
        alive = owner is not None and world.is_alive(owner)
        if alive and held.get(owner, 0) <= keep:
            # Someone lives here and it is within what they can keep up. Never ages.
            building.decay = 0
            continue
        building.decay += 1
        if building.decay >= limit:
            doomed.append(entity)
            if alive:
                # Their surplus shrinks as it falls, so an owner stops losing huts the
                # moment they are back within their allowance.
                held[owner] = held.get(owner, 1) - 1

    for entity in doomed:
        world.destroy_entity(entity)
