"""The price list a visitor picks a mind from.

The world's own advisor charges a flat estimate per call, which is defensible for one
fixed model at fractions of a cent. It is not defensible here: Surplus publishes 374
models spanning $0.04 to $37.50 per million input — a 900x spread — and the visitor picks
which one. A flat rate would overcharge the cheapest by 90x.

So these are mostly about arithmetic being right and a third party's response never
taking a page down with it.
"""

from __future__ import annotations

import asyncio

import pytest

from src.llm import surplus


def _payload(*models: dict) -> dict:
    return {"models": list(models)}


def _model(model_id: str, *prices: tuple[float, float], name: str = "") -> dict:
    return {
        "model": model_id,
        "displayName": name or model_id,
        "providers": [
            {
                "provider": f"p{i}",
                "providerName": f"Provider {i}",
                "pricing": {"input": inp, "output": out},
            }
            for i, (inp, out) in enumerate(prices)
        ],
    }


# --- what a steer costs -----------------------------------------------------------


def test_a_steer_is_priced_from_both_halves_of_the_call():
    """Input and output are priced differently and output is dearer. Charging on one of
    them is the kind of wrong that only shows up on the bill."""
    models = surplus.parse(_payload(_model("m", (5.0, 25.0))))
    expected = (
        surplus.PROMPT_TOKENS * 5.0 + surplus.COMPLETION_TOKENS * 25.0
    ) / surplus.PER_MILLION * 1_000_000

    assert models[0].units_per_steer == pytest.approx(expected, abs=1)


def test_a_fraction_of_a_unit_rounds_up():
    """The cheapest models cost a fraction of one micro-dollar per steer. Rounding down
    makes them free, and free is how a paid feature turns into an unpaid one."""
    models = surplus.parse(_payload(_model("tiny", (0.0001, 0.0001))))

    assert models[0].units_per_steer == 1


def test_the_spread_between_models_survives_pricing():
    """The whole reason this exists: a flat per-call cost is meaningless across a 900x
    spread. If both ends came out the same, the pricing is not doing anything."""
    models = {m.model_id: m for m in surplus.parse(
        _payload(_model("cheap", (0.04, 0.08)), _model("dear", (37.5, 225.0)))
    )}

    assert models["dear"].units_per_steer > models["cheap"].units_per_steer * 100


def test_a_thousand_steers_is_what_gets_quoted():
    """One steer is $0.000016 on a cheap model, which tells a visitor nothing."""
    models = surplus.parse(_payload(_model("m", (5.0, 25.0))))
    quoted = models[0].as_dict()

    assert quoted["estimated_units_per_1000_steers"] == quoted["estimated_units_per_steer"] * 1000


def test_every_price_is_named_an_estimate():
    """It is a floor, not a promise — real cost is token usage nobody can count up front,
    and Surplus routes to whichever provider is cheapest at the time. A field called
    `units_per_steer` invites somebody to bill from it."""
    quoted = surplus.parse(_payload(_model("m", (5.0, 25.0))))[0].as_dict()

    priced = [k for k in quoted if "units" in k]
    assert priced, "no cost fields at all"
    assert all(k.startswith("estimated_") for k in priced), priced


# --- picking the provider a call would really use ---------------------------------


def test_the_cheapest_provider_is_the_one_quoted():
    """Surplus lists several per model and routes to what is cheapest and available.
    Quoting the first in the list would misprice the thing being bought."""
    models = surplus.parse(_payload(_model("m", (30.0, 100.0), (5.0, 25.0), (12.0, 60.0))))

    assert models[0].input_price == 5.0
    assert models[0].output_price == 25.0


def test_a_provider_with_no_price_is_not_free():
    """A missing price read as zero would put an unpriced model at the top of a list
    sorted by cost, which is the single most expensive way to be wrong here."""
    entry = _model("m", (5.0, 25.0))
    entry["providers"].insert(0, {"provider": "x", "providerName": "X", "pricing": {}})
    models = surplus.parse(_payload(entry))

    assert models[0].input_price == 5.0


