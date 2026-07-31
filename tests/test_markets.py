"""Prediction markets on simulation events.

The load-bearing property is the first test: a market must not be able to change the world
it is betting on. Everything else — payouts, bounds, determinism — is ordinary correctness
that would still leave the feature unsafe if that one failed.
"""

from __future__ import annotations

import pytest

from src.world.components import Agent, Clan, MarketBook, Needs, Position
from src.world.config import WorldConfig
from src.world.systems import markets
from src.world.tick import Simulation, create_world, state_hash

BASE = dict(seed=4242, grid_width=32, grid_height=32, agent_count=40, resource_count=90)
FAST = dict(market_open_every_ticks=100, market_horizon_ticks=300, market_lock_before_ticks=100)


def _world(**overrides):
    return create_world(WorldConfig(**{**BASE, **FAST, **overrides}))


def _agent_state(world):
    return [
        (
            e,
            world.get(e, Agent).state.value,
            world.get(e, Position).x,
            world.get(e, Position).y,
            world.get(e, Needs).energy,
            world.get(e, Needs).hunger,
        )
        for e in world.query(Agent, Position, Needs)
    ]


def _clan_state(world):
    return [
        (
            world.get(e, Clan).clan_id,
            world.get(e, Clan).goal,
            sorted(world.get(e, Clan).members),
            sorted(world.get(e, Clan).allies),
            {k: list(v) for k, v in sorted(world.get(e, Clan).grudges.items())},
        )
        for e in world.query(Clan)
    ]


# --- the property the whole feature rests on ---------------------------------


def test_markets_cannot_change_the_world_they_bet_on():
    """Spectators watch; they do not move the world.

    Two worlds from the same seed, one with markets running and one without. Every agent
    position, need and state, and every clan goal, membership, truce and grudge, must be
    identical after a long run. If this ever fails, a bet has become able to influence its
    own outcome and the feature is not safe to ship at any level of polish.
    """
    with_markets = Simulation(_world(markets_enabled=True))
    without = Simulation(_world(markets_enabled=False))
    with_markets.run(1200)
    without.run(1200)

    assert _agent_state(with_markets.world) == _agent_state(without.world)
    assert _clan_state(with_markets.world) == _clan_state(without.world)
    # and the markets really did run, or the comparison proves nothing
    assert len(markets.book(with_markets.world).markets) > 0
    assert markets.book(without.world).markets == []


def test_a_market_run_is_reproducible():
    """Same seed, same bets at the same ticks, same book — including payouts."""

    def once():
        simulation = Simulation(_world())
        for tick in range(900):
            simulation.step()
            if tick in (150, 320, 480):
                open_markets = [
                    m for m in markets.book(simulation.world).markets if m["state"] == markets.OPEN
                ]
                if open_markets:
                    markets.place(simulation.world, "ana", open_markets[0]["id"], markets.YES, 25)
        chest = markets.book(simulation.world)
        return (
            [(m["id"], m["kind"], m["state"], m["outcome"]) for m in chest.markets],
            dict(sorted(chest.balances.items())),
        )

    assert once() == once()


def test_a_world_with_markets_still_saves_and_reloads_identically(tmp_path):
    from src.persistence.sqlite_store import SqliteWorldStore

    simulation = Simulation(_world())
    simulation.run(600)
    open_markets = [m for m in markets.book(simulation.world).markets if m["state"] == markets.OPEN]
    if open_markets:
        markets.place(simulation.world, "ana", open_markets[0]["id"], markets.YES, 40)
    simulation.run(5)

    store = SqliteWorldStore(tmp_path / "world.db")
    store.save(simulation.world)
    reloaded = store.load()
    store.close()

    assert state_hash(reloaded) == state_hash(simulation.world)
    before = markets.book(simulation.world)
    after = markets.book(reloaded)
    assert after.balances == before.balances
    assert [m["id"] for m in after.markets] == [m["id"] for m in before.markets]


# --- positions ---------------------------------------------------------------


