"""Standing and rank progression. Standing only increases from recorded actions."""

from __future__ import annotations

import pytest

from src.world.components import (
    Agent,
    Clan,
    ClanRef,
    Inbox,
    Inventory,
    Needs,
    Outbox,
    Position,
    Standing,
)
from src.world.config import WorldConfig
from src.world.ecs import World
from src.world.systems import standing, trade
from src.world.tick import Simulation, create_world


CONFIG = WorldConfig(
    seed=9,
    grid_width=16,
    grid_height=16,
    agent_count=4,
    resource_count=20,
    standing_trusted_threshold=10,
    standing_officer_threshold=30,
    standing_per_hut_built=10,
    standing_per_food_given=3,
    standing_per_wood_given=3,
    standing_per_raid_won=40,
    standing_per_survival_ticks=200,
    standing_per_membership_ticks=100,
)


def _agent(world: World, name: str = "A") -> int:
    entity = world.create_entity()
    world.add(entity, Agent(name=name))
    world.add(entity, Position(0, 0))
    world.add(entity, Needs(energy=50, hunger=50, social=50, safety=50))
    world.add(entity, Inventory())
    world.add(entity, ClanRef())
    world.add(entity, Inbox())
    world.add(entity, Outbox())
    world.add(entity, Standing())
    return entity


def test_standing_created_lazily():
    world = World(CONFIG)
    entity = world.create_entity()
    world.add(entity, Agent(name="Bare"))
    assert not world.has(entity, Standing)
    standing._ensure_standing(world, entity)
    assert world.has(entity, Standing)
    assert world.get(entity, Standing).value == 0
    assert world.get(entity, Standing).rank == "member"


def test_add_standing_increases_action_value():
    world = World(CONFIG)
    entity = _agent(world)
    standing.add_standing(world, entity, 5)
    assert world.get(entity, Standing).action_value == 5


def test_standing_cap_prevents_unbounded_growth():
    world = World(CONFIG)
    entity = _agent(world)
    standing.add_standing(world, entity, 10000)
    st = world.get(entity, Standing)
    assert st.action_value <= standing._max_standing(world)
    assert st.value <= standing._max_standing(world)


def test_record_food_given_tracks_donation_and_standing():
    world = World(CONFIG)
    donor = _agent(world)
    world.get(donor, Inventory).food = 5
    standing.record_resource_given(world, donor, food=5)
    s = world.get(donor, Standing)
    assert s.given_food == 5
    assert s.action_value == 5 * CONFIG.standing_per_food_given


def test_record_wood_given_tracks_donation_and_standing():
    world = World(CONFIG)
    donor = _agent(world)
    world.get(donor, Inventory).wood = 5
    standing.record_resource_given(world, donor, wood=5)
    s = world.get(donor, Standing)
    assert s.given_wood == 5
    assert s.action_value == 5 * CONFIG.standing_per_wood_given


def test_record_hut_built_promotes_to_trusted():
    world = World(CONFIG)
    entity = _agent(world)
    standing.record_hut_built(world, entity)
    s = world.get(entity, Standing)
    assert s.action_value == CONFIG.standing_per_hut_built
    # One hut is enough for trusted under the low test threshold.
    standing._promote(world, s)
    assert s.rank == "trusted"


def test_record_raid_won_promotes_to_officer():
    world = World(CONFIG)
    entity = _agent(world)
    standing.record_raid_won(world, entity)
    s = world.get(entity, Standing)
    assert s.action_value == CONFIG.standing_per_raid_won
    standing._promote(world, s)
    assert s.rank == "officer"


def test_promotion_only_moves_upward():
    world = World(CONFIG)
    entity = _agent(world)
    s = world.get(entity, Standing)
    s.rank = "officer"
    s.value = 0
    standing._promote(world, s)
    assert s.rank == "officer"


def test_trade_transfer_records_donor_standing():
    world = World(CONFIG)
    donor = _agent(world)
    receiver = _agent(world)
    world.get(donor, Inventory).food = 5
    world.get(receiver, Inventory).food = 0

    trade.transfer(world, donor, receiver, trade.ResourceKind.FOOD, 5)

    assert world.get(donor, Standing).value > 0
    assert world.get(donor, Standing).given_food == 5
    assert world.get(receiver, Standing).value == 0