# --- a third party's response never breaks a page ---------------------------------


def test_models_with_no_priced_provider_are_dropped():
    """197 of 374 published models currently carry no priced provider, so the unusable
    majority is the normal case here rather than the edge case."""
    payload = _payload(_model("good", (1.0, 2.0)), {"model": "bare", "providers": []})

    assert [m.model_id for m in surplus.parse(payload)] == ["good"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"models": None},
        {"models": []},
        {"models": ["not a dict"]},
        {"models": [{"model": ""}]},
        {"models": [{"model": "m", "providers": None}]},
        {"models": [{"model": "m", "providers": [{"pricing": "cheap"}]}]},
        {"models": [{"model": "m", "providers": [{"pricing": {"input": None, "output": 1}}]}]},
    ],
)
def test_a_shape_we_do_not_recognise_yields_nothing_rather_than_raising(payload):
    assert surplus.parse(payload) == []


def test_the_list_is_ordered_by_what_it_costs():
    models = surplus.parse(
        _payload(_model("dear", (30.0, 60.0)), _model("cheap", (1.0, 2.0)), _model("mid", (5.0, 10.0)))
    )

    assert [m.model_id for m in models] == ["cheap", "mid", "dear"]


# --- caching, and an outage costing freshness rather than the list -----------------


class _Catalog(surplus.Catalog):
    """A catalog whose fetches are scripted instead of networked."""

    def __init__(self, *answers, **kwargs):
        super().__init__(**kwargs)
        self.answers = list(answers)
        self.calls = 0

    async def _fetch(self):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_a_warm_list_is_not_refetched():
    catalog = _Catalog(_payload(_model("m", (1.0, 2.0))), ttl=600)
    asyncio.run(catalog.refresh(now=0))
    asyncio.run(catalog.refresh(now=10))

    assert catalog.calls == 1


def test_a_cold_list_is_refetched():
    catalog = _Catalog(
        _payload(_model("m", (1.0, 2.0))), _payload(_model("m2", (1.0, 2.0))), ttl=60
    )
    asyncio.run(catalog.refresh(now=0))
    asyncio.run(catalog.refresh(now=100))

    assert catalog.calls == 2
    assert [m.model_id for m in catalog.models] == ["m2"]


def test_an_outage_keeps_the_last_good_list():
    """The catalog is a price list. A visitor browsing it does not care that it was taken
    nine minutes ago, and emptying it because a third party had a bad minute turns their
    outage into ours."""
    catalog = _Catalog(_payload(_model("m", (1.0, 2.0))), httpx_error := RuntimeError("boom"), ttl=1)
    asyncio.run(catalog.refresh(now=0))
    asyncio.run(catalog.refresh(now=100))

    assert [m.model_id for m in catalog.models] == ["m"]
    assert catalog.status(now=100)["error"] == str(httpx_error)


def test_an_empty_answer_does_not_wipe_a_good_list():
    """A well-formed response with nothing priced in it is still a failure to price
    anything, and letting it overwrite a working list is a downgrade dressed as success."""
    catalog = _Catalog(_payload(_model("m", (1.0, 2.0))), _payload(), ttl=1)
    asyncio.run(catalog.refresh(now=0))
    asyncio.run(catalog.refresh(now=100))

    assert [m.model_id for m in catalog.models] == ["m"]


def test_status_reports_age_and_staleness():
    catalog = _Catalog(_payload(_model("m", (1.0, 2.0))), ttl=60)
    assert catalog.status(now=0)["age_seconds"] is None

    asyncio.run(catalog.refresh(now=0))
    assert catalog.status(now=30) == {"models": 1, "age_seconds": 30, "stale": False, "error": ""}
    assert catalog.status(now=90)["stale"] is True
