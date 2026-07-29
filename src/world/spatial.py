"""Chunked spatial lookup for resource nodes.

`_nearest_resource` used to scan every node in the world for every seeking agent, every
tick — 56% of runtime by tick 5000, and the first thing that would block phase 3 agent
counts. This is the spatial partitioning `planish/TECHNICAL_ARCHITECTURE.md` calls for.

Node positions never change: nodes are created at genesis and are never destroyed, only
made dormant. So the buckets are pure position data and are built once per tick, then
queried many times. Dormancy is filtered at query time.

Tie-breaking is deliberate. Candidates are visited in ascending entity order and compared
with a strict `<`, which is exactly what a sorted `world.query()` scan did — the lowest
entity id wins a tie. Preserving that is what makes this a pure speed-up and not a
behaviour change.
"""

from __future__ import annotations

from .components import Position, ResourceKind, ResourceNode
from .ecs import Entity, World


class ResourceIndex:
    __slots__ = ("_world", "_chunk", "_buckets")

    def __init__(self, world: World, chunk_size: int) -> None:
        self._world = world
        self._chunk = max(1, chunk_size)
        self._buckets: dict[tuple[int, int], list[Entity]] = {}

        for entity in world.query(ResourceNode, Position):
            position = world.get(entity, Position)
            key = (position.x // self._chunk, position.y // self._chunk)
            self._buckets.setdefault(key, []).append(entity)

    def _candidates(self, x: int, y: int, radius: int) -> list[Entity]:
        chunk = self._chunk
        found: list[Entity] = []
        for cx in range((x - radius) // chunk, (x + radius) // chunk + 1):
            for cy in range((y - radius) // chunk, (y + radius) // chunk + 1):
                bucket = self._buckets.get((cx, cy))
                if bucket:
                    found.extend(bucket)
        found.sort()
        return found

    def nearest(self, origin: Position, kind: ResourceKind, radius: int) -> Entity | None:
        world = self._world
        best_entity: Entity | None = None
        best_distance = radius * radius + 1

        for entity in self._candidates(origin.x, origin.y, radius):
            node = world.get(entity, ResourceNode)
            if node.kind is not kind or node.is_dormant:
                continue
            position = world.get(entity, Position)
            dx = position.x - origin.x
            dy = position.y - origin.y
            distance = dx * dx + dy * dy
            if distance < best_distance:
                best_distance = distance
                best_entity = entity

        return best_entity

    def live_at(self, x: int, y: int, kind: ResourceKind) -> bool:
        world = self._world
        bucket = self._buckets.get((x // self._chunk, y // self._chunk))
        if not bucket:
            return False
        for entity in bucket:
            node = world.get(entity, ResourceNode)
            if node.kind is not kind or node.is_dormant:
                continue
            position = world.get(entity, Position)
            if position.x == x and position.y == y:
                return True
        return False
