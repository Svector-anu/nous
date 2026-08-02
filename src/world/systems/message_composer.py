"""Rule-based natural-language message text for the social layer.

No LLM is used here.  Generating text locally keeps the $0 budget intact and lets the
simulation run deterministically: the same sender, message type, tick, and structured
content always produce the same words.  The `text` field is viewer-only cosmetics; the
structured content is what agents actually act on.

Personality only exists on user-deployed agents.  Genesis leaders (empty personality) get
default templates, so the feature is visible without requiring user deployments.
"""

from __future__ import annotations

import hashlib

from ..components import Agent, Clan, ClanGoal, MessageType, ResourceKind
from ..ecs import Entity, World

MAX_TEXT_LENGTH = 80


_PERSONALITY_TRAITS = {
    "builder": {"wood", "build", "hoard", "stockpile", "fortify", "walls"},
    "gatherer": {"food", "hungry", "gather", "forage", "harvest", "scavenge"},
    "warrior": {"war", "raid", "fight", "distrust", "stranger", "enemy", "attack"},
    "diplomat": {"peace", "friend", "trust", "help", "share", "ally", "unite"},
    "leader": {"leader", "command", "order", "follow", "march", "obey"},
}


_RALLY_TEMPLATES = {
    "default": [
        "Rally at {x},{y}",
        "Meet at {x},{y}",
        "Gather at {x},{y}",
    ],
    "builder": [
        "Build at {x},{y}",
        "Walls rise at {x},{y}",
        "Strengthen {x},{y}",
    ],
    "gatherer": [
        "Forage near {x},{y}",
        "Find food at {x},{y}",
        "Harvest at {x},{y}",
    ],
    "warrior": [
        "Hold the line at {x},{y}",
        "Defend {x},{y}",
        "Stand ready at {x},{y}",
    ],
    "diplomat": [
        "Come together at {x},{y}",
        "Join me at {x},{y}",
        "We gather at {x},{y}",
    ],
    "leader": [
        "Assemble at {x},{y}",
        "Form up at {x},{y}",
        "To me at {x},{y}",
    ],
}


_GOAL_TEMPLATES = {
    ClanGoal.GATHER_FOOD: {
        "default": ["We need food", "Find food now", "Feed the clan"],
        "gatherer": ["Hunt, gather, eat", "Bring in the harvest", "Fill our food stores"],
        "warrior": ["Take what we need", "Food by any means", "No one goes hungry"],
        "diplomat": ["Let's gather together", "Share the work", "All hands to food"],
        "leader": ["Food is the order", "Gather for the clan", "Obey: find food"],
    },
    ClanGoal.GATHER_WOOD: {
        "default": ["We need wood", "Gather wood", "Build the stores"],
        "builder": ["Wood for walls", "Stockpile timber", "Build the future"],
        "warrior": ["Axes up, wood in", "Take the timber", "Fuel and arms"],
        "diplomat": ["Wood shared is strength", "Let's gather timber", "Many hands, more wood"],
        "leader": ["Timber for the clan", "Wood now", "Cut and carry"],
    },
    ClanGoal.EXPAND: {
        "default": ["Expand our ground", "Build more huts", "Grow the settlement"],
        "builder": ["More walls, more home", "Build outward", "Stone by stone"],
        "warrior": ["Claim the ground", "Push the border", "Expand or be crushed"],
        "diplomat": ["Room for everyone", "Grow together", "Build for all"],
        "leader": ["Build the clan's reach", "Expand by my order", "New huts, now"],
    },
    ClanGoal.RALLY: {
        "default": ["Stay close", "Rally to the centre", "Keep together"],
        "diplomat": ["Unity is our strength", "Stay with us", "Together we endure"],
        "warrior": ["Circle the wagons", "Hold formation", "Stand as one"],
        "leader": ["Rally to me", "Stay in formation", "Hold the clan"],
    },
    ClanGoal.RAID: {
        "default": ["We raid", "Take from them", "Strike now"],
        "warrior": ["Blood and bread", "Hit them hard", "No mercy"],
        "diplomat": ["We do what we must", "This is our need", "Forgive, but take"],
        "leader": ["Raid by my command", "Take what is ours", "Attack"],
    },
}


_REQUEST_TEMPLATES = {
    ResourceKind.FOOD: {
        "default": ["Need food", "I'm starving", "Food, please"],
        "gatherer": ["Bring me food", "I need a harvest"],
        "warrior": ["Feed me or I fall", "I need strength to fight"],
        "diplomat": ["Can anyone spare food?", "Help me eat"],
    },
    ResourceKind.WOOD: {
        "default": ["Need wood", "Wood, please", "I need timber"],
        "builder": ["Wood for my hands", "Timber for my work"],
    },
    None: {
        "default": ["Need supplies", "Supplies, please", "I'm running low"],
    },
}


