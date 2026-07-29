"""Clan leader cognition — the middle tier of the hierarchy.

The simulation core never calls the network. An advisor is *asked* for a decision on one
tick and *answers* on some later tick, exactly like a user deployment or a message: the
decision is an input to the world, not an event inside it. That is what keeps the world
reproducible with an LLM in the loop — the recorded decision, not the API call, is what
the world depends on.

Three implementations:

- `NullAdvisor`   — answers nothing. Every clan falls back to `social._choose_goal`.
                    This is the default, and it is what the whole test suite runs on.
- `ScriptedAdvisor`— answers from a fixed table. Deterministic, offline, used by tests.
- `ClaudeAdvisor` — calls Claude on a background thread. Only clan leaders, hard-capped.

Bottom-tier agents never get an advisor. At one tick per second and hundreds of agents,
per-agent inference is not a cost problem to optimise — it is arithmetic that does not
work, and the FSM is what makes this world runnable at all.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

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
    def submit(self, brief: GoalBrief) -> bool:
        """Ask for a decision. Returns False when refused (budget, capacity, disabled)."""

    def collect(self) -> list[GoalDecision]:
        """Decisions that have arrived since the last call. Never blocks."""

    def pending(self) -> int: ...

    def inflight_clans(self) -> set[int]:
        """Clans with a request actually in flight in *this* process."""

    def close(self) -> None: ...


class NullAdvisor:
    """The default. Answers nothing, so every clan uses the rule-based goal."""

    def submit(self, brief: GoalBrief) -> bool:
        return False

    def collect(self) -> list[GoalDecision]:
        return []

    def pending(self) -> int:
        return 0

    def inflight_clans(self) -> set[int]:
        return set()

    def close(self) -> None:
        return None


class ScriptedAdvisor:
    """Deterministic stand-in for tests: answers from a table, after a fixed lag."""

    def __init__(self, answers: dict[int, str], lag_calls: int = 1, reason: str = "scripted"):
        self.answers = answers
        self.lag_calls = max(0, lag_calls)
        self.reason = reason
        self.calls = 0
        self._queue: list[tuple[int, GoalDecision]] = []

    def submit(self, brief: GoalBrief) -> bool:
        goal = self.answers.get(brief.clan_id)
        if goal is None:
            return False
        self.calls += 1
        self._queue.append(
            (
                self.lag_calls,
                GoalDecision(
                    brief.clan_id,
                    goal,
                    self.reason,
                    source="llm",
                    rules_goal=brief.rules_goal,
                    prompt=brief.as_prompt(),
                    raw_response=f'{{"goal": "{goal}", "reason": "{self.reason}"}}',
                ),
            )
        )
        return True

    def collect(self) -> list[GoalDecision]:
        ready = [decision for countdown, decision in self._queue if countdown <= 0]
        self._queue = [
            (countdown - 1, decision) for countdown, decision in self._queue if countdown > 0
        ]
        return ready

    def pending(self) -> int:
        return len(self._queue)

    def inflight_clans(self) -> set[int]:
        return {decision.clan_id for _, decision in self._queue}

    def close(self) -> None:
        self._queue.clear()


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

_RESPONSE_SCHEMA = {
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
    """Every limit that stops this becoming an unbounded bill."""

    max_inflight: int = 2
    max_calls_per_session: int = 200
    timeout_seconds: float = 30.0
    calls_made: int = 0

    def has_headroom(self, inflight: int) -> bool:
        return inflight < self.max_inflight and self.calls_made < self.max_calls_per_session


class ClaudeAdvisor:
    """Calls Claude on a worker thread. Never blocks the tick loop.

    Every failure mode — no SDK, no credentials, timeout, malformed answer, budget
    exhausted — resolves to "no decision", which the leadership system reads as "use the
    rules". There is no path where a failed call stalls or breaks the world.
    """

    def __init__(
        self,
        model: str = "claude-opus-5",
        budget: AdvisorBudget | None = None,
        effort: str = "low",
        max_tokens: int = 2048,
    ) -> None:
        self.model = model
        self.budget = budget if budget is not None else AdvisorBudget()
        self.effort = effort
        self.max_tokens = max_tokens
        self._client = None
        self._pool: ThreadPoolExecutor | None = None
        self._inflight: list[tuple[Future, GoalBrief, float]] = []
        self._unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self._ensure_client() is not None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if self._unavailable_reason is not None:
            return None
        try:
            import anthropic
        except ImportError:
            self._unavailable_reason = "anthropic sdk not installed"
            logger.warning("llm advisor disabled: %s", self._unavailable_reason)
            return None
        try:
            self._client = anthropic.Anthropic()
        except Exception as error:  # noqa: BLE001 - any construction failure disables it
            self._unavailable_reason = f"client init failed: {error}"
            logger.warning("llm advisor disabled: %s", self._unavailable_reason)
            return None
        self._pool = ThreadPoolExecutor(max_workers=self.budget.max_inflight, thread_name_prefix="advisor")
        return self._client

    def _ask(self, brief: GoalBrief) -> GoalDecision | None:
        client = self._client
        if client is None:
            return None
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA},
            },
            messages=[{"role": "user", "content": brief.as_prompt()}],
        )
        if response.stop_reason == "refusal":
            logger.warning("clan %d: model declined", brief.clan_id)
            return None

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            return None
        payload = json.loads(text)
        return GoalDecision(
            clan_id=brief.clan_id,
            goal=payload["goal"],
            reason=payload.get("reason", ""),
            source="llm",
            rules_goal=brief.rules_goal,
            prompt=brief.as_prompt(),
            raw_response=text,
        )

    @staticmethod
    def _is_auth_failure(error: Exception) -> bool:
        """Permanent credential problems, as opposed to a transient network blip.

        Two distinct shapes. A *rejected* credential raises a typed SDK error. A *missing*
        one raises a plain TypeError from the client before any request is made — caught
        here by message, because letting it through spends the entire session budget
        rediscovering the same permanent fact one doomed call at a time.
        """
        try:
            import anthropic
        except ImportError:
            return False
        if isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return True
        return isinstance(error, TypeError) and "could not resolve authentication" in str(error).lower()

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
                    logger.warning("clan %d: advisor timed out, falling back to rules", brief.clan_id)
                else:
                    still_running.append((future, brief, started))
                continue

            try:
                decision = future.result()
            except Exception as error:  # noqa: BLE001 - any failure falls back to rules
                if self._is_auth_failure(error):
                    # Credentials are wrong or absent. Disable permanently rather than
                    # spending the whole session budget discovering that 200 times.
                    self._unavailable_reason = f"authentication failed: {error}"
                    self._client = None
                    logger.warning("llm advisor disabled: %s", self._unavailable_reason)
                else:
                    logger.warning(
                        "clan %d: advisor failed (%s), falling back to rules",
                        brief.clan_id,
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


def build_advisor(config) -> GoalAdvisor:
    """A live advisor only when explicitly enabled.

    Credentials are deliberately *not* checked here. An unset ANTHROPIC_API_KEY does not
    mean there are no credentials — the SDK also resolves an auth token or an
    `ant auth login` profile — so gating on the env var would refuse a working setup.
    Instead the advisor tries, and disables itself permanently on the first
    authentication failure.
    """
    if not getattr(config, "llm_enabled", False):
        return NullAdvisor()
    return ClaudeAdvisor(
        model=config.llm_model,
        budget=AdvisorBudget(
            max_inflight=config.llm_max_inflight,
            max_calls_per_session=config.llm_max_calls_per_session,
            timeout_seconds=config.llm_timeout_seconds,
        ),
        effort=config.llm_effort,
    )
