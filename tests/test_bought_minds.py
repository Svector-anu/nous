"""Minds a visitor bought rather than ran themselves.

The visitor picks a model, pays for it, and the world calls it. The answer goes into
`MindQueue` through the same door a self-hosted mind posts through, so from the tick's
point of view the two are indistinguishable — same rate limit, same fixed drain, same
narrow set of things a mind may do.

That last part is the rule the feature lives on: **a bought mind picks differently, never
better.** The moment paying buys reach, speed or an exemption from hunger, the world is a
game about who spent money.
"""

from __future__ import annotations

import asyncio

import pytest

from src.llm import surplus
from src.world import bought_minds, spend as governor
from src.world.components import Agent, AttachedMind, MindQueue, SpendBook
from src.world.config import WorldConfig
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(
    seed=3,
    agent_count=6,
    resource_count=12,
    attached_minds_enabled=True,
    attached_mind_cooldown_ticks=5,
)

PRICES = {
    "models": [
        {
            "model": "cheap",
            "displayName": "Cheap",
            "providers": [{"providerName": "P", "pricing": {"input": 1.0, "output": 2.0}}],
        }
    ]
}


class FakeCatalog(surplus.Catalog):
    async def _fetch(self):
        return PRICES


class FakeBuyer(surplus.Buyer):
    """A buyer whose answers are scripted."""

    def __init__(self, *answers, key="k"):
        super().__init__(api_key=key)
        self.answers = list(answers)
        self.asked = []

    async def steer(self, model_id, perception, max_tokens=200):
        self.asked.append((model_id, perception))
        if not self.answers:
            return surplus.Steer(error="nothing scripted")
        answer = self.answers.pop(0)
        return answer


def _world(model="cheap", budget=1_000_000, enabled=True):
    world = create_world(CONFIG)
    Simulation(world).run(3)
    book = world.get(world.first(SpendBook), SpendBook)
    book.enabled = enabled
    governor.approve(book, budget, by="test", tick=world.tick)

    entity = next(iter(world.query(Agent)))
    world.add(entity, AttachedMind(owner="0xabc", model=model))
    governor.credit(book, entity, budget)
    return world, entity, book


def _steer(wants="food", say="", prompt=100, completion=10):
    return surplus.Steer(
        wants=wants, say=say, prompt_tokens=prompt, completion_tokens=completion
    )


def _run(world, buyer, catalog=None):
    return asyncio.run(bought_minds.run_once(world, catalog or FakeCatalog(), buyer))


# --- the answer arrives through the ordinary door ---------------------------------


def test_a_bought_steer_lands_in_the_queue_like_any_other():
    """Not applied directly. A steer is an input, and applying one the moment it arrives
    would make history depend on when a third party answered."""
    world, entity, _ = _world()
    _run(world, FakeBuyer(_steer(wants="wood", say="over here")))

    queued = world.get(world.first(MindQueue), MindQueue).pending
    assert [e["agent_id"] for e in queued] == [entity]
    assert queued[0]["wants"] == "wood"


def test_a_bought_mind_obeys_the_same_cooldown_as_a_self_hosted_one():
    """The rate limit is what stops a paid mind being a faster mind."""
    world, entity, _ = _world()
    buyer = FakeBuyer(_steer(), _steer(), _steer())

    _run(world, buyer)
    world.get(entity, AttachedMind).last_steer_tick = world.tick
    _run(world, buyer)

    assert len(buyer.asked) == 1, "it was asked again inside its cooldown"


def test_a_bought_mind_may_only_do_what_any_mind_may_do():
    """`minds.apply` is the one gate, reused verbatim. A model asking for something the
    state machine could not have chosen must change nothing."""
    world, entity, _ = _world()
    before = world.get(entity, Agent).wants
    _run(world, FakeBuyer(_steer(wants="gold")))

    Simulation(world).run(1)
    assert world.get(entity, Agent).wants is before


def test_an_agent_without_a_bought_mind_is_never_asked():
    world, entity, _ = _world(model="")
    buyer = FakeBuyer(_steer())
    _run(world, buyer)

    assert buyer.asked == []