_OFFER_TEMPLATES = {
    ResourceKind.FOOD: {
        "default": ["I have food", "Take this food", "Food here"],
        "diplomat": ["Share this with me", "Food for a friend"],
        "warrior": ["Eat. Then fight.", "Fuel up"],
    },
    ResourceKind.WOOD: {
        "default": ["I have wood", "Take this wood", "Wood here"],
        "builder": ["Timber for the clan", "Wood for building"],
    },
    None: {
        "default": ["I can help", "Help here", "I have something"],
    },
}


_ALERT_TEMPLATES = {
    "raid": {
        "default": ["Raid!", "We're under attack!", "To arms!"],
        "warrior": ["Fight them off!", "Stand and defend!", "Blood for blood!"],
        "diplomat": ["Protect each other!", "Stay together!", "Hold fast!"],
        "leader": ["Defend the clan!", "Stand by me!", "Hold the line!"],
    },
    "starving": {
        "default": ["Starving!", "I need food now!", "Dying of hunger!"],
        "gatherer": ["Harvest now!", "Find food fast!"],
    },
    "default": {
        "default": ["Alarm!", "Look out!", "Danger!"],
    },
}


def _traits(personality: str) -> set[str]:
    """Map a free-form personality string to trait buckets by substring.

    This is intentionally coarse: it is a cheap, deterministic way to make a short
    note influence short messages without parsing English or calling a model.
    """
    lowered = personality.lower()
    return {name for name, words in _PERSONALITY_TRAITS.items() if any(w in lowered for w in words)}


def _choose(templates: dict[str, list[str]], traits: set[str], key: str) -> str:
    """Pick the most specific template list for the given traits, falling back to default.

    The index is derived from a stable hash of the key so the choice is identical across
    runs with the same world state.
    """
    digest = hashlib.blake2b(key.encode(), digest_size=8).digest()
    index = int.from_bytes(digest, "big")
    # Fixed priority: builder/gatherer/warrior/diplomat/leader, first match wins.
    for trait in ("builder", "gatherer", "warrior", "diplomat", "leader"):
        if trait in traits and trait in templates:
            return templates[trait][index % len(templates[trait])]
    return templates["default"][index % len(templates["default"])]


def _sender(world: World, sender: Entity) -> tuple[Agent | None, str]:
    agent = world.try_get(sender, Agent)
    return agent, (agent.personality if agent is not None else "")


def _clan_for_leader(world: World, sender: Entity) -> Clan | None:
    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        if clan.leader == sender:
            return clan
    return None


def _resource(value: str | ResourceKind | None) -> ResourceKind | None:
    if isinstance(value, ResourceKind):
        return value
    if value == ResourceKind.FOOD.value:
        return ResourceKind.FOOD
    if value == ResourceKind.WOOD.value:
        return ResourceKind.WOOD
    return None


def compose_text(
    world: World,
    sender: Entity,
    message_type: MessageType,
    content: dict,
    tick: int | None = None,
) -> str:
    """Return a short natural-language string for the message, respecting personality.

    The result is bounded to `MAX_TEXT_LENGTH` so it fits speech bubbles and feed cards.
    """
    agent, personality = _sender(world, sender)
    traits = _traits(personality)
    stable_key = f"{sender}:{message_type.value}:{tick}:{sorted(content.items())}"
    key = f"{stable_key}:{hashlib.blake2b(stable_key.encode(), digest_size=8).hexdigest()}"

    if message_type is MessageType.INFO:
        if "rally" in content:
            x, y = content["rally"]
            templates = _RALLY_TEMPLATES
            text = _choose(templates, traits, key).format(x=x, y=y)
            return text[:MAX_TEXT_LENGTH]
        if "goal" in content:
            goal_value = content["goal"]
            try:
                goal = ClanGoal(goal_value)
            except ValueError:
                goal = None
            if goal is None or goal not in _GOAL_TEMPLATES:
                return f"New goal: {goal_value}"[:MAX_TEXT_LENGTH]
            text = _choose(_GOAL_TEMPLATES[goal], traits, key)
            return text[:MAX_TEXT_LENGTH]

    if message_type is MessageType.REQUEST:
        resource = _resource(content.get("resource"))
        templates = _REQUEST_TEMPLATES.get(resource, _REQUEST_TEMPLATES[None])
        return _choose(templates, traits, key)[:MAX_TEXT_LENGTH]

    if message_type is MessageType.OFFER:
        resource = _resource(content.get("resource"))
        templates = _OFFER_TEMPLATES.get(resource, _OFFER_TEMPLATES[None])
        return _choose(templates, traits, key)[:MAX_TEXT_LENGTH]

    if message_type is MessageType.ALERT:
        reason = content.get("reason", "default")
        templates = _ALERT_TEMPLATES.get(reason, _ALERT_TEMPLATES["default"])
        return _choose(templates, traits, key)[:MAX_TEXT_LENGTH]

    return "Message"


def compose_content(
    world: World,
    sender: Entity,
    message_type: MessageType,
    content: dict,
    tick: int | None = None,
) -> dict:
    """Return a copy of `content` with a `text` field for the viewer.

    Structured fields are preserved so the simulation can still read them.
    """
    content = dict(content)
    content["text"] = compose_text(world, sender, message_type, content, tick)
    return content
