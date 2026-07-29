"""Hut placement.

An agent in BUILD spends its wood on a hut at its current tile, unless that tile
is already occupied — in which case it steps back to IDLE and tries elsewhere.
"""

from __future__ import annotations

from ..components import Agent, AgentState, Building, Inventory, Position
from ..ecs import World
from ..rng import TickRng

HUT = "hut"


def _occupied_tiles(world: World) -> set[tuple[int, int]]:
    tiles: set[tuple[int, int]] = set()
    for entity in world.query(Building, Position):
        position = world.get(entity, Position)
        tiles.add((position.x, position.y))
    return tiles


def run(world: World, rng: TickRng) -> None:
    config = world.config
    builders = [
        entity
        for entity in world.query(Agent, Inventory, Position)
        if world.get(entity, Agent).state is AgentState.BUILD
    ]
    if not builders:
        return

    occupied = _occupied_tiles(world)
    build_ceiling = config.build_ceiling()

    for entity in builders:
        agent = world.get(entity, Agent)
        inventory = world.get(entity, Inventory)
        position = world.get(entity, Position)
        tile = (position.x, position.y)

        out_of_allowance = agent.huts_owned >= config.max_huts_per_agent
        map_too_dense = len(occupied) >= build_ceiling

        if inventory.wood < config.wood_per_hut or out_of_allowance or map_too_dense:
            agent.state = AgentState.IDLE
            agent.target_x = None
            agent.target_y = None
            continue

        if tile in occupied:
            position.x = max(0, min(config.grid_width - 1, position.x + rng.between(-1, 1)))
            position.y = max(0, min(config.grid_height - 1, position.y + rng.between(-1, 1)))
            continue

        inventory.wood -= config.wood_per_hut
        hut = world.create_entity()
        world.add(hut, Position(position.x, position.y))
        world.add(hut, Building(kind=HUT, owner=entity))
        occupied.add(tile)
        agent.huts_owned += 1
        agent.state = AgentState.IDLE