# --- the money --------------------------------------------------------------------


def test_a_steer_is_charged_to_the_visitor_not_the_world():
    """Their model, their bill. The whole economic argument for this feature is that the
    number of thinking agents grows without the operator's bill growing with it.

    The balance is what proves it. Written first against `spent_units` alone, this passed
    with the charge booked to the world instead of the visitor — the world's envelope went
    down either way, and the one number that says *who paid* was never read.
    """
    world, entity, book = _world()
    before = book.balances[governor.key(entity)]
    _run(world, FakeBuyer(_steer(prompt=1000, completion=100)))

    # 1000 tokens at $1/M + 100 at $2/M = $0.0012 = 1200 micro-dollars.
    assert book.spent_units == 1200
    assert world.get(entity, AttachedMind).spent_units == 1200
    assert book.balances[governor.key(entity)] == before - 1200, "the visitor was not billed"


def test_a_visitor_out_of_credit_stops_thinking_even_with_a_full_envelope():
    """The envelope and the visitor's balance are different questions. An operator with
    money must not silently pay for a visitor's model — that is exactly the bill this
    feature exists to stop growing."""
    world, entity, book = _world()
    book.balances[governor.key(entity)] = 300
    buyer = FakeBuyer(_steer(prompt=1000, completion=100))
    _run(world, buyer)

    assert buyer.asked == [], "it called a model the visitor could not pay for"
    assert book.spent_units == 0


def test_the_charge_follows_real_usage_not_the_estimate():
    """A model that answers at length costs more than one that does not, and the visitor
    chose the model. Charging a flat rate across a 900x price spread is the thing this
    whole path exists to avoid."""
    world, _, book = _world()
    _run(world, FakeBuyer(_steer(prompt=100, completion=10)))
    small = book.spent_units

    world2, _, book2 = _world()
    _run(world2, FakeBuyer(_steer(prompt=4000, completion=400)))

    assert book2.spent_units > small * 10


def test_a_response_with_no_usage_still_costs_something():
    """Some providers return no usage block. Charging zero for a call that happened is how
    a paid feature quietly becomes a free one."""
    world, _, book = _world()
    _run(world, FakeBuyer(surplus.Steer(wants="food", prompt_tokens=0, completion_tokens=0)))

    assert book.spent_units > 0


def test_a_visitor_who_cannot_afford_it_is_not_asked():
    world, entity, book = _world()
    book.balances[governor.key(entity)] = 0
    buyer = FakeBuyer(_steer())
    _run(world, buyer)

    assert buyer.asked == []
    assert "not earned" in world.get(entity, AttachedMind).last_error


def test_a_failed_call_costs_nothing_and_holds_nothing():
    """The reservation must not survive a call that bought nothing — a leak here starves
    every other mind of budget that was never spent."""
    world, _, book = _world()
    _run(world, FakeBuyer(surplus.Steer(error="surplus said 500")))

    assert book.spent_units == 0
    assert book.reserved_units == 0


def test_a_halt_stops_bought_minds_too():
    world, entity, book = _world()
    governor.halt(book, "operator stopped it", tick=world.tick)
    buyer = FakeBuyer(_steer())
    _run(world, buyer)

    assert buyer.asked == []
    assert book.spent_units == 0


# --- settling an estimate against what it really cost ------------------------------


def test_settling_under_the_estimate_gives_the_rest_back():
    book = SpendBook(enabled=True, budget_units=10_000)
    governor.credit(book, 1, 10_000)
    governor.reserve(book, 1, 500, tick=1)
    governor.settle(book, 1, reserved=500, actual=200, tick=1)

    assert book.spent_units == 200
    assert book.reserved_units == 0


def test_settling_over_the_estimate_charges_what_it_cost():
    """A model that rambled past its estimate genuinely spent more, and refusing to record
    that is the more expensive lie."""
    book = SpendBook(enabled=True, budget_units=10_000)
    governor.credit(book, 1, 10_000)
    governor.reserve(book, 1, 500, tick=1)
    governor.settle(book, 1, reserved=500, actual=900, tick=1)

    assert book.spent_units == 900
    assert book.reserved_units == 0


