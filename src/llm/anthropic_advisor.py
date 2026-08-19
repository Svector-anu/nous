"""Claude backend, via the official Anthropic SDK."""

from __future__ import annotations

import json

from .base import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    AdvisorBudget,
    GoalBrief,
    GoalDecision,
    ThreadedAdvisor,
    logger,
)

DEFAULT_MODEL = "claude-opus-5"


class ClaudeAdvisor(ThreadedAdvisor):
    """Only the three provider hooks. Everything else is inherited."""

    name = "anthropic"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        budget: AdvisorBudget | None = None,
        effort: str = "low",
        max_tokens: int = 2048,
    ) -> None:
        super().__init__(model=model or DEFAULT_MODEL, budget=budget, max_tokens=max_tokens)
        self.effort = effort

    def _create_client(self):
        import anthropic

        return anthropic.Anthropic()

    def _is_auth_failure(self, error: Exception) -> bool:
        """Two distinct shapes. A *rejected* credential raises a typed SDK error. A
        *missing* one raises a plain TypeError from the client before any request is
        made — caught by message, because letting it through spends the entire session
        budget rediscovering the same permanent fact one doomed call at a time."""
        try:
            import anthropic
        except ImportError:
            return False
        if isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return True
        return isinstance(error, TypeError) and "could not resolve authentication" in str(error).lower()

    def _ask(self, brief: GoalBrief) -> GoalDecision | None:
        # Use the per-clan override when present; fall back to the advisor's own default.
        model = brief.model or self.model
        response = self._client.messages.create(
            model=model,
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
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
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
            message=str(payload.get("message", ""))[:120],
            source="llm",
            rules_goal=brief.rules_goal,
            prompt=brief.as_prompt(),
            raw_response=text,
        )
