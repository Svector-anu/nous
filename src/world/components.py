"""Phase 0 components.

Needs carries all four drives named in planish/AGENT_SYSTEM.md. Phase 0 only
decays energy and hunger; social and safety are stored so the FSM can start
consuming them in Phase 1 without a schema change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .ecs import Entity


class AgentState(str, Enum):
    IDLE = "IDLE"
    SEEK_NEED = "SEEK_NEED"
    GATHER = "GATHER"
    BUILD = "BUILD"
    REST = "REST"
    FLEE = "FLEE"
    FOLLOW = "FOLLOW"
    MEET = "MEET"


class MessageType(str, Enum):
    INFO = "info"
    REQUEST = "request"
    OFFER = "offer"
    ALERT = "alert"


BROADCAST_CLAN = "clan"
BROADCAST_ALL = "all"


class ResourceKind(str, Enum):
    FOOD = "FOOD"
    WOOD = "WOOD"


@dataclass
class Position:
    x: int
    y: int


@dataclass
class Needs:
    energy: int
    hunger: int
    social: int
    safety: int


@dataclass
class Inventory:
    food: int = 0
    wood: int = 0

    def total(self) -> int:
        return self.food + self.wood


@dataclass
class ClanRef:
    clan_id: int | None = None


@dataclass
class Agent:
    name: str
    state: AgentState = AgentState.IDLE
    target_entity: Entity | None = None
    target_x: int | None = None
    target_y: int | None = None
    gather_progress: int = 0
    wants: ResourceKind = ResourceKind.FOOD
    huts_owned: int = 0
    last_request_tick: int = -1
    received: int = 0
    personality: str = ""
    user_deployed: bool = False

    def clear_target(self) -> None:
        self.target_entity = None
        self.target_x = None
        self.target_y = None
        self.gather_progress = 0


@dataclass
class ResourceNode:
    """Nodes are never destroyed. A fully foraged node goes dormant at amount 0
    and climbs back toward max_amount, one unit every regrow_every ticks."""

    kind: ResourceKind
    amount: int
    max_amount: int
    regrow_every: int

    @property
    def is_dormant(self) -> bool:
        return self.amount <= 0


@dataclass
class Building:
    kind: str
    owner: Entity | None = None


class ClanGoal(str, Enum):
    GATHER_FOOD = "gather_food"
    GATHER_WOOD = "gather_wood"
    EXPAND = "expand"
    RALLY = "rally"


KEY_FOOD_LOCATIONS = "food_locations"
KEY_WOOD_LOCATIONS = "wood_locations"


def clan_rally_key(clan_id: int) -> str:
    return f"clan:{clan_id}:rally"


def clan_goal_key(clan_id: int) -> str:
    return f"clan:{clan_id}:goal"


def clan_centre_key(clan_id: int) -> str:
    return f"clan:{clan_id}:centre"


@dataclass
class Blackboard:
    """Global key-value store, held by a single world entity.

    Each entry is [value, written_by, written_tick]. The tick stamp is what the
    blackboard system uses to expire stale knowledge — a food sighting from 400 ticks
    ago is worse than no sighting at all.
    """

    entries: dict[str, list] = field(default_factory=dict)

    def write(self, key: str, value: object, written_by: Entity, tick: int) -> None:
        # Keys are kept in sorted order. A save round-trips through
        # json.dumps(sort_keys=True), so insertion order would otherwise differ between
        # a live world and a reloaded one — identical content, different iteration.
        is_new = key not in self.entries
        self.entries[key] = [value, written_by, tick]
        if is_new:
            self.entries = dict(sorted(self.entries.items()))

    def read(self, key: str) -> object | None:
        entry = self.entries.get(key)
        return entry[0] if entry is not None else None

    def written_by(self, key: str) -> Entity | None:
        entry = self.entries.get(key)
        return entry[1] if entry is not None else None

    def age(self, key: str, tick: int) -> int | None:
        entry = self.entries.get(key)
        return tick - entry[2] if entry is not None else None

    def expire(self, tick: int, ttl: int) -> list[str]:
        stale = sorted(key for key, e in self.entries.items() if tick - e[2] > ttl)
        for key in stale:
            del self.entries[key]
        return stale


@dataclass
class SpawnQueue:
    """Deployment requests waiting to enter the world, held by a single world entity.

    A user deploying an agent is an *input* to the simulation, so it cannot be applied
    the moment an http request lands — that would make history depend on wall clock.
    Requests queue here and are drained at a fixed point in the tick, with position and
    needs drawn from the tick's rng stream. Same seed plus same deployments at the same
    ticks reproduces the same world.
    """

    pending: list[dict] = field(default_factory=list)


@dataclass
class Inbox:
    """Messages delivered at the start of this tick. Bounded: an unread inbox must
    never become an unbounded accumulator."""

    messages: list[dict] = field(default_factory=list)


@dataclass
class Outbox:
    """Messages queued this tick, delivered at the start of the next one."""

    messages: list[dict] = field(default_factory=list)


@dataclass
class Clan:
    clan_id: int
    leader: Entity | None = None
    members: list[Entity] = field(default_factory=list)
    last_rally: list | None = None
    goal: str = ClanGoal.RALLY.value
    goal_set_tick: int = 0
    centre: list | None = None
    influence_radius: int = 0

    def add(self, entity: Entity) -> None:
        if entity not in self.members:
            self.members.append(entity)
            self.members.sort()
        if self.leader is None:
            self.leader = entity

    def remove(self, entity: Entity) -> None:
        if entity in self.members:
            self.members.remove(entity)
        if self.leader == entity:
            self.leader = self.members[0] if self.members else None