def _open_market(world):
    for _ in range(400):
        for entry in markets.book(world).markets:
            if entry["state"] == markets.OPEN:
                return entry
        return None
    return None


def _settle(simulation, ticks=1):
    simulation.run(ticks)


def test_a_bet_is_an_input_not_an_immediate_effect():
    """Like a deployment: queued by the caller, applied at a fixed point in the tick. If it
    landed the instant the request arrived, history would depend on wall clock."""
    simulation = Simulation(_world())
    simulation.run(200)
    entry = _open_market(simulation.world)
    assert entry is not None

    markets.place(simulation.world, "ana", entry["id"], markets.YES, 30)
    chest = markets.book(simulation.world)
    assert chest.pending, "the position should be queued, not applied"
    assert entry["pool"][markets.YES] == 0

    _settle(simulation)
    assert chest.pending == []
    assert entry["pool"][markets.YES] == 30
    assert chest.balances["ana"] == 1000 - 30


@pytest.mark.parametrize(
    "stake,reason",
    [(0, "zero"), (-5, "negative"), (101, "over the cap"), (5000, "over the balance")],
)
def test_bad_stakes_are_refused(stake, reason):
    simulation = Simulation(_world())
    simulation.run(200)
    entry = _open_market(simulation.world)
    markets.place(simulation.world, "ana", entry["id"], markets.YES, stake)
    _settle(simulation)

    assert entry["pool"][markets.YES] == 0, f"{reason} stake was accepted"
    assert markets.book(simulation.world).balances.get("ana", 1000) == 1000


def test_a_locked_market_takes_no_more_money():
    simulation = Simulation(_world())
    simulation.run(200)
    entry = _open_market(simulation.world)
    entry["state"] = markets.LOCKED

    markets.place(simulation.world, "ana", entry["id"], markets.YES, 50)
    _settle(simulation)

    assert entry["pool"][markets.YES] == 0
    assert markets.book(simulation.world).balances.get("ana", 1000) == 1000


# --- payout ------------------------------------------------------------------


def _book_with(entries, balances=None):
    chest = MarketBook()
    chest.markets = entries
    chest.balances = balances or {}
    return chest


def _market(pool, positions, state=markets.LOCKED):
    return {
        "id": 1, "kind": "clan_survives", "subject": {}, "question": "",
        "trigger": "test", "opened_tick": 0, "locks_tick": 0, "resolves_tick": 0,
        "state": state, "outcome": None, "pool": pool, "positions": positions,
        "resolved_tick": None, "evidence": None,
    }


def test_the_winners_split_the_whole_pool():
    """Parimutuel: no house. Everything staked is paid out."""
    entry = _market(
        {markets.YES: 100, markets.NO: 100},
        {"ana": {markets.YES: 100, markets.NO: 0}, "bo": {markets.YES: 0, markets.NO: 100}},
    )
    chest = _book_with([entry], {"ana": 0, "bo": 0})
    markets._pay_out(chest, entry, markets.YES)

    assert chest.balances["ana"] == 200, "sole winner takes the whole pool"
    assert chest.balances["bo"] == 0
    assert sum(chest.balances.values()) == 200, "no credits created or destroyed"


def test_the_pool_splits_in_proportion_to_stake():
    entry = _market(
        {markets.YES: 90, markets.NO: 30},
        {
            "ana": {markets.YES: 60, markets.NO: 0},
            "bo": {markets.YES: 30, markets.NO: 0},
            "cy": {markets.YES: 0, markets.NO: 30},
        },
    )
    chest = _book_with([entry], {"ana": 0, "bo": 0, "cy": 0})
    markets._pay_out(chest, entry, markets.YES)

    assert chest.balances["ana"] == 80  # 60/90 of 120
    assert chest.balances["bo"] == 40   # 30/90 of 120
    assert chest.balances["cy"] == 0
    assert sum(chest.balances.values()) == 120


