"""Minds a visitor brought: something outside this process deciding what one agent wants.

The world holds around a hundred agents and can afford to think for almost none of them.
A mind attached by a visitor arrives with its own budget, so the number of thinking agents
grows without the operator's bill growing with it. That is the whole economic argument,
and it is why this exists at all.

Three rules hold the design together.

**A mind picks differently, never better.** It sets `wants` — food or wood — and may say
something. Both values were already reachable by the state machine; the agent gains no
reach, no speed, no exemption from hunger. An agent with a mind starves exactly as fast as
one without. Without this, a paid mind is a bought advantage and the world becomes a game
about who spent money.

**A mind may never delay a tick.** The client submits and returns; answers are applied
whenever they arrive, and until then the state machine decides as it always did. This is
the same contract the clan advisor has, for the same reason — an endpoint on somebody
else's laptop is exactly as unreliable as it sounds.

**A broken endpoint costs less each time.** Failures back off, so one dead url does not
buy a request every cooldown for the rest of the world's life.
"""

from __future__ import annotations

from ..components import AttachedMind, Agent, Outbox, ResourceKind

# What a mind is allowed to ask for. Anything else is discarded rather than guessed at:
# a mind that returns nonsense should change nothing, not something arbitrary.
WANTS = {"food": ResourceKind.FOOD, "wood": ResourceKind.WOOD}

# Speech is bounded. It is rendered in a viewer and stored in world state, and an endpoint
# is free to return a megabyte.
MAX_SAY = 160

# After this many consecutive failures a mind is left alone until its owner fixes it.
MAX_FAILURES = 5


class NullMinds:
    """The default. No visitor has attached anything, so nothing is asked."""

    def submit(self, request: dict) -> bool:
        return False

    def collect(self) -> list[dict]:
        return []

    def pending(self) -> int:
        return 0


def _due(mind: AttachedMind, tick: int, cooldown: int) -> bool:
    if not mind.enabled or not mind.endpoint:
        return False
    if mind.failures >= MAX_FAILURES:
        return False
    return mind.last_tick < 0 or tick - mind.last_tick >= cooldown


def _perception(world, entity, agent: Agent) -> dict:
    """What a mind is told. Small and stable on purpose — it becomes a public interface
    the moment anybody builds against it, and every field added is one that can never
    quietly change shape again."""
    from ..components import Needs, Inventory, ClanRef

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


def apply(world, entity, mind: AttachedMind, answer: dict) -> bool:
    """Apply one answer. Returns whether anything actually changed.

    Every field is optional and every unknown value is dropped. A mind that returns an
    empty object, a misspelled want or a novel action changes nothing — which is the
    behaviour that keeps a third party from being able to put this world into a state its
    own rules could not reach.
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
            # could talk faster than the world does would be a different kind of advantage.
            outbox.messages.append({"from": entity, "kind": "say", "text": said})
        changed = True
    return changed


def run(world, rng) -> None:
    """Ask any agent whose mind is due, and apply whatever has come back.

    Collect first, then submit. An answer to the previous request is worth applying now;
    a request made this tick cannot be.
    """
    client = getattr(world, "minds", None)
    if client is None:
        return

    config = world.config
    if not getattr(config, "attached_minds_enabled", False):
        return

    for answer in client.collect():
        entity = answer.get("agent_id")
        if entity is None or not world.is_alive(entity):
            continue
        mind = world.try_get(entity, AttachedMind)
        if mind is None:
            continue
        if answer.get("failed"):
            mind.failures += 1
            continue
        mind.failures = 0
        apply(world, entity, mind, answer)

    cooldown = max(1, getattr(config, "attached_mind_cooldown_ticks", 20))
    for entity in world.query(AttachedMind, Agent):
        mind = world.get(entity, AttachedMind)
        if not _due(mind, world.tick, cooldown):
            continue
        agent = world.get(entity, Agent)
        # Marked before the answer arrives. A request that fails or never returns must
        # still cost a cooldown, or a dead endpoint is asked again every single tick.
        mind.last_tick = world.tick
        try:
            client.submit({"endpoint": mind.endpoint, "perception": _perception(world, entity, agent)})
        except Exception:  # noqa: BLE001 - a client failure is never the world's problem
            mind.failures += 1


def status(world) -> dict:
    """Read-only summary for the viewer and the api."""
    attached = list(world.query(AttachedMind))
    live = 0
    for entity in attached:
        mind = world.get(entity, AttachedMind)
        if mind.enabled and mind.endpoint and mind.failures < MAX_FAILURES:
            live += 1
    client = getattr(world, "minds", None)
    return {
        "enabled": bool(getattr(world.config, "attached_minds_enabled", False)),
        "attached": len(attached),
        "answering": live,
        "pending": client.pending() if client is not None else 0,
    }
