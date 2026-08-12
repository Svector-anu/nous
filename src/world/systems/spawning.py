"""User-deployed agents entering the world.

Runs first each tick, so a newly deployed agent is visible to every other system on the
same tick it arrives.

A deployment is an input to the simulation, not an event inside it. The api only appends
to the queue; this system is the single place an agent is actually created, and it draws
position from the tick's rng stream. That is what keeps history reproducible: the same
seed plus the same deployments at the same ticks rebuilds the same world, and a save
carrying a pending request replays it identically on reload.

Deployed agents are ordinary agents. They need, forage, build, join clans, starve and die
exactly like the rest — they simply carry a name and a personality note.
"""

from __future__ import annotations

from ..components import (
    Agent,
    ClanRef,
    Inbox,
    Inventory,
    Needs,
    Outbox,
    Position,
    SpawnQueue,
    Standing,
)
from ..ecs import Entity, World
from .. import naming
from ..rng import TickRng
from . import markets


def queue(world: World) -> SpawnQueue | None:
    entity = world.first(SpawnQueue)
    return world.get(entity, SpawnQueue) if entity is not None else None


def user_agent_count(world: World) -> int:
    return sum(
        1 for entity in world.query(Agent) if world.get(entity, Agent).user_deployed
    )


def enqueue(world: World, name: str, personality: str) -> None:
    """Append a deployment request. Validation belongs to the caller."""
    pending = queue(world)
    if pending is None:
        raise ValueError("world has no spawn queue")
    pending.pending.append(
        {"name": name, "personality": personality, "requested_tick": world.tick}
    )


def _spawn(world: World, request: dict, rng: TickRng) -> Entity:
    config = world.config
    entity = world.create_entity()

    world.add(entity, Position(rng.below(config.grid_width), rng.below(config.grid_height)))
    world.add(
        entity,
        Needs(
            energy=config.need_max,
            hunger=config.need_max,
            social=config.need_max,
            safety=config.need_max,
        ),
    )
    world.add(entity, Inventory())
    world.add(entity, ClanRef())
    world.add(entity, Inbox())
    world.add(entity, Outbox())
    world.add(
        entity,
        Agent(
            # Applied here rather than at the http door, so a name that arrives through
            # any route — http, a2a, a replayed queue from an older save — gets the same
            # treatment. The queue is the one place every spawn passes through.
            name=naming.clean(str(request["name"]), entity),
            personality=str(request.get("personality", "")),
            user_deployed=True,
            spawn_tick=world.tick,
        ),
    )
    world.add(entity, Standing())
    return entity


def run(world: World, rng: TickRng) -> None:
    pending = queue(world)
    if pending is None or not pending.pending:
        return

    for request in pending.pending:
        entity = _spawn(world, request, rng)
        agent = world.get(entity, Agent)
        if agent.user_deployed:
            markets.open_agent_survives(world, entity, agent.name)
    pending.pending.clear()
