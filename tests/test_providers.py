"""Provider selection, and the contract every backend inherits.

The point of the abstraction is that the guarantees live in `ThreadedAdvisor`, not in any
one provider — so a new backend cannot forget them. These tests assert that by exercising
the shared machinery through a fake provider, and by checking each real provider only for
what is genuinely its own: how it builds a client, how it shapes a request, and how it
recognises a permanent credential failure.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.llm.advisor import (
    PROVIDERS,
    ClaudeAdvisor,
    DGridAdvisor,
    GrokAdvisor,
    NullAdvisor,
    OpenAICompatibleAdvisor,
    build_advisor,
)
from src.llm.base import (
    RESPONSE_SCHEMA,
    AdvisorBudget,
    GoalBrief,
    GoalDecision,
    ThreadedAdvisor,
)
from src.llm.openai_advisor import DGRID_BASE_URL, OPENAI_BASE_URL, XAI_BASE_URL
from src.world.config import WorldConfig

BRIEF = GoalBrief(1, 100, 5, 42.0, 0.6, 2.0, 3, "rally", 2, 0, rules_goal="gather_food")


# --- selection --------------------------------------------------------------


def test_disabled_beats_every_provider():
    for provider in PROVIDERS:
        advisor = build_advisor(WorldConfig(llm_enabled=False, llm_provider=provider))
        assert isinstance(advisor, NullAdvisor)


def test_provider_none_is_off():
    assert isinstance(build_advisor(WorldConfig(llm_enabled=True, llm_provider="none")), NullAdvisor)


def test_anthropic_is_the_default_provider():
    advisor = build_advisor(WorldConfig(llm_enabled=True))
    assert isinstance(advisor, ClaudeAdvisor)
    advisor.close()


@pytest.mark.parametrize(
    "provider,expected,base_url",
    [
        ("anthropic", ClaudeAdvisor, None),
        ("xai", GrokAdvisor, XAI_BASE_URL),
        ("openai", OpenAICompatibleAdvisor, OPENAI_BASE_URL),
        ("dgrid", DGridAdvisor, DGRID_BASE_URL),
    ],
)
def test_each_provider_is_selectable(provider, expected, base_url):
    advisor = build_advisor(WorldConfig(llm_enabled=True, llm_provider=provider, llm_model="m"))
    assert isinstance(advisor, expected)
    if base_url is not None:
        assert advisor.base_url == base_url
    advisor.close()


def test_provider_name_is_case_and_space_insensitive():
    advisor = build_advisor(WorldConfig(llm_enabled=True, llm_provider="  XAI  ", llm_model="m"))
    assert isinstance(advisor, GrokAdvisor)
    advisor.close()


def test_unknown_provider_falls_back_to_rules():
    advisor = build_advisor(WorldConfig(llm_enabled=True, llm_provider="hal9000"))
    assert isinstance(advisor, NullAdvisor), "an unknown provider must not become live"


def test_base_url_override_reaches_the_advisor():
    advisor = build_advisor(
        WorldConfig(
            llm_enabled=True,
            llm_provider="openai",
            llm_model="llama3",
            llm_base_url="http://localhost:11434/v1",
        )
    )
    assert advisor.base_url == "http://localhost:11434/v1"
    advisor.close()


def test_local_endpoints_do_not_require_a_key():
    """ollama, vllm and lm studio serve the same contract with no auth at all."""
    advisor = build_advisor(
        WorldConfig(
            llm_enabled=True,
            llm_provider="openai",
            llm_model="llama3",
            llm_base_url="http://localhost:11434/v1",
        )
    )
    assert advisor.require_api_key is False
    advisor.close()


def test_dgrid_always_requires_a_key():
    """A gateway needs its key. The plain "openai" path only requires one when the base url
    is openai's own, so routing dgrid through that would send no auth at all and surface as
    an opaque 401 instead of a clear local error."""
    advisor = build_advisor(WorldConfig(llm_enabled=True, llm_provider="dgrid", llm_model="m"))
    assert advisor.require_api_key is True
    assert advisor.api_key_env == "DGRID_API_KEY"
    advisor.close()


def test_dgrid_takes_provider_prefixed_model_names():
    """Models are addressed as provider/model on the gateway, so a slash must survive."""
    advisor = build_advisor(
        WorldConfig(
            llm_enabled=True, llm_provider="dgrid", llm_model="anthropic/claude-opus-4.7"
        )
    )
    assert advisor.model == "anthropic/claude-opus-4.7"
    advisor.close()


def test_hosted_openai_does_require_a_key():
    advisor = build_advisor(WorldConfig(llm_enabled=True, llm_provider="openai", llm_model="gpt-4"))
    assert advisor.require_api_key is True
    advisor.close()


def test_api_key_env_is_configurable():
    advisor = build_advisor(
        WorldConfig(
            llm_enabled=True, llm_provider="xai", llm_model="m", llm_api_key_env="MY_KEY"
        )
    )
    assert advisor.api_key_env == "MY_KEY"
    advisor.close()


# --- the contract every backend inherits ------------------------------------


class _FakeProvider(ThreadedAdvisor):
    """A backend that implements only the three hooks, to prove the rest is inherited."""

    name = "fake"

    def __init__(self, answer="expand", explode=None, **kwargs):
        super().__init__(model="fake-1", **kwargs)
        self.answer = answer
        self.explode = explode
        self.asked = 0

    def _create_client(self):
        return object()

    def _is_auth_failure(self, error: Exception) -> bool:
        return isinstance(error, PermissionError)

    def _ask(self, brief: GoalBrief) -> GoalDecision | None:
        self.asked += 1
        if self.explode is not None:
            raise self.explode
        return GoalDecision(
            brief.clan_id, self.answer, "because", "llm",
            rules_goal=brief.rules_goal, prompt=brief.as_prompt(), raw_response="{}",
        )


def _drain(advisor, tries: int = 200):
    import time

    for _ in range(tries):
        decisions = advisor.collect()
        if decisions or advisor.pending() == 0:
            return decisions
        time.sleep(0.01)
    return []


def test_a_minimal_provider_gets_the_whole_contract():
    advisor = _FakeProvider()
    assert advisor.submit(BRIEF) is True
    decisions = _drain(advisor)

    assert len(decisions) == 1
    assert decisions[0].goal == "expand"
    assert decisions[0].rules_goal == "gather_food"
    assert decisions[0].verdict == "differs"
    advisor.close()


def test_inherited_budget_cap():
    advisor = _FakeProvider(budget=AdvisorBudget(max_inflight=8, max_calls_per_session=2))
    accepted = sum(1 for _ in range(10) if advisor.submit(BRIEF))
    assert accepted == 2
    advisor.close()


def test_inherited_auth_disable():
    advisor = _FakeProvider(explode=PermissionError("bad key"))
    advisor.submit(BRIEF)
    _drain(advisor)

    assert advisor._unavailable_reason is not None
    assert advisor.submit(BRIEF) is False
    advisor.close()


def test_inherited_transient_failure_does_not_disable():
    advisor = _FakeProvider(explode=RuntimeError("network blip"))
    advisor.submit(BRIEF)
    _drain(advisor)

    assert advisor._unavailable_reason is None
    advisor.close()


def test_inherited_invalid_goal_is_discarded():
    advisor = _FakeProvider(answer="conquer_the_moon")
    advisor.submit(BRIEF)
    assert _drain(advisor) == []
    advisor.close()


def test_inherited_client_failure_disables():
    class Broken(_FakeProvider):
        def _create_client(self):
            raise RuntimeError("no client")

    advisor = Broken()
    assert advisor.submit(BRIEF) is False
    assert advisor._unavailable_reason is not None


def test_inflight_clans_is_tracked():
    advisor = _FakeProvider(budget=AdvisorBudget(max_inflight=1, max_calls_per_session=5))
    advisor.submit(BRIEF)
    assert advisor.inflight_clans() <= {1}
    _drain(advisor)
    assert advisor.inflight_clans() == set()
    advisor.close()


# --- openai-compatible request and response shape ---------------------------


def _stub(handler) -> OpenAICompatibleAdvisor:
    advisor = OpenAICompatibleAdvisor(model="grok-test", require_api_key=False)
    advisor._client = httpx.Client(
        base_url="http://stub", transport=httpx.MockTransport(handler)
    )
    return advisor


def test_openai_request_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"goal":"rally","reason":"ok"}'}}]},
        )

    advisor = _stub(handler)
    decision = advisor._ask(BRIEF)

    assert seen["model"] == "grok-test"
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
    assert seen["response_format"]["json_schema"]["schema"] == RESPONSE_SCHEMA
    assert decision.goal == "rally"
    assert decision.rules_goal == "gather_food"


def test_openai_parses_a_fenced_or_embedded_object():
    """Endpoints vary in how faithfully they honour response_format."""
    for body in (
        '```json\n{"goal":"expand","reason":"x"}\n```',
        'Sure! {"goal":"expand","reason":"x"} hope that helps',
    ):
        advisor = _stub(
            lambda r, b=body: httpx.Response(200, json={"choices": [{"message": {"content": b}}]})
        )
        assert advisor._ask(BRIEF).goal == "expand"


def test_openai_discards_an_unparseable_answer():
    advisor = _stub(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "no idea"}}]})
    )
    assert advisor._ask(BRIEF) is None


def test_openai_handles_an_empty_choices_list():
    advisor = _stub(lambda r: httpx.Response(200, json={"choices": []}))
    assert advisor._ask(BRIEF) is None


@pytest.mark.parametrize("status", [401, 403])
def test_openai_auth_errors_are_permanent(status):
    advisor = _stub(lambda r: httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        advisor._ask(BRIEF)
    assert advisor._is_auth_failure(caught.value) is True


@pytest.mark.parametrize("status", [429, 500, 503])
def test_openai_transient_errors_are_not_permanent(status):
    advisor = _stub(lambda r: httpx.Response(status, json={"error": "later"}))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        advisor._ask(BRIEF)
    assert advisor._is_auth_failure(caught.value) is False


def test_missing_key_disables_before_reaching_the_wire(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    advisor = GrokAdvisor(model="grok-test")
    assert advisor.submit(BRIEF) is False
    assert advisor._unavailable_reason is not None


def test_grok_defaults_to_the_xai_host():
    advisor = GrokAdvisor(model="grok-test")
    assert advisor.base_url == XAI_BASE_URL
    assert advisor.api_key_env == "XAI_API_KEY"
    assert advisor.name == "xai"
