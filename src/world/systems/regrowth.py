"""Resource regrowth.

Runs last each tick, after agents have taken what they came for. A node that has
been foraged to zero is not removed — it sits dormant and recovers, which is what
keeps the world sustaining itself indefinitely.
"""

from __future__ import annotations

from ..components import ResourceNode
from ..ecs import World
from ..rng import TickRng


def run(world: World, rng: TickRng) -> None:
    tick = world.tick

    for entity in world.query(ResourceNode):
        node = world.get(entity, ResourceNode)
        if node.amount >= node.max_amount:
            continue
        if tick % node.regrow_every == 0:
            node.amount += 1
