"""One grid step per tick toward the agent's target.

Seeking agents move toward resources; following agents move toward their clan's rally
point. Gathering, building and resting hold position.
"""

from __future__ import annotations

from ..components import Agent, AgentState, Position
from ..ecs import World
from ..rng import TickRng


def _step(current: int, target: int) -> int:
    if target > current:
        return 1
    if target < current:
        return -1
    return 0


def run(world: World, rng: TickRng) -> None:
    config = world.config

    for entity in world.query(Agent, Position):
        agent = world.get(entity, Agent)
        if agent.state not in (AgentState.SEEK_NEED, AgentState.FOLLOW, AgentState.MEET):
            continue

        position = world.get(entity, Position)
        target_x = agent.target_x
        target_y = agent.target_y
        if target_x is None or target_y is None:
            continue

        position.x = max(0, min(config.grid_width - 1, position.x + _step(position.x, target_x)))
        position.y = max(0, min(config.grid_height - 1, position.y + _step(position.y, target_y)))

        if position.x == target_x and position.y == target_y and agent.target_entity is None:
            agent.target_x = None
            agent.target_y = None
