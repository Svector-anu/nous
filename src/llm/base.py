"""Provider-neutral advisor machinery.

Everything here is shared by every backend: the decision types, the prompt, the schema,
the budget, and the threading. A concrete provider supplies three things and nothing
else — how to build its client, how to ask one question, and how to recognise a
permanent credential failure.

The guarantees do not belong to any provider. They belong here:

- the simulation core never calls the network; a provider is asked on one tick and
  answers on a later one
- a decision is a durable input, recorded in the world, never a live dependency
- the rules are the permanent fallback, so every failure resolves to "no decision"
- spend and outstanding requests live in `AdvisorState`, which is persisted
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger("neociv.llm")

GOALS = ("gather_food", "gather_wood", "expand", "rally", "raid")


@dataclass(frozen=True)
class GoalBrief:
    """What a leader knows when it decides. Deliberately small — it is the prompt."""

    clan_id: int
    tick: int
    members: int
    mean_hunger: float
    food_ratio: float
    huts_per_member: float
    wood_held: int
    current_goal: str
    nearby_clans: int
    recent_raids_suffered: int
    # What the rule-based _choose_goal would pick in this exact state. Carried purely so
    # every model answer can be compared against the rules it is meant to beat.
    rules_goal: str = ""

    def as_prompt(self) -> str:
        return (
            f"tick: {self.tick}\n"
            f"clan size: {self.members}\n"
            f"current goal: {self.current_goal}\n"
            f"mean hunger (0 starving, 100 full): {self.mean_hunger:.0f}\n"
            f"food stores (0 empty, 1 full): {self.food_ratio:.2f}\n"
            f"huts per member (target {3}): {self.huts_per_member:.1f}\n"
            f"wood held by clan: {self.wood_held}\n"
            f"other clans within sight: {self.nearby_clans}\n"
            f"raids suffered recently: {self.recent_raids_suffered}"
        )


@dataclass(frozen=True)
class GoalDecision:
    clan_id: int
    goal: str
    reason: str
    source: str
    latency_ms: int = 0
    rules_goal: str = ""
    prompt: str = ""
    raw_response: str = ""

    def is_valid(self) -> bool:
        return self.goal in GOALS

    @property
    def verdict(self) -> str:
        """How the model's choice compares to the rules it is meant to improve on."""
        if not self.rules_goal:
            return "unknown"
        return "same" if self.goal == self.rules_goal else "differs"


class GoalAdvisor(Protocol):
    """The whole surface the simulation talks to. Four methods, none of them blocking."""

    def submit(self, brief: GoalBrief) -> bool:
        """Ask for a decision. Returns False when refused (budget, capacity, disabled)."""

    def collect(self) -> list[GoalDecision]:
        """Decisions that have arrived since the last call. Never blocks."""

    def pending(self) -> int: ...

    def inflight_clans(self) -> set[int]:
        """Clans with a request actually in flight in *this* process."""

    def close(self) -> None: ...


SYSTEM_PROMPT = """You are the leader of a clan in a persistent agent civilization.

Each tick, agents act on a fixed state machine: they forage food and wood, build huts, \
eat, rest, and starve if their energy reaches zero. Resting is unavailable while hunger \
is at zero, so a starving clan cannot recover by resting. Food regrows slowly; the margin \
between supply and demand is thin.

You set ONE goal for your clan. Members bias their behaviour toward it, but survival \
always wins: a starving member goes for food no matter what you choose.

- gather_food: bias members toward foraging food. Choose when stores are low or members \
are hungry.
- gather_wood: bias members toward foraging wood. Choose when short of huts and timber.
- expand: build huts inside the clan's territory. Choose when short of huts but holding \
wood. Each member can own at most 3 huts; once every member is built out this achieves \
nothing.
- rally: gather at a meeting point. Choose when fed and built out — it costs foraging \
time, so it is the goal of a comfortable clan.
- raid: take food from non-clanmates by force. Members only rob someone they are already \
standing next to; they never give chase. Fights cost both sides energy and can kill. \
Choose only in genuine famine — it is a last resort, not a strategy, and a raiding clan \
that was not desperate simply loses energy for nothing.

Answer with the goal and one short sentence of reasoning. Be decisive."""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "enum": list(GOALS)},
        "reason": {"type": "string", "description": "One short sentence."},
    },
    "required": ["goal", "reason"],
    "additionalProperties": False,
}


