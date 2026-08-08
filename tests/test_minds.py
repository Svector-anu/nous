"""Minds a visitor attached to their own agent.

No network anywhere here. The client is injected, so these drive a stub and assert on
what the world does with the answers — including all the answers a hostile or broken
endpoint might send.
"""

from __future__ import annotations

from dataclasses import replace

from src.world.components import AttachedMind, Agent, Outbox, ResourceKind
from src.world.config import WorldConfig
from src.world.systems import minds
from src.world.tick import Simulation, create_world

CONFIG = WorldConfig(
    seed=7,
    agent_count=6,
    resource_count=12,
    attached_minds_enabled=True,
    attached_mind_cooldown_ticks=5,
)


class StubMind:
    """Answers whatever it was told to, and records what it was asked."""

    def __init__(self, answer: dict | None = None) -> None:
        self.answer = answer
        self.asked: list[dict] = []
        self._queued: list[dict] = []

    def submit(self, request: dict) -> bool:
        self.asked.append(request)
        if self.answer is not None:
            reply = dict(self.answer)
            reply["agent_id"] = request["perception"]["agent_id"]
            self._queued.append(reply)
        return True

    def collect(self) -> list[dict]:
        out, self._queued = self._queued, []
        return out

    def pending(self) -> int:
        return len(self._queued)


def _world_with_mind(config=CONFIG, **mind_kwargs):
    world = create_world(config)
    entity = next(iter(world.query(Agent)))
    world.add(entity, AttachedMind(endpoint="https://example.test/mind", **mind_kwargs))
    return world, entity


# --- the seam -----------------------------------------------------------------------


def test_a_mind_sets_what_its_agent_wants():
    world, entity = _world_with_mind()
    world.minds = StubMind({"wants": "wood"})
    world.get(entity, Agent).wants = ResourceKind.FOOD

    simulation = Simulation(world)
    simulation.run(12)

    assert world.get(entity, Agent).wants is ResourceKind.WOOD


def test_a_mind_gains_no_power_the_agent_did_not_have():
    """It picks differently, never better. Both values were already reachable by the
    state machine — without this a paid mind is a bought advantage and the world becomes
    a game about who spent money."""
    assert set(minds.WANTS) == {"food", "wood"}
    assert set(minds.WANTS.values()) == {ResourceKind.FOOD, ResourceKind.WOOD}


def test_nothing_is_asked_while_the_feature_is_off():
    """An endpoint on somebody else's machine must not be reached until an operator
    says so."""
    world, _ = _world_with_mind(config=replace(CONFIG, attached_minds_enabled=False))
    client = StubMind({"wants": "wood"})
    world.minds = client

    Simulation(world).run(20)
    assert client.asked == []


def test_a_world_with_no_client_attached_just_runs():
    world, _ = _world_with_mind()
    Simulation(world).run(10)  # world.minds never set


# --- what a broken or hostile endpoint can do -----------------------------------------


def test_an_unknown_want_changes_nothing():
    """A mind that returns nonsense should change nothing, not something arbitrary."""
    world, entity = _world_with_mind()
    world.get(entity, Agent).wants = ResourceKind.FOOD
    mind = world.get(entity, AttachedMind)

    assert minds.apply(world, entity, mind, {"wants": "gold"}) is False
    assert world.get(entity, Agent).wants is ResourceKind.FOOD


def test_an_empty_answer_changes_nothing():
    world, entity = _world_with_mind()
    mind = world.get(entity, AttachedMind)
    assert minds.apply(world, entity, mind, {}) is False


def test_speech_is_truncated():
    """It is rendered in a viewer and stored in world state, and an endpoint is free to
    return a megabyte."""
    world, entity = _world_with_mind()
    mind = world.get(entity, AttachedMind)
    minds.apply(world, entity, mind, {"say": "x" * 5000})

    assert len(mind.last_said) == minds.MAX_SAY


def test_speech_goes_through_the_ordinary_message_path():
    """A mind that could talk faster than the world does would be a different kind of
    advantage."""
    world, entity = _world_with_mind()
    mind = world.get(entity, AttachedMind)
    minds.apply(world, entity, mind, {"say": "meet me at the river"})

    messages = world.get(entity, Outbox).messages
    assert messages and messages[-1]["text"] == "meet me at the river"


def test_a_failing_endpoint_is_eventually_left_alone():
    """One dead url must not buy a request every cooldown for the rest of the world's
    life."""
    world, entity = _world_with_mind()
    world.minds = StubMind({"failed": True})

    Simulation(world).run(minds.MAX_FAILURES * CONFIG.attached_mind_cooldown_ticks * 3)
    mind = world.get(entity, AttachedMind)
    assert mind.failures >= minds.MAX_FAILURES

    before = len(world.minds.asked)
    Simulation(world).run(60)
    assert len(world.minds.asked) == before, "a failed mind was still being asked"


def test_a_good_answer_clears_earlier_failures():
    world, entity = _world_with_mind(failures=3)
    world.minds = StubMind({"wants": "food"})

    Simulation(world).run(12)
    assert world.get(entity, AttachedMind).failures == 0


def test_a_client_that_raises_costs_a_failure_not_the_tick():
    class Exploding(StubMind):
        def submit(self, request):
            raise RuntimeError("endpoint on fire")

    world, entity = _world_with_mind()
    world.minds = Exploding()

    Simulation(world).run(12)  # must not raise
    assert world.get(entity, AttachedMind).failures > 0


# --- cadence ------------------------------------------------------------------------


def test_a_mind_is_asked_on_its_own_cooldown():
    """Held per agent rather than globally, so one busy mind cannot crowd out another."""
    world, _ = _world_with_mind()
    client = StubMind({"wants": "food"})
    world.minds = client

    Simulation(world).run(20)
    # 20 ticks at a cooldown of 5 is four windows; never one per tick.
    assert 1 <= len(client.asked) <= 5, f"asked {len(client.asked)} times in 20 ticks"


def test_an_agent_without_a_mind_is_never_asked():
    world, entity = _world_with_mind()
    client = StubMind({"wants": "food"})
    world.minds = client

    Simulation(world).run(20)
    asked = {r["perception"]["agent_id"] for r in client.asked}
    assert asked == {entity}


def test_the_perception_carries_the_vocabulary():
    """A mind should not have to guess the answer set from documentation it may not
    have read."""
    world, _ = _world_with_mind()
    client = StubMind({"wants": "food"})
    world.minds = client

    Simulation(world).run(10)
    assert client.asked[0]["perception"]["choices"] == ["food", "wood"]


def test_status_reports_what_is_attached():
    world, _ = _world_with_mind()
    world.minds = StubMind({"wants": "food"})
    status = minds.status(world)

    assert status["enabled"] is True
    assert status["attached"] == 1
    assert status["answering"] == 1
