"""User rest-mode requests applied at a fixed point in the tick.

Rest mode is not a simulation rule change for ordinary agents: only user-deployed
agents can be toggled, and the queue is drained once per tick so the request is an
input to the world rather than a wall-clock event.
"""

from __future__ import annotations

from ..components import Agent, RestQueue
from ..ecs import World
from ..rng import TickRng


def queue(world: World) -> RestQueue | None:
    entity = world.first(RestQueue)
    return world.get(entity, RestQueue) if entity is not None else None


def enqueue(world: World, agent_id: int, rest: bool) -> None:
    """Queue a rest-mode toggle for the next tick.

    Pending requests are capped by max_user_agents: one toggle per user agent is
    enough, and the bound prevents the queue from growing without limit.
    """
    pending = queue(world)
    if pending is None:
        raise ValueError("world has no rest queue")

    config = world.config
    # Replace any earlier pending toggle for the same agent.
    entries = [e for e in pending.pending if e.get("agent_id") != agent_id]
    if len(entries) >= config.max_user_agents:
        entries.pop(0)
    entries.append({"agent_id": agent_id, "rest": rest, "requested_tick": world.tick})
    pending.pending = entries


def run(world: World, rng: TickRng) -> None:
    pending = queue(world)
    if pending is None or not pending.pending:
        return

    for entry in pending.pending:
        agent = world.try_get(entry["agent_id"], Agent)
        if agent is None:
            continue
        agent.rest_mode = bool(entry["rest"])
    pending.pending.clear()
