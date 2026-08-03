"""LLM-generated leader messages are broadcast and fall back to templates when absent."""

from __future__ import annotations

from src.world.components import (
    AdvisorState,
    Agent,
    Blackboard,
    Clan,
    ClanRef,
    DecisionLog,
    Inbox,
    Inventory,
    MessageLog,
    Needs,
    Outbox,
    Position,
    SpawnQueue,
)
from src.world.config import WorldConfig
from src.world.ecs import World
from src.llm.advisor import ScriptedAdvisor
from src.world.systems import leadership, messaging
from src.world.tick import Simulation, build_registry


CONFIG = WorldConfig(
    seed=8,
    grid_width=16,
    grid_height=16,
    agent_count=8,
    resource_count=30,
    llm_enabled=True,
    llm_provider="scripted",
    llm_min_ticks_between_calls=1,
    llm_max_calls_per_session=10,
)


def _world_with_leader() -> tuple[Simulation, int, int]:
    """Return a simulation, leader entity id, and clan id."""
    world = World(CONFIG)
    world.add(world.create_entity(), Blackboard())
    world.add(world.create_entity(), SpawnQueue())
    world.add(world.create_entity(), DecisionLog())
    world.add(world.create_entity(), MessageLog())
    world.add(world.create_entity(), AdvisorState())

    leader = world.create_entity()
    world.add(leader, Agent(name="Leader", personality="hoards wood and builds walls"))
    world.add(leader, Position(5, 5))
    world.add(leader, Needs(energy=50, hunger=50, social=50, safety=50))
    world.add(leader, Inventory())
    world.add(leader, ClanRef())
    world.add(leader, Inbox())
    world.add(leader, Outbox())

    member = world.create_entity()
    world.add(member, Agent(name="Member"))
    world.add(member, Position(5, 5))
    world.add(member, Needs(energy=50, hunger=50, social=50, safety=50))
    world.add(member, Inventory())
    world.add(member, ClanRef())
    world.add(member, Inbox())
    world.add(member, Outbox())

    clan = Clan(clan_id=1)
    clan.add(leader)
    clan.add(member)
    clan.leader = leader
    world.add(world.create_entity(), clan)
    world.get(leader, ClanRef).clan_id = 1
    world.get(member, ClanRef).clan_id = 1

    sim = Simulation(world, registry=build_registry())
    return sim, leader, clan.clan_id


def _deliver(world: World) -> None:
    world.tick += 1
    messaging.run(world, None)


def test_llm_message_is_broadcast_to_clan():
    sim, leader, clan_id = _world_with_leader()
    sim.world.advisor = ScriptedAdvisor(
        {clan_id: "expand"},
        lag_calls=0,
        messages={clan_id: "Wood first, walls second."},
    )
    sim.world.tick = 1
    leadership.run(sim.world, None)  # submit
    sim.world.tick = 2
    leadership.run(sim.world, None)  # collect + apply
    _deliver(sim.world)

    member = [e for e in sim.world.query(Agent, Inbox) if e != leader][0]
    inbox = sim.world.get(member, Inbox)
    assert len(inbox.messages) == 1
    assert inbox.messages[0]["content"]["text"] == "Wood first, walls second."


def test_llm_message_caps_length():
    sim, leader, clan_id = _world_with_leader()
    long_message = "x" * 200
    sim.world.advisor = ScriptedAdvisor(
        {clan_id: "expand"},
        lag_calls=0,
        messages={clan_id: long_message},
    )
    sim.world.tick = 1
    leadership.run(sim.world, None)
    sim.world.tick = 2
    leadership.run(sim.world, None)
    _deliver(sim.world)

    member = [e for e in sim.world.query(Agent, Inbox) if e != leader][0]
    text = sim.world.get(member, Inbox).messages[0]["content"]["text"]
    assert len(text) <= 120


def test_no_llm_message_falls_back_to_composer():
    sim, leader, clan_id = _world_with_leader()
    sim.world.advisor = ScriptedAdvisor(
        {clan_id: "gather_food"},
        lag_calls=0,
        messages={clan_id: ""},
    )
    sim.world.tick = 1
    leadership.run(sim.world, None)
    sim.world.tick = 2
    leadership.run(sim.world, None)
    _deliver(sim.world)

    member = [e for e in sim.world.query(Agent, Inbox) if e != leader][0]
    text = sim.world.get(member, Inbox).messages[0]["content"]["text"]
    assert "food" in text.lower()


def test_personality_appears_in_brief_prompt():
    world = World(CONFIG)
    leader = world.create_entity()
    world.add(leader, Agent(name="Leader", personality="hates strangers"))
    world.add(leader, Position(0, 0))
    clan = Clan(clan_id=1)
    clan.add(leader)
    clan.leader = leader
    world.add(world.create_entity(), clan)

    brief = leadership._brief(world, clan, 0)
    assert brief.leader_personality == "hates strangers"
    assert brief.leader_name == "Leader"
    assert "hates strangers" in brief.as_prompt()


def test_budget_still_holds():
    from src.world.components import AdvisorState

    sim, leader, clan_id = _world_with_leader()
    sim.world.advisor = ScriptedAdvisor(
        {clan_id: "expand"},
        lag_calls=0,
        messages={clan_id: "Build!"},
    )
    sim.world.config = CONFIG
    sim.world.tick = 1
    calls_before = sim.world.advisor.calls
    leadership.run(sim.world, None)
    assert sim.world.advisor.calls > calls_before

    # The durable budget in AdvisorState is also incremented.
    state_entity = sim.world.first(AdvisorState)
    state = sim.world.get(state_entity, AdvisorState)
    assert state.calls_made > 0