def test_settling_never_releases_more_than_it_reserved():
    """The bug this function exists for. `commit` releases the reservation it commits, so
    a caller that also released its own estimate gives back more than it took — and
    reservations are one shared pool, so the surplus becomes somebody else's headroom.
    With several minds in flight that is money appearing from nowhere.
    """
    book = SpendBook(enabled=True, budget_units=100_000)
    governor.credit(book, 1, 100_000)
    governor.credit(book, 2, 100_000)

    governor.reserve(book, 1, 500, tick=1)
    governor.reserve(book, 2, 5000, tick=1)  # another mind, still in flight
    governor.settle(book, 1, reserved=500, actual=100, tick=1)

    assert book.reserved_units == 5000, "it ate the other mind's reservation"


@pytest.mark.parametrize("actual", [0, 1, 250, 500, 501, 5000])
def test_the_pool_is_never_left_wrong_whatever_it_cost(actual):
    book = SpendBook(enabled=True, budget_units=1_000_000)
    governor.credit(book, 1, 1_000_000)
    governor.credit(book, 2, 1_000_000)
    governor.reserve(book, 1, 500, tick=1)
    assert not governor.reserve(book, 2, 7000, tick=1), "the other mind never reserved"
    governor.settle(book, 1, reserved=500, actual=actual, tick=1)

    assert book.reserved_units == 7000
    assert book.spent_units == actual


# --- failures are explained, never silent -----------------------------------------


def test_a_model_that_left_the_price_list_says_so():
    """The catalog is a live third-party list. A model that was there when it was bought
    can be gone when it is used, and that must read as an explained pause."""
    world, entity, _ = _world(model="vanished")
    _run(world, FakeBuyer(_steer()))

    assert "price list" in world.get(entity, AttachedMind).last_error


def test_a_failure_is_recorded_where_the_owner_can_read_it():
    """A mind that silently stops is the failure this project has already been bitten by:
    an advisor died on a live world and nobody knew for two days."""
    world, entity, _ = _world()
    _run(world, FakeBuyer(surplus.Steer(error="surplus said 429")))

    assert world.get(entity, AttachedMind).last_error == "surplus said 429"


def test_a_working_call_clears_a_previous_failure():
    world, entity, _ = _world()
    _run(world, FakeBuyer(surplus.Steer(error="surplus said 429")))
    world.get(entity, AttachedMind).last_steer_tick = -1
    _run(world, FakeBuyer(_steer()))

    assert world.get(entity, AttachedMind).last_error == ""


def test_nothing_is_asked_when_there_is_no_key():
    world, _, _ = _world()
    buyer = FakeBuyer(_steer(), key="")
    _run(world, buyer)

    assert buyer.asked == []


def test_nothing_is_asked_when_attached_minds_are_switched_off():
    from dataclasses import replace

    world, _, _ = _world()
    world.config = replace(world.config, attached_minds_enabled=False)
    buyer = FakeBuyer(_steer())
    _run(world, buyer)

    assert buyer.asked == []


# --- one mind's failure never stops the rest ---------------------------------------


def test_a_mind_that_raises_does_not_stop_the_others():
    """Bounded concurrency means these run together. `steer_one` is written not to raise,
    but a world where one visitor's model can silence everyone else's is not one to bet
    on that."""

    class Exploding(FakeBuyer):
        async def steer(self, model_id, perception, max_tokens=200):
            self.asked.append((model_id, perception))
            if len(self.asked) == 1:
                raise RuntimeError("boom")
            return _steer()

    world, first, book = _world()
    others = [e for e in world.query(Agent) if e != first][:2]
    for entity in others:
        world.add(entity, AttachedMind(owner="0xdef", model="cheap"))
        governor.credit(book, entity, 100_000)

    bought = _run(world, Exploding())
    assert bought >= 1, "one exploding mind stopped every other"
