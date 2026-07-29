"""Blackboard upkeep.

Runs last each tick and expires knowledge older than `blackboard_ttl_ticks`. Without
this the board fills with sightings of nodes that were foraged out hundreds of ticks ago
and agents walk to empty ground on the strength of them.
"""

from __future__ import annotations

from ..components import Blackboard
from ..ecs import Entity, World
from ..rng import TickRng


def board_entity(world: World) -> Entity | None:
    return world.first(Blackboard)


def board(world: World) -> Blackboard | None:
    entity = board_entity(world)
    return world.get(entity, Blackboard) if entity is not None else None


def run(world: World, rng: TickRng) -> None:
    current = board(world)
    if current is None:
        return
    current.expire(world.tick, world.config.blackboard_ttl_ticks)