def test_the_division_remainder_goes_to_the_lowest_user_id():
    """Integer arithmetic leaves a remainder. Dropping it would quietly destroy credits;
    handing it to whoever happens to be first in a dict would be non-deterministic. Lowest
    id is the same tie-break the rest of the simulation uses."""
    entry = _market(
        {markets.YES: 3, markets.NO: 7},
        {
            "zed": {markets.YES: 1, markets.NO: 0},
            "ana": {markets.YES: 1, markets.NO: 0},
            "bo": {markets.YES: 1, markets.NO: 0},
            "cy": {markets.YES: 0, markets.NO: 7},
        },
    )
    chest = _book_with([entry], {"zed": 0, "ana": 0, "bo": 0, "cy": 0})
    markets._pay_out(chest, entry, markets.YES)

    assert sum(chest.balances.values()) == 10, "the whole pool is paid out, nothing is lost"
    assert chest.balances["ana"] == 4, "ana sorts first and takes the remainder"
    assert chest.balances["zed"] == 3
    assert chest.balances["bo"] == 3


def test_a_void_refunds_every_stake():
    entry = _market(
        {markets.YES: 40, markets.NO: 60},
        {"ana": {markets.YES: 40, markets.NO: 0}, "bo": {markets.YES: 0, markets.NO: 60}},
    )
    chest = _book_with([entry], {"ana": 0, "bo": 0})
    markets._pay_out(chest, entry, markets.VOID)

    assert chest.balances == {"ana": 40, "bo": 60}


def test_an_uncontested_market_refunds_rather_than_paying_nothing():
    """Everyone on one side and nobody on the other: there is no counterparty, so there is
    nothing to win. Refund beats confiscating the stake."""
    entry = _market(
        {markets.YES: 0, markets.NO: 50}, {"ana": {markets.YES: 0, markets.NO: 50}}
    )
    chest = _book_with([entry], {"ana": 0})
    markets._pay_out(chest, entry, markets.YES)

    assert chest.balances["ana"] == 50


# --- resolution --------------------------------------------------------------


def test_a_dead_clan_settles_immediately_rather_than_at_the_horizon():
    """Death is permanent, so the question is answerable the moment it happens. Waiting
    for the horizon would leave money tied up in a settled fact."""
    world = _world()
    entry = _market({markets.YES: 0, markets.NO: 0}, {}, state=markets.OPEN)
    entry.update(kind="clan_survives", subject={"clan_id": 99}, resolves_tick=10 ** 9)

    outcome = markets._resolve_clan_survives(world, entry, final=False)
    assert outcome is not None
    assert outcome[0] == markets.NO


def test_a_truce_market_voids_when_a_clan_dies_rather_than_resolving_no():
    """Nobody broke the truce. Resolving NO would punish a position that was never wrong."""
    world = _world()
    entry = _market({markets.YES: 0, markets.NO: 0}, {}, state=markets.OPEN)
    entry.update(kind="truce_holds", subject={"clan_a": 98, "clan_b": 99})

    outcome = markets._resolve_truce_holds(world, entry, final=True)
    assert outcome is not None
    assert outcome[0] == markets.VOID


def test_every_kind_has_a_resolver():
    assert set(markets.KINDS) == set(markets.RESOLVERS)


def test_a_resolved_market_records_the_fact_that_settled_it():
    """`evidence` is machine-readable so settlement is auditable, and so this can later be
    handed to external infrastructure without re-parsing the english question."""
    simulation = Simulation(_world())
    simulation.run(1200)
    resolved = [m for m in markets.book(simulation.world).markets if m["state"] == markets.RESOLVED]
    assert resolved, "nothing settled in 1200 ticks"
    for entry in resolved:
        assert entry["outcome"] in (markets.YES, markets.NO, markets.VOID)
        assert isinstance(entry["evidence"], dict) and entry["evidence"]
        assert entry["resolved_tick"] is not None


# --- bounds ------------------------------------------------------------------


def test_open_markets_are_capped():
    simulation = Simulation(_world(market_max_open=3, market_open_every_ticks=20))
    simulation.run(1500)
    live = [m for m in markets.book(simulation.world).markets if m["state"] != markets.RESOLVED]
    assert len(live) <= 3