def test_clan_membership_time_adds_standing():
    world = World(CONFIG)
    entity = _agent(world)
    world.get(entity, Agent).spawn_tick = 0
    world.get(entity, ClanRef).clan_id = 1
    clan = Clan(clan_id=1)
    clan.add(entity)
    world.add(world.create_entity(), clan)

    standing.record_clan_join(world, entity)
    world.tick = 100
    standing.run(world, None)

    s = world.get(entity, Standing)
    assert s.value == 100 // CONFIG.standing_per_membership_ticks


def test_no_membership_standing_when_not_in_clan():
    world = World(CONFIG)
    entity = _agent(world)
    world.get(entity, Agent).spawn_tick = 0
    world.get(entity, ClanRef).clan_id = 1
    standing.record_clan_join(world, entity)
    world.tick = 100
    standing.run(world, None)
    assert world.get(entity, Standing).value == 0


def test_successor_prefers_officer():
    world = World(CONFIG)
    leader = _agent(world)
    officer = _agent(world)
    member = _agent(world)

    clan = Clan(clan_id=1)
    clan.add(leader)
    clan.add(officer)
    clan.add(member)
    clan.leader = leader
    world.add(world.create_entity(), clan)

    world.get(officer, Standing).rank = "officer"
    world.get(member, Standing).rank = "member"

    successor = standing.eligible_successor(world, clan)
    assert successor == officer


def test_successor_falls_back_to_none_without_officer():
    world = World(CONFIG)
    member = _agent(world)
    clan = Clan(clan_id=1)
    clan.add(member)
    world.add(world.create_entity(), clan)
    assert standing.eligible_successor(world, clan) is None


def test_social_prune_prefers_officer_as_leader():
    """When the leader dies, an officer inherits before a lower-id member."""
    from src.world.systems import social
    from src.world.rng import TickRng

    world = World(CONFIG)
    leader = _agent(world)
    officer = _agent(world)
    member = _agent(world)

    clan = Clan(clan_id=1)
    for m in (leader, officer, member):
        clan.add(m)
    clan.leader = leader
    world.add(world.create_entity(), clan)
    for m in (leader, officer, member):
        world.get(m, ClanRef).clan_id = 1

    world.get(officer, Standing).rank = "officer"
    world.get(officer, Standing).action_value = CONFIG.standing_officer_threshold
    world.get(member, Standing).rank = "member"

    # Kill the leader.
    world.destroy_entity(leader)
    world.tick = 1
    social.run(world, TickRng(CONFIG.seed, world.tick, "social"))

    assert clan.leader == officer


def test_social_prune_falls_back_to_lowest_id_without_officer():
    """Without an officer, the existing lowest-id succession rule still applies."""
    from src.world.systems import social
    from src.world.rng import TickRng

    world = World(CONFIG)
    leader = _agent(world)
    first = _agent(world)
    second = _agent(world)

    clan = Clan(clan_id=1)
    for m in (leader, first, second):
        clan.add(m)
    clan.leader = leader
    world.add(world.create_entity(), clan)
    for m in (leader, first, second):
        world.get(m, ClanRef).clan_id = 1

    # Ensure lowest id is the first added member.
    assert first < second
    world.destroy_entity(leader)
    world.tick = 1
    social.run(world, TickRng(CONFIG.seed, world.tick, "social"))

    assert clan.leader == first


def test_standing_is_durable_across_save_load():
    from src.persistence.sqlite_store import SqliteWorldStore
    from tempfile import TemporaryDirectory
    from pathlib import Path

    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "world.db"
        store = SqliteWorldStore(db)
        world = create_world(CONFIG)
        entity = next(iter(world.query(Agent)))
        standing.record_hut_built(world, entity)
        store.save(world)
        store.close()

        store2 = SqliteWorldStore(db)
        loaded = store2.load()
        st = loaded.get(entity, Standing)
        assert st.action_value == CONFIG.standing_per_hut_built
        assert st.value == CONFIG.standing_per_hut_built
        store2.close()
