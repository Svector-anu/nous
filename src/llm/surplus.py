"""The price list for minds a visitor can buy.

Surplus sells inference from many models at market prices and publishes what each one
costs. This fetches that list, keeps it for a while, and turns it into the only number a
visitor actually needs: what one steer of their agent costs.

**Never call this from a system.** Same rule as `chain/rpc.py` — systems run inside the
tick loop, and a network round trip there would stall the world and make history depend
on the network. Api routes and background jobs only.

Two things shape the whole module.

**A stale price is better than no price.** The catalog is decoration until money moves,
and a visitor looking at a model list does not care whether it was fetched nine minutes
ago. So a failed refresh keeps serving the last good answer and says when it was taken,
rather than emptying the list because a third party had a bad minute.

**A price is a floor, never a promise.** What a call actually costs depends on tokens
nobody can count in advance, and Surplus routes to whichever provider is cheapest at the
time. Everything here is named as an estimate so no caller is tempted to bill from it.
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger("neociv")

BASE_URL = "https://api.surplusintelligence.ai"
PRICES_PATH = "/v1/prices"

DEFAULT_TIMEOUT = 8.0
# Long enough that a visitor browsing the list is not refetching it, short enough that a
# model whose price moved is not misquoted for an hour.
DEFAULT_TTL_SECONDS = 600.0

# The shape of a steer, measured against what `minds.perception()` actually sends plus the
# instruction wrapped around it — not a guess at a typical prompt. A steer is small by
# design: the whole answer is a want and at most 160 characters of speech.
PROMPT_TOKENS = 350
COMPLETION_TOKENS = 40

# Prices are quoted per million tokens.
PER_MILLION = 1_000_000

# What a visitor is actually deciding between. Quoting a single steer gives everyone
# $0.000016 and tells them nothing.
STEERS_QUOTED = 1000


class Model:
    """One buyable model at its cheapest live provider."""

    __slots__ = ("model_id", "name", "provider", "input_price", "output_price")

    def __init__(
        self,
        model_id: str,
        name: str,
        provider: str,
        input_price: float,
        output_price: float,
    ) -> None:
        self.model_id = model_id
        self.name = name
        self.provider = provider
        self.input_price = input_price
        self.output_price = output_price

    @property
    def units_per_steer(self) -> int:
        """Cost of one steer in the token's smallest unit — micro-dollars, since both
        assets this world accepts carry six decimals.

        Rounded up. A rounding error that favours the world is a rounding error nobody has
        to refund.
        """
        dollars = (
            PROMPT_TOKENS * self.input_price + COMPLETION_TOKENS * self.output_price
        ) / PER_MILLION
        micro = dollars * 1_000_000
        whole = int(micro)
        return whole + 1 if micro > whole else whole

    def as_dict(self) -> dict:
        return {
            "model": self.model_id,
            "name": self.name,
            "provider": self.provider,
            "input_per_million": self.input_price,
            "output_per_million": self.output_price,
            # Named "estimated" everywhere it is exposed. The real cost is token usage
            # read back from the call, which this cannot know.
            "estimated_units_per_steer": self.units_per_steer,
            "estimated_units_per_1000_steers": self.units_per_steer * STEERS_QUOTED,
        }


def _cheapest(providers: list[dict]) -> dict | None:
    """The provider a call would actually be routed to.

    Surplus lists several per model and routes to what is available and cheapest, so
    quoting anything else would misprice the thing a visitor is buying. Entries missing a
    price are dropped rather than treated as free.
    """
    priced = []
    for provider in providers or []:
        pricing = provider.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        if not isinstance(pricing.get("input"), (int, float)):
            continue
        if not isinstance(pricing.get("output"), (int, float)):
            continue
        priced.append(provider)
    if not priced:
        return None
    return min(priced, key=lambda p: (p["pricing"]["input"], p["pricing"]["output"]))


def parse(payload: dict) -> list[Model]:
    """Turn what Surplus published into what a visitor can pick from.

    Written to survive a shape it does not recognise. This is a third party's response
    parsed on a live server: a model with no providers, a null price, an entry that is not
    a dict at all. Of 374 models published, 197 currently carry no priced provider — so
    the unusable majority is the normal case here, not the edge case.
    """
    models = []
    for entry in (payload or {}).get("models") or []:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("model") or "").strip()
        if not model_id:
            continue
        best = _cheapest(entry.get("providers"))
        if best is None:
            continue
        pricing = best["pricing"]
        models.append(
            Model(
                model_id=model_id,
                name=str(entry.get("displayName") or model_id),
                provider=str(best.get("providerName") or best.get("provider") or "unknown"),
                input_price=float(pricing["input"]),
                output_price=float(pricing["output"]),
            )
        )
    models.sort(key=lambda m: (m.units_per_steer, m.model_id))
    return models


class Catalog:
    """The price list, cached.

    Holds the last good answer and its age. A refresh that fails leaves both alone, so a
    Surplus outage costs the freshness of the list rather than the list itself.
    """

    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        ttl: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.ttl = ttl
        self._models: list[Model] = []
        self._fetched_at: float = 0.0
        self._error: str = ""

    @property
    def models(self) -> list[Model]:
        return list(self._models)

    def _stale(self, now: float) -> bool:
        return not self._models or (now - self._fetched_at) >= self.ttl

    async def _fetch(self) -> dict:
        """The network, and nothing else.

        Split out so the caching and failure behaviour above can be tested against the
        real `refresh` rather than a copy of it. A test that reimplements the logic it is
        checking passes whatever the code does.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}{PRICES_PATH}")
            response.raise_for_status()
            return response.json()

    async def refresh(self, now: float | None = None) -> list[Model]:
        """Fetch unless the last answer is still warm.

        Never raises. A caller of this is serving a page, and a third party being down is
        not a reason to fail a request that has a perfectly good older answer to give.
        """
        moment = time.monotonic() if now is None else now
        if not self._stale(moment):
            return self.models

        try:
            payload = await self._fetch()
        except Exception as error:  # noqa: BLE001 - a price list must never break a page
            self._error = str(error)
            logger.warning("surplus price list unavailable (%s); serving what we have", error)
            return self.models

        parsed = parse(payload)
        if not parsed:
            # A well-formed answer with nothing usable in it is still a failure to price
            # anything, and overwriting a good list with it would be a downgrade.
            self._error = "surplus returned no priced models"
            logger.warning("%s; serving what we have", self._error)
            return self.models

        self._models = parsed
        self._fetched_at = moment
        self._error = ""
        return self.models

    def status(self, now: float | None = None) -> dict:
        moment = time.monotonic() if now is None else now
        return {
            "models": len(self._models),
            "age_seconds": int(moment - self._fetched_at) if self._models else None,
            "stale": self._stale(moment),
            "error": self._error,
        }