@dataclass
class AdvisorBudget:
    """In-process guard rails.

    The *durable* spend counter lives in `AdvisorState` and is the real authority — this
    is a second line of defence inside one process, so a bug in the leadership system
    cannot produce an unbounded burst.
    """

    max_inflight: int = 2
    max_calls_per_session: int = 200
    timeout_seconds: float = 30.0
    calls_made: int = 0

    def has_headroom(self, inflight: int) -> bool:
        return inflight < self.max_inflight and self.calls_made < self.max_calls_per_session


class ThreadedAdvisor(ABC):
    """Shared implementation for any advisor that calls a remote model.

    Subclasses implement `_create_client`, `_ask`, and optionally `_is_auth_failure`.
    Everything that makes the contract safe — never blocking the tick loop, containing
    every exception, self-disabling on bad credentials, honouring the budget — is here,
    so a new provider cannot forget it.
    """

    name = "threaded"

    def __init__(
        self,
        model: str,
        budget: AdvisorBudget | None = None,
        max_tokens: int = 2048,
    ) -> None:
        self.model = model
        self.budget = budget if budget is not None else AdvisorBudget()
        self.max_tokens = max_tokens
        self._client: Any = None
        self._pool: ThreadPoolExecutor | None = None
        self._inflight: list[tuple[Future, GoalBrief, float]] = []
        self._unavailable_reason: str | None = None

    # --- provider hooks -----------------------------------------------------

    @abstractmethod
    def _create_client(self) -> Any:
        """Build the provider client, or raise. Raising disables the advisor."""

    @abstractmethod
    def _ask(self, brief: GoalBrief) -> GoalDecision | None:
        """One request. Runs on a worker thread; may raise."""

    def _is_auth_failure(self, error: Exception) -> bool:
        """Permanent credential problems, as opposed to a transient network blip."""
        return False

    # --- shared machinery ---------------------------------------------------

    @property
    def available(self) -> bool:
        return self._ensure_client() is not None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._unavailable_reason is not None:
            return None
        try:
            self._client = self._create_client()
        except Exception as error:  # noqa: BLE001 - any construction failure disables it
            self._unavailable_reason = f"client init failed: {error}"
            logger.warning("%s advisor disabled: %s", self.name, self._unavailable_reason)
            return None
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, self.budget.max_inflight), thread_name_prefix=f"advisor-{self.name}"
        )
        return self._client

    def _disable(self, reason: str) -> None:
        self._unavailable_reason = reason
        self._client = None
        logger.warning("%s advisor disabled: %s", self.name, reason)

    def submit(self, brief: GoalBrief) -> bool:
        if self._ensure_client() is None:
            return False
        if not self.budget.has_headroom(len(self._inflight)):
            return False
        assert self._pool is not None
        self.budget.calls_made += 1
        self._inflight.append((self._pool.submit(self._ask, brief), brief, time.monotonic()))
        return True

    def collect(self) -> list[GoalDecision]:
        decisions: list[GoalDecision] = []
        still_running: list[tuple[Future, GoalBrief, float]] = []

        for future, brief, started in self._inflight:
            elapsed = time.monotonic() - started

            if not future.done():
                if elapsed > self.budget.timeout_seconds:
                    future.cancel()
                    logger.warning(
                        "clan %d: %s advisor timed out, falling back to rules",
                        brief.clan_id,
                        self.name,
                    )
                else:
                    still_running.append((future, brief, started))
                continue

            try:
                decision = future.result()
            except Exception as error:  # noqa: BLE001 - any failure falls back to rules
                if self._is_auth_failure(error):
                    self._disable(f"authentication failed: {error}")
                else:
                    logger.warning(
                        "clan %d: %s advisor failed (%s), falling back to rules",
                        brief.clan_id,
                        self.name,
                        error,
                    )
                continue

            if decision is None or not decision.is_valid():
                continue
            decisions.append(
                GoalDecision(
                    decision.clan_id,
                    decision.goal,
                    decision.reason,
                    decision.source,
                    latency_ms=int(elapsed * 1000),
                    rules_goal=decision.rules_goal,
                    prompt=decision.prompt,
                    raw_response=decision.raw_response,
                )
            )

        self._inflight = still_running
        return decisions

    def pending(self) -> int:
        return len(self._inflight)

    def inflight_clans(self) -> set[int]:
        return {brief.clan_id for _, brief, _ in self._inflight}

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        self._inflight.clear()
