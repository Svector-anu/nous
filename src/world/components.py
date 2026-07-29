"""Phase 0 components.

Needs carries all four drives named in planish/AGENT_SYSTEM.md. Phase 0 only
decays energy and hunger; social and safety are stored so the FSM can start
consuming them in Phase 1 without a schema change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .ecs import Entity


class AgentState(str, Enum):
    IDLE = "IDLE"
    SEEK_NEED = "SEEK_NEED"
    GATHER = "GATHER"
    BUILD = "BUILD"
    REST = "REST"
    FLEE = "FLEE"


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
