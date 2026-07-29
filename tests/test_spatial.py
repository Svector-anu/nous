"""The spatial index must be a pure speed-up: same answers as a brute-force scan."""

from __future__ import annotations

from src.world.components import Position, ResourceKind, ResourceNode
from src.world.config import WorldConfig
from src.world.ecs import Entity, World
from src.world.spatial import ResourceIndex
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(seed=44, grid_width=32, grid_height=32, agent_count=20, resource_count=80)


def _brute_nearest(world: World, origin: Position, kind: ResourceKind, radius: int) -> Entity | None:
    """Exactly what fsm._nearest_resource did before the index existed."""
    best_entity: Entity | None = None
    best_distance = radius * radius + 1
    for entity in world.query(ResourceNode, Position):
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


def _brute_live_at(world: World, x: int, y: int, kind: ResourceKind) -> bool:
    for entity in world.query(ResourceNode, Position):
        node = world.get(entity, ResourceNode)
        if node.kind is not kind or node.is_dormant:
            continue
        position = world.get(entity, Position)
        if position.x == x and position.y == y:
            return True
    return False


def _index(world: World) -> ResourceIndex:
    return ResourceIndex(world, world.config.spatial_chunk_size)


def test_nearest_matches_brute_force_everywhere():
    world = create_world(CONFIG)
    index = _index(world)

    for x in range(0, CONFIG.grid_width, 3):
        for y in range(0, CONFIG.grid_height, 3):
            origin = Position(x, y)
            for kind in ResourceKind:
                assert index.nearest(origin, kind, CONFIG.vision_radius) == _brute_nearest(
                    world, origin, kind, CONFIG.vision_radius
                ), f"disagreed at ({x},{y}) for {kind}"


def test_nearest_matches_brute_force_after_depletion():
    """Dormancy is filtered at query time, so the index must track it."""
    simulation = Simulation(create_world(CONFIG))
    simulation.run(800)
    world = simulation.world
    index = _index(world)

    for x in range(0, CONFIG.grid_width, 5):
        for y in range(0, CONFIG.grid_height, 5):
            origin = Position(x, y)
            for kind in ResourceKind:
                assert index.nearest(origin, kind, CONFIG.vision_radius) == _brute_nearest(
                    world, origin, kind, CONFIG.vision_radius
                )


def test_live_at_matches_brute_force():
    world = create_world(CONFIG)
    index = _index(world)

    for entity in world.query(ResourceNode, Position):
        position = world.get(entity, Position)
        for kind in ResourceKind:
            assert index.live_at(position.x, position.y, kind) == _brute_live_at(
                world, position.x, position.y, kind
            )


def test_live_at_is_false_on_empty_ground():
    world = World(CONFIG)
    index = _index(world)
    assert index.live_at(3, 3, ResourceKind.FOOD) is False


def test_nearest_respects_the_radius():
    world = World(CONFIG)
    node = world.create_entity()
    world.add(node, Position(20, 20))
    world.add(node, ResourceNode(kind=ResourceKind.FOOD, amount=5, max_amount=5, regrow_every=50))
    index = _index(world)

    assert index.nearest(Position(0, 0), ResourceKind.FOOD, 5) is None
    assert index.nearest(Position(20, 21), ResourceKind.FOOD, 5) == node


def test_nearest_ignores_dormant_nodes():
    world = World(CONFIG)
    node = world.create_entity()
    world.add(node, Position(5, 5))
    world.add(node, ResourceNode(kind=ResourceKind.FOOD, amount=0, max_amount=5, regrow_every=50))

    assert _index(world).nearest(Position(5, 5), ResourceKind.FOOD, 10) is None


def test_ties_go_to_the_lowest_entity_id():
    """The pre-index scan visited sorted ids and used a strict <, so the lowest id won
    a tie. Determinism depends on the index doing the same."""
    world = World(CONFIG)
    created = []
    for _ in range(3):
        entity = world.create_entity()
        world.add(entity, Position(10, 10))
        world.add(entity, ResourceNode(kind=ResourceKind.FOOD, amount=5, max_amount=5, regrow_every=50))
        created.append(entity)

    assert _index(world).nearest(Position(10, 10), ResourceKind.FOOD, 5) == min(created)


def test_chunk_size_does_not_change_answers():
    world = create_world(CONFIG)
    reference = ResourceIndex(world, 1)

    for chunk in (2, 4, 8, 16, 64):
        index = ResourceIndex(world, chunk)
        for x in range(0, CONFIG.grid_width, 7):
            for y in range(0, CONFIG.grid_height, 7):
                origin = Position(x, y)
                assert index.nearest(origin, ResourceKind.FOOD, CONFIG.vision_radius) == \
                    reference.nearest(origin, ResourceKind.FOOD, CONFIG.vision_radius)


def test_empty_world_is_handled():
    world = World(CONFIG)
    index = _index(world)
    assert index.nearest(Position(0, 0), ResourceKind.FOOD, 10) is None
