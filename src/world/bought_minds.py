"""Running the minds visitors bought.

A visitor picks a model and pays for it; this calls that model and turns the answer into
a steer. The steer then goes into `MindQueue` through `minds.enqueue_steer`, the same door
a visitor's own process posts through — so from the tick's point of view a bought mind and
a self-hosted one are indistinguishable, subject to the same rate limit, the same fixed
drain and the same narrow set of things a mind may do.

**Not a system.** Systems run inside the tick loop and this makes network calls; putting
it there would stall the world and make history depend on somebody else's latency. It runs
as a background job beside the sim loop, exactly like the rules in `chain/rpc.py` say.

**The world never calls an address a stranger chose.** A visitor supplies a model *name*,
checked against the catalog Surplus publishes. The host is fixed and is the same one the
world already trusts for its own advisor. That is why this does not reopen the class of
bug the push-only design was built to delete.

**Money is reserved before the call and committed after it.** The same shape as clan
decisions, for the same reason: a limit checked before a network round trip and committed
after lets everything arriving in between through. Here the reservation is at the model's
estimated price and the commit is at what the response says it actually used.
"""

from __future__ import annotations

import logging

from ..llm import surplus
from . import spend as governor
from .components import AttachedMind
from .systems import minds

logger = logging.getLogger("neociv")

# How many bought minds may be in flight at once. Every one is a paid api call, and a
# world with a hundred of them must not open a hundred sockets in the same instant.
MAX_CONCURRENT = 8


def due(world, cooldown: int) -> list[int]:
    """Agents whose bought mind is owed a steer.

    The cooldown is read from the same field the queue drain uses, so a bought mind cannot
    think more often than a self-hosted one however fast it is asked.
    """
    ready = []
    for entity in world.query(AttachedMind):
        mind = world.get(entity, AttachedMind)
        if not mind.enabled or not mind.model:
            continue
        if not world.is_alive(entity):
            continue
        if mind.last_steer_tick >= 0 and world.tick - mind.last_steer_tick < cooldown:
            continue
        ready.append(entity)
    return ready


def _model(catalog: surplus.Catalog, model_id: str) -> surplus.Model | None:
    for model in catalog.models:
        if model.model_id == model_id:
            return model
    return None


async def steer_one(world, entity: int, catalog: surplus.Catalog, buyer: surplus.Buyer) -> str:
    """Buy one steer for one agent. Returns "" or why nothing was bought.

    Never raises. Every outcome that is not a steer is recorded on the mind where its
    owner can read it — a model somebody chose and paid for going quiet with no reason
    given is a failure this project has already been bitten by once.
    """
    mind = world.try_get(entity, AttachedMind)
    if mind is None or not mind.model:
        return "no bought mind"

    model = _model(catalog, mind.model)
    if model is None:
        # The catalog is a live third-party list. A model that was there when it was
        # bought can be gone by the time it is used, and that must read as an explained
        # pause rather than a silent one.
        return _note(mind, f"{mind.model} is not on the price list right now")

    book = governor.book(world)
    if book is None:
        return _note(mind, "spending is not set up")

    estimate = model.units_per_steer
    refused = governor.reserve(book, entity, estimate, world.tick)
    if refused:
        return _note(mind, refused)

    steer = await buyer.steer(model.model_id, minds.perception(world, entity))

    # Whatever happened, the reservation must not survive it.
    if steer.error:
        governor.release(book, estimate)
        return _note(mind, steer.error)

    cost = steer.units(model)
    # Charge what it really cost, not what was held. `settle` squares the reservation with
    # the actual charge in one place — releasing the estimate here *and* letting `commit`
    # release the charge would give back more than was ever taken, and reservations are a
    # shared pool, so the surplus would quietly become someone else's headroom.
    governor.settle(
        book, entity, estimate, cost, world.tick, note=f"mind {entity} {model.model_id}"
    )
    mind.spent_units += cost
    mind.last_error = ""

    if not steer:
        # Paid for, and the model chose to ask for nothing. That is a real answer and the
        # state machine covers the gap, so it is not an error — but it is not a steer.
        return ""

    minds.enqueue_steer(world, entity, wants=steer.wants, say=steer.say)
    return ""


def _note(mind: AttachedMind, why: str) -> str:
    mind.last_error = why[:160]
    return mind.last_error


async def run_once(world, catalog: surplus.Catalog, buyer: surplus.Buyer) -> int:
    """One pass over every bought mind that is owed a steer. Returns how many were bought.

    Bounded concurrency, and the world is re-checked for each agent because this awaits:
    an agent can die, be detached or run out of money between being listed and being
    called.
    """
    import asyncio

    if not buyer.ready:
        return 0
    if not getattr(world.config, "attached_minds_enabled", False):
        return 0

    cooldown = max(1, getattr(world.config, "attached_mind_cooldown_ticks", 20))
    waiting = due(world, cooldown)
    if not waiting:
        return 0

    await catalog.refresh()
    bought = 0

    for start in range(0, len(waiting), MAX_CONCURRENT):
        batch = [e for e in waiting[start : start + MAX_CONCURRENT] if world.is_alive(e)]
        if not batch:
            continue
        results = await asyncio.gather(
            *(steer_one(world, entity, catalog, buyer) for entity in batch),
            return_exceptions=True,
        )
        for entity, result in zip(batch, results):
            if isinstance(result, Exception):
                # `steer_one` is written not to raise; if it ever does, one mind's bug
                # must not stop the rest from thinking.
                logger.warning("bought mind %d raised (%s)", entity, result)
                continue
            if not result:
                bought += 1
    return bought


def status(world) -> dict:
    """What the api reports about bought minds."""
    bought = []
    for entity in world.query(AttachedMind):
        mind = world.get(entity, AttachedMind)
        if not mind.model:
            continue
        bought.append(
            {
                "agent_id": entity,
                "model": mind.model,
                "spent_units": mind.spent_units,
                "steers": mind.steers,
                "last_error": mind.last_error,
            }
        )
    bought.sort(key=lambda entry: entry["agent_id"])
    return {"count": len(bought), "minds": bought}
