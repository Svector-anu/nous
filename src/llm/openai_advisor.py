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
# Gateway in front of many providers. Auth is `Authorization: Bearer <key>` — not the
# x-api-key that anthropic's own endpoint wants — which is exactly why it fits this
# openai-shaped client rather than the native claude one.
DGRID_BASE_URL = "https://api.dgrid.ai/v1"

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
            message=str(payload.get("message", ""))[:120],
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


class DGridAdvisor(OpenAICompatibleAdvisor):
    """DGrid, a gateway that fronts many providers behind one openai-compatible endpoint.

    Worth its own class rather than reusing the plain "openai" path for one reason: that
    path only *requires* a key when the base url is openai's own, so a missing DGrid key
    would be sent as no key at all and come back as an opaque 401. Here it fails loudly.

    Models are named `provider/model` — `anthropic/claude-opus-4.7`, `openai/gpt-4o`. The
    default below is a starting point, not a promise; set `llm_model` explicitly.
    """

    name = "dgrid"
    DEFAULT_MODEL = "anthropic/claude-sonnet-4"

    # The gateway issues two kinds of credential and only one of them can infer.
    MANAGEMENT_KEY_PREFIX = "mk-"

    def __init__(
        self,
        model: str = "",
        base_url: str = DGRID_BASE_URL,
        api_key_env: str = "DGRID_API_KEY",
        budget: AdvisorBudget | None = None,
        max_tokens: int = 2048,
    ) -> None:
        super().__init__(
            model=model or self.DEFAULT_MODEL,
            base_url=base_url,
            api_key_env=api_key_env,
            budget=budget,
            max_tokens=max_tokens,
            require_api_key=True,
        )

    def _create_client(self):
        # A management key (mk-) administers other keys and cannot call inference, but it
        # *can* read /v1/models — so listing models succeeds and then chat/completions
        # returns a bare 401, which reads like a bad key rather than the wrong kind of key.
        # Catch it here, where the reason can actually be stated.
        api_key = os.environ.get(self.api_key_env, "")
        if api_key.startswith(self.MANAGEMENT_KEY_PREFIX):
            raise RuntimeError(
                f"{self.api_key_env} holds a management key ({self.MANAGEMENT_KEY_PREFIX}...), "
                "which cannot call inference. create a model key (sk-...) in the dgrid console"
            )
        # Every model on the gateway is addressed provider/model. A bare name is the config
        # default leaking through — llm_model defaults to a plain anthropic id, which is
        # right for the anthropic provider and wrong here — and would come back as a remote
        # 400 about an unknown model rather than pointing at the missing prefix.
        if "/" not in self.model:
            raise RuntimeError(
                f"dgrid model {self.model!r} has no provider prefix; "
                f"use e.g. 'anthropic/{self.model}' (see GET {self.base_url}/models)"
            )
        return super()._create_client()

    def _payload(self, brief: GoalBrief) -> dict[str, Any]:
        """Fold the system prompt into the user turn.

        The gateway drops system-role messages. Measured, not guessed: the same
        instruction sent as `system` came back "I don't have context for this", and sent in
        the user turn came back as exactly the requested JSON. So on this route the entire
        system prompt — the goal list, the rules, the output shape — never reached the
        model, and it answered plausibly from the state summary alone. That looked like a
        parsing bug and is not one.

        `response_format` is left in place; the gateway accepts and ignores it, and it
        costs nothing for the day it starts honouring it.
        """
        payload = super()._payload(brief)
        messages = payload["messages"]
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        rest = [m for m in messages if m["role"] != "system"]
        if system and rest:
            rest[0] = {**rest[0], "content": f"{system}\n\n{rest[0]['content']}"}
        payload["messages"] = rest or messages
        return payload