def test_history_is_bounded():
    """Every unbounded accumulator in this project has eventually eaten the simulation."""
    simulation = Simulation(
        _world(market_history_limit=10, market_open_every_ticks=20, market_horizon_ticks=60,
               market_lock_before_ticks=20)
    )
    simulation.run(3000)
    chest = markets.book(simulation.world)
    assert len(chest.markets) <= 10 + chest_open(chest)


def chest_open(chest):
    return sum(1 for m in chest.markets if m["state"] != markets.RESOLVED)


def test_markets_can_be_switched_off_entirely():
    simulation = Simulation(_world(markets_enabled=False))
    simulation.run(600)
    assert markets.book(simulation.world).markets == []


def test_every_component_in_a_live_world_is_persisted():
    """A component type missing from the store's registry is silently dropped on reload —
    no error, no warning, just a None where state used to be. MarketBook hit exactly that
    on the way in. This guards the next one rather than the last one."""
    from src.persistence.sqlite_store import COMPONENT_TYPES

    simulation = Simulation(_world())
    simulation.run(600)
    present = set(simulation.world.component_types())
    missing = sorted(t.__name__ for t in present if t not in COMPONENT_TYPES)
    assert missing == [], f"not registered with SqliteWorldStore: {missing}"


# --- http surface ------------------------------------------------------------


def _client(tmp_path, **overrides):
    from fastapi.testclient import TestClient
    from src.api.server import create_app

    # The live server ticks once a second, so a market cadence measured in hundreds of
    # ticks would never open one inside a test. Only the schedule is sped up.
    config = WorldConfig(**{**BASE, **FAST, **overrides})
    return TestClient(create_app(config=config, db_path=tmp_path / "markets.db"))


def test_the_api_serves_the_book_and_a_leaderboard(tmp_path):
    with _client(tmp_path) as client:
        body = client.get("/markets").json()
        assert set(body) >= {"open", "settled", "balances", "leaderboard", "max_stake"}
        assert body["max_stake"] == 100
        assert body["starting_balance"] == 1000


@pytest.mark.parametrize(
    "payload,status,why",
    [
        ({"user": "", "side": "yes", "stake": 10}, 422, "empty user"),
        ({"user": "ana", "side": "maybe", "stake": 10}, 422, "not a side"),
        ({"user": "ana", "side": "yes", "stake": 0}, 422, "zero stake"),
        ({"user": "ana", "side": "yes", "stake": 101}, 422, "over the cap"),
    ],
)
def test_the_api_refuses_bad_positions(tmp_path, payload, status, why):
    with _client(tmp_path) as client:
        response = client.post("/markets/1/positions", json=payload)
        assert response.status_code == status, why


def test_the_api_refuses_a_market_that_does_not_exist(tmp_path):
    with _client(tmp_path) as client:
        response = client.post(
            "/markets/9999/positions", json={"user": "ana", "side": "yes", "stake": 10}
        )
        assert response.status_code == 404


def test_a_position_taken_over_http_reaches_the_book(tmp_path):
    """End to end: the request only queues, and the tick applies it."""
    import time

    with _client(tmp_path, market_open_every_ticks=1, market_horizon_ticks=40,
                 market_lock_before_ticks=10) as client:
        entry = None
        for _ in range(60):
            open_markets = client.get("/markets").json()["open"]
            if open_markets:
                entry = open_markets[0]
                break
            time.sleep(0.2)
        if entry is None:
            pytest.skip("no market opened inside the polling window")

        accepted = client.post(
            f"/markets/{entry['id']}/positions",
            json={"user": "ana", "side": "yes", "stake": 25},
        )
        assert accepted.status_code == 202
        assert accepted.json()["queued"] is True

        for _ in range(40):
            body = client.get("/markets").json()
            if body["balances"].get("ana") is not None:
                assert body["balances"]["ana"] == 975
                assert body["leaderboard"][0]["user"] == "ana"
                return
            time.sleep(0.2)
        pytest.fail("the queued position never reached the book")
