"""Advisor selection, and the offline advisors the tests run on.

This module is the public surface — the simulation imports from here and never from a
provider module, so adding a backend touches nothing outside `src/llm/`.

Provider is chosen by config:

    llm_enabled = True
    llm_provider = "anthropic" | "xai" | "openai" | "none"

Every backend inherits the same contract from `ThreadedAdvisor`: never blocks the tick
loop, contains every exception, self-disables on bad credentials, honours the budget.
`AdvisorState` in the world holds the durable spend and outstanding requests, so those
guarantees survive a restart regardless of which provider is in use.
"""

from __future__ import annotations

from .anthropic_advisor import ClaudeAdvisor
from .base import (
    GOALS,
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    AdvisorBudget,
    GoalAdvisor,
    GoalBrief,
    GoalDecision,
    ThreadedAdvisor,
    logger,
)
from .openai_advisor import (
    DGRID_BASE_URL,
    OPENAI_BASE_URL,
    XAI_BASE_URL,
    DGridAdvisor,
    GrokAdvisor,
    OpenAICompatibleAdvisor,
)

__all__ = [
    "GOALS",
    "RESPONSE_SCHEMA",
    "SYSTEM_PROMPT",
    "AdvisorBudget",
    "ClaudeAdvisor",
    "DGridAdvisor",
    "GoalAdvisor",
    "GoalBrief",
    "GoalDecision",
    "GrokAdvisor",
    "NullAdvisor",
    "OpenAICompatibleAdvisor",
    "PROVIDERS",
    "ScriptedAdvisor",
    "ThreadedAdvisor",
    "build_advisor",
]

PROVIDERS = ("none", "anthropic", "xai", "openai", "dgrid")


class NullAdvisor:
    """The default. Answers nothing, so every clan uses the rule-based goal."""

    name = "none"

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

    name = "scripted"

    def __init__(
        self,
        answers: dict[int, str],
        lag_calls: int = 1,
        reason: str = "scripted",
        messages: dict[int, str] | None = None,
    ):
        self.answers = answers
        self.messages = messages or {}
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
                    message=self.messages.get(brief.clan_id, ""),
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


def build_advisor(config) -> GoalAdvisor:
    """Pick a backend from config.

    Credentials are deliberately not checked here. An unset key does not always mean
    there are no credentials — the Anthropic SDK also resolves an auth token or an
    `ant auth login` profile — so gating on an env var would refuse a working setup.
    Instead the advisor tries, and disables itself permanently on the first
    authentication failure.
    """
    if not getattr(config, "llm_enabled", False):
        return NullAdvisor()

    provider = (getattr(config, "llm_provider", "anthropic") or "none").strip().lower()
    if provider in ("none", ""):
        return NullAdvisor()
    if provider not in PROVIDERS:
        logger.warning(
            "unknown llm_provider %r (expected one of %s); clan goals stay rule-based",
            provider,
            ", ".join(PROVIDERS),
        )
        return NullAdvisor()

    budget = AdvisorBudget(
        max_inflight=config.llm_max_inflight,
        max_calls_per_session=config.llm_max_calls_per_session,
        timeout_seconds=config.llm_timeout_seconds,
    )
    model = getattr(config, "llm_model", "") or ""
    base_url = (getattr(config, "llm_base_url", "") or "").strip()

    if provider == "anthropic":
        return ClaudeAdvisor(model=model, budget=budget, effort=config.llm_effort)

    if provider == "dgrid":
        return DGridAdvisor(
            model=model,
            base_url=base_url or DGRID_BASE_URL,
            api_key_env=getattr(config, "llm_api_key_env", "") or "DGRID_API_KEY",
            budget=budget,
        )

    if provider == "xai":
        return GrokAdvisor(
            model=model,
            base_url=base_url or XAI_BASE_URL,
            api_key_env=getattr(config, "llm_api_key_env", "") or "XAI_API_KEY",
            budget=budget,
        )

    # "openai" — also the path for OpenRouter, Ollama, vLLM, LM Studio and friends,
    # which differ only by base_url and whether they want a key at all.
    resolved = base_url or OPENAI_BASE_URL
    return OpenAICompatibleAdvisor(
        model=model,
        base_url=resolved,
        api_key_env=getattr(config, "llm_api_key_env", "") or "OPENAI_API_KEY",
        budget=budget,
        require_api_key=resolved == OPENAI_BASE_URL,
    )
