"""OpenAI-compatible backends: xAI Grok, OpenAI, OpenRouter, Ollama, vLLM, LM Studio.

Spoken over raw HTTP with `httpx` rather than through the `openai` SDK, deliberately.
The point of this backend is to work against *any* endpoint that implements
`POST /chat/completions`, including local servers whose compatibility is approximate.
A raw request has no opinion about which of those it is talking to, and httpx is already
a dependency.

Structured output is requested via `response_format`, but never relied upon: endpoints
differ in whether they honour it, so the response is also parsed defensively. An answer
that cannot be parsed into a known goal is discarded, and the rules stand — the same
outcome as any other failure.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .base import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    AdvisorBudget,
    GoalBrief,
    GoalDecision,
    ThreadedAdvisor,
    logger,
)

XAI_BASE_URL = "https://api.x.ai/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


class OpenAICompatibleAdvisor(ThreadedAdvisor):
    """Any endpoint exposing the OpenAI chat-completions contract."""

    name = "openai"

    def __init__(
        self,
        model: str,
        base_url: str = OPENAI_BASE_URL,
        api_key_env: str = "OPENAI_API_KEY",
        budget: AdvisorBudget | None = None,
        max_tokens: int = 2048,
        require_api_key: bool = True,
    ) -> None:
        super().__init__(model=model, budget=budget, max_tokens=max_tokens)
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        # Local servers (ollama, vllm, lm studio) usually need no key at all.
        self.require_api_key = require_api_key

    def _create_client(self):
        import httpx

        api_key = os.environ.get(self.api_key_env, "")
        if self.require_api_key and not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set")

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=self.budget.timeout_seconds,
        )

    def _is_auth_failure(self, error: Exception) -> bool:
        try:
            import httpx
        except ImportError:
            return False
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code in (401, 403)
        # A missing key never reaches the wire — it fails at client construction, which
        # ThreadedAdvisor already treats as permanently disabling.
        return isinstance(error, RuntimeError) and "is not set" in str(error)

    def _payload(self, brief: GoalBrief) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": brief.as_prompt()},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "clan_goal",
                    "strict": True,
                    "schema": RESPONSE_SCHEMA,
                },
            },
        }

    @staticmethod
    def _parse(text: str) -> dict[str, Any] | None:
        """Endpoints vary in how faithfully they honour response_format, so accept a bare
        object, a fenced block, or an object embedded in prose — and nothing else."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        match = _JSON_OBJECT.search(text)
        if match is None:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    def _ask(self, brief: GoalBrief) -> GoalDecision | None:
        response = self._client.post("/chat/completions", json=self._payload(brief))
        response.raise_for_status()
        body = response.json()

        choices = body.get("choices") or []
        if not choices:
            logger.warning("clan %d: %s returned no choices", brief.clan_id, self.name)
            return None
        text = (choices[0].get("message") or {}).get("content") or ""
        if not text:
            return None

        payload = self._parse(text)
        if payload is None or "goal" not in payload:
            logger.warning("clan %d: %s returned unparseable answer", brief.clan_id, self.name)
            return None

        return GoalDecision(
            clan_id=brief.clan_id,
            goal=str(payload["goal"]),
            reason=str(payload.get("reason", "")),
            source="llm",
            rules_goal=brief.rules_goal,
            prompt=brief.as_prompt(),
            raw_response=text,
        )


class GrokAdvisor(OpenAICompatibleAdvisor):
    """xAI, which exposes the same contract at a different host.

    `DEFAULT_MODEL` is a starting point, not a promise — model names move, so set
    `llm_model` explicitly rather than trusting this.
    """

    name = "xai"
    DEFAULT_MODEL = "grok-4"

    def __init__(
        self,
        model: str = "",
        base_url: str = XAI_BASE_URL,
        api_key_env: str = "XAI_API_KEY",
        budget: AdvisorBudget | None = None,
        max_tokens: int = 2048,
    ) -> None:
        super().__init__(
            model=model or self.DEFAULT_MODEL,
            base_url=base_url,
            api_key_env=api_key_env,
            budget=budget,
            max_tokens=max_tokens,
        )
