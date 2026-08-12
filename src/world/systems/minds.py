"""Minds a visitor brought: something outside this process driving one agent.

The world holds around a hundred agents and can afford to think for almost none of them.
A mind a visitor attaches arrives with its own budget, so the number of thinking agents
grows without the operator's bill growing with it. That is the whole economic argument.

**The mind calls in; the world never calls out.** An earlier draft had the server post to
an endpoint the visitor supplied, which would have made Nous an http client of fifty
strangers: server-side request forgery, dns rebinding, redirect handling, connection
pinning to a validated address, and a thread pool to keep all of it off the tick. Every
one of those defences is a thing that can be got subtly wrong. Inverting the direction
deletes the class instead of defending it, and costs nothing — the state machine already
covers every gap, so a mind that says nothing is a mind that changed nothing.

Three rules hold the rest together.

**A mind picks differently, never better.** It sets `wants` — food or wood — and may say
something. Both values were already reachable by the state machine; the agent gains no
reach, no speed, no exemption from hunger. Without this a paid mind is a bought advantage
and the world becomes a game about who spent money.

**A steer is an input, so it queues.** Applying it when the request lands would make
history depend on wall clock. It drains at a fixed point in the tick, like every other
user input here.

**Absence is the fallback.** A mind that stops posting stops steering, and the fsm takes
over. Nothing to detect, no failure counter, no back-off.
"""

from __future__ import annotations

import hashlib
import hmac

from ..components import AttachedMind, Agent, MindQueue, Outbox, ResourceKind

# What a mind is allowed to ask for. Anything else is discarded rather than guessed at:
# a mind that returns nonsense should change nothing, not something arbitrary.
WANTS = {"food": ResourceKind.FOOD, "wood": ResourceKind.WOOD}

# Speech is bounded. It is rendered in a viewer and stored in world state, and a caller is
# free to post a megabyte.
MAX_SAY = 160

# The queue is bounded like every other one here — a seam a stranger can append to is an
# unbounded accumulator unless something says otherwise.
MAX_PENDING = 64


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def token_matches(mind: AttachedMind, token: str) -> bool:
    """Constant-time, so the comparison cannot be turned into an oracle that leaks the
    hash a character at a time."""
    if not mind.token_hash or not token:
        return False
    return hmac.compare_digest(mind.token_hash, hash_token(token))


def queue(world) -> MindQueue | None:
    entity = world.first(MindQueue)
    return None if entity is None else world.get(entity, MindQueue)


def perception(world, entity) -> dict:
    """What a mind is told about its agent. Small and stable on purpose — it becomes a
    public interface the moment anybody builds against it, and every field added is one
    that can never quietly change shape again.

    Nothing here is private: the same values are already on /state for anyone to read.
    """
    from ..components import ClanRef, Inventory, Needs

    agent = world.try_get(entity, Agent)
    if agent is None:
        return {}
    needs = world.try_get(entity, Needs)
    inventory = world.try_get(entity, Inventory)
    reference = world.try_get(entity, ClanRef)
    return {
        "agent_id": entity,
        "name": agent.name,
        "tick": world.tick,
        "state": agent.state.value,
        "wants": agent.wants.value,
        "personality": agent.personality,
        "hunger": needs.hunger if needs is not None else 0,
        "energy": needs.energy if needs is not None else 0,
        "food": inventory.food if inventory is not None else 0,
        "wood": inventory.wood if inventory is not None else 0,
        "clan": reference.clan_id if reference is not None else None,
        # The only two answers that mean anything, sent so a mind does not have to guess
        # the vocabulary from documentation it may not have read.
        "choices": sorted(WANTS),
    }


def enqueue_steer(world, entity, wants: str = "", say: str = "") -> bool:
    """Accept a steer for a later tick. Caller has already proved it owns the agent.

    Bounded and de-duplicated: a mind that posts twice before a tick lands gets its
    latest instruction applied, not both. Without that, posting in a loop would let one
    mind fill the queue and starve every other.
    """
    pending = queue(world)
    if pending is None:
        return False
    entry = {"agent_id": entity, "wants": str(wants or ""), "say": str(say or "")[:MAX_SAY]}
    pending.pending = [e for e in pending.pending if e.get("agent_id") != entity]
    pending.pending.append(entry)
    del pending.pending[:-MAX_PENDING]
    return True


def apply(world, entity, mind: AttachedMind, answer: dict) -> bool:
    """Apply one steer. Returns whether anything actually changed.

    Every field is optional and every unknown value is dropped. A mind that sends an empty
    object, a misspelled want or a novel action changes nothing — which is what keeps a
    third party from putting this world into a state its own rules could not reach.
    """
    agent = world.try_get(entity, Agent)
    if agent is None:
        return False

    changed = False
    want = str(answer.get("wants", "")).strip().lower()
    if want in WANTS and agent.wants is not WANTS[want]:
        agent.wants = WANTS[want]
        changed = True

    said = str(answer.get("say", "")).strip()[:MAX_SAY]
    if said:
        mind.last_said = said
        outbox = world.try_get(entity, Outbox)
        if outbox is not None:
            # Through the ordinary message path, so a mind's speech is delivered on the
            # same one-tick lag as everything else agents say to each other. A mind that
            # could talk faster than the world does would be its own kind of advantage.
            outbox.messages.append({"from": entity, "kind": "say", "text": said})
        changed = True
    return changed


def run(world, rng) -> None:
    """Drain the steers queued since the last tick."""
    pending = queue(world)
    if pending is None or not pending.pending:
        return

    config = world.config
    if not getattr(config, "attached_minds_enabled", False):
        # Enabled is checked here rather than at the door so a world switched off simply
        # ignores what it was sent, instead of accumulating it against being switched on.
        pending.pending.clear()
        return

    cooldown = max(1, getattr(config, "attached_mind_cooldown_ticks", 20))
    # Ascending, so a reloaded world applies them in the same order.
    entries = sorted(pending.pending, key=lambda e: e.get("agent_id", 0))
    pending.pending.clear()

    for entry in entries:
        entity = entry.get("agent_id")
        if entity is None or not world.is_alive(entity):
            continue
        mind = world.try_get(entity, AttachedMind)
        if mind is None or not mind.enabled:
            continue
        # The rate limit lives here, not at the door: a mind may post as often as it likes
        # and the world still only listens on its own cadence.
        if mind.last_steer_tick >= 0 and world.tick - mind.last_steer_tick < cooldown:
            continue
        # Marked because the steer was *accepted*, not because it changed something. A
        # mind that asks for what the rules already chose has still used its turn, and
        # gating the cooldown on a state change made the rate limit both unobservable and
        # dependent on what the state machine happened to pick that tick.
        mind.last_steer_tick = world.tick
        mind.steers += 1
        apply(world, entity, mind, entry)


def status(world) -> dict:
    """Read-only summary for the viewer and the api."""
    attached = list(world.query(AttachedMind))
    cooldown = max(1, getattr(world.config, "attached_mind_cooldown_ticks", 20))
    # A mind that has steered within a few cooldowns is alive; one that has not has simply
    # stopped, and its agent is back on the state machine with nothing else needed.
    fresh = sum(
        1
        for entity in attached
        if (mind := world.get(entity, AttachedMind)).enabled
        and mind.last_steer_tick >= 0
        and world.tick - mind.last_steer_tick <= cooldown * 3
    )
    pending = queue(world)
    return {
        "enabled": bool(getattr(world.config, "attached_minds_enabled", False)),
        "attached": len(attached),
        "steering": fresh,
        "pending": len(pending.pending) if pending is not None else 0,
    }
