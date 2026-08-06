"""Wallet addresses attached to user-deployed agents at a fixed point in the tick.

An address is a *label*. Nothing in this file, and nothing anywhere in `src/world`, may
read `owner_address` to decide what an agent does — that would make ownership an in-world
advantage, which is settled and closed (NEXT.md, "do not reopen"). A linked agent forages,
starves and dies exactly like every other agent.

The signature that proves an address is verified at the api boundary, before anything
reaches this queue. That separation is the same one the advisor follows: the world depends
on the *recorded* outcome, never on the network call that produced it, so a replay from a
save needs no rpc and no key.

Runs immediately after spawning, so an agent deployed and linked in the same request pair
carries its label from the first tick anyone can see it.
"""

from __future__ import annotations

from ..components import Agent, IdentityQueue
from ..ecs import World
from ..rng import TickRng


def queue(world: World) -> IdentityQueue | None:
    entity = world.first(IdentityQueue)
    return world.get(entity, IdentityQueue) if entity is not None else None


def normalise(address: str) -> str:
    """Lowercase an address so one wallet is one string.

    EIP-55 checksummed and all-lowercase forms are the same account, and comparing them
    verbatim would let a user link twice and see two different owners.
    """
    return (address or "").strip().lower()


def enqueue(world: World, agent_id: int, address: str) -> None:
    """Queue a verified link for the next tick. Verification belongs to the caller.

    A later request for the same agent replaces an earlier pending one, and the queue is
    capped by `identity_queue_limit`: every unbounded accumulator added to this project
    has eventually eaten the simulation.
    """
    pending = queue(world)
    if pending is None:
        raise ValueError("world has no identity queue")

    entries = [e for e in pending.pending if e.get("agent_id") != agent_id]
    limit = max(1, world.config.identity_queue_limit)
    while len(entries) >= limit:
        entries.pop(0)
    entries.append(
        {
            "agent_id": agent_id,
            "address": normalise(address),
            "requested_tick": world.tick,
        }
    )
    pending.pending = entries


def owner_of(world: World, agent_id: int) -> str:
    agent = world.try_get(agent_id, Agent)
    return agent.owner_address if agent is not None else ""


def agents_owned_by(world: World, address: str) -> list[int]:
    """Every agent carrying this address, in ascending id order like every other query."""
    wanted = normalise(address)
    if not wanted:
        return []
    return [
        entity
        for entity in world.query(Agent)
        if world.get(entity, Agent).owner_address == wanted
    ]


def run(world: World, rng: TickRng) -> None:
    pending = queue(world)
    if pending is None or not pending.pending:
        return

    for entry in pending.pending:
        agent = world.try_get(entry["agent_id"], Agent)
        # Only user-deployed agents can be claimed. An unowned native agent is part of
        # the world, not a thing a spectator may put their name on.
        if agent is None or not agent.user_deployed:
            continue
        agent.owner_address = normalise(entry.get("address", ""))
    pending.pending.clear()