# --- buying one steer -------------------------------------------------------------
#
# The world calls Surplus and hands the answer to `minds.enqueue_steer`, so a bought mind
# and a mind a visitor runs themselves arrive down the same path and are subject to the
# same rate limit, the same tick drain and the same narrow set of things a mind may do.
# Nothing downstream can tell them apart, which is the point: a bought mind must not be a
# better mind.

API_KEY_ENV = "SURPLUS_API_KEY"
CHAT_PATH = "/v1/chat/completions"

SYSTEM_PROMPT = (
    "You are the mind of one agent in a persistent world. You will be given what your "
    "agent can see. Reply with JSON only: {\"wants\":\"food\"|\"wood\",\"say\":\"...\"}. "
    "`wants` is what your agent should seek next. `say` is at most 160 characters spoken "
    "aloud to nearby agents, or \"\" to stay quiet. No other keys, no explanation."
)


class Steer:
    """What one call bought."""

    __slots__ = ("wants", "say", "prompt_tokens", "completion_tokens", "error")

    def __init__(
        self,
        wants: str = "",
        say: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: str = "",
    ) -> None:
        self.wants = wants
        self.say = say
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.error = error

    def __bool__(self) -> bool:
        """A steer is worth enqueuing only if it asks for something. Written explicitly
        because a `Steer` carrying only an error is still an object, and `if steer:` on a
        plain object is always true — the exact shape of a bug that reopened a payment
        replay hole elsewhere in this codebase."""
        return not self.error and bool(self.wants or self.say)

    def units(self, model: Model) -> int:
        """What this call actually cost, from the usage the response reported.

        Rounded up, and falls back to the model's estimate when a provider returns no
        usage block — charging zero for a call that happened is how a paid feature
        quietly becomes a free one.
        """
        if not (self.prompt_tokens or self.completion_tokens):
            return model.units_per_steer
        dollars = (
            self.prompt_tokens * model.input_price
            + self.completion_tokens * model.output_price
        ) / PER_MILLION
        micro = dollars * 1_000_000
        whole = int(micro)
        return whole + 1 if micro > whole else whole


def parse_steer(payload: dict) -> Steer:
    """Read a steer out of an openai-shaped response.

    Every field is optional and anything unrecognised is dropped rather than guessed at.
    `minds.apply` discards unknown wants anyway, so this is the second of two gates, not
    the only one.
    """
    import json

    usage = (payload or {}).get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    counted = {
        "prompt_tokens": int(prompt_tokens) if isinstance(prompt_tokens, int) else 0,
        "completion_tokens": int(completion_tokens) if isinstance(completion_tokens, int) else 0,
    }

    choices = (payload or {}).get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return Steer(error="no answer in the response", **counted)
    content = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not content:
        return Steer(error="empty answer", **counted)

    # Models wrap json in prose and fences however they like. Take the outermost object
    # rather than demanding the whole reply be json.
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return Steer(error="answer was not json", **counted)
    try:
        answer = json.loads(content[start : end + 1])
    except ValueError:
        return Steer(error="answer was not valid json", **counted)
    if not isinstance(answer, dict):
        return Steer(error="answer was not an object", **counted)

    return Steer(
        wants=str(answer.get("wants") or "").strip().lower(),
        say=str(answer.get("say") or "").strip(),
        **counted,
    )


class Buyer:
    """Buys one steer at a time from Surplus.

    Holds no world state and knows nothing about payment. The caller decides whether the
    visitor can afford this and records what it cost, because the money lives in the spend
    governor and this is only the thing that makes the call.
    """

    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        api_key: str = "",
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    async def steer(self, model_id: str, perception: dict, max_tokens: int = 200) -> Steer:
        """Ask one model what this agent should do next. Never raises.

        A mind that fails is a mind that said nothing, and a mind that says nothing is
        already a case the world handles — the state machine takes over with nothing to
        detect. So every failure here returns a `Steer` carrying a reason, which the owner
        can see, rather than an exception the tick loop would have to survive.
        """
        import json

        if not self.ready:
            return Steer(error="no surplus key configured")

        body = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(perception, sort_keys=True)},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}{CHAT_PATH}",
                    json=body,
                    headers={"authorization": f"Bearer {self.api_key}"},
                )
                if response.status_code >= 400:
                    # The body carries why, and the owner is the one who needs to know —
                    # a model they chose and paid for going quiet with no reason is the
                    # failure this world has already been bitten by.
                    return Steer(error=f"surplus said {response.status_code}")
                payload = response.json()
        except Exception as error:  # noqa: BLE001 - a bought mind must never break a tick
            logger.warning("surplus steer failed for %s (%s)", model_id, error)
            return Steer(error=str(error)[:120])

        return parse_steer(payload)
