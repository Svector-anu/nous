"""Putting a paid-for decision on a public chain.

A visitor pays over x402 to make a clan leader reconsider; this writes a transaction when
that happens, so the decision has a receipt that does not depend on trusting this server's
own log.

What these mostly guard is the direction of the dependency. The world decides on its own
and the chain observes; a chain that is slow, broken, or absent must leave the world
behaving exactly as it did before any of this existed. Every failure here is a `None`.

No test in this file touches the network.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.chain import keeperhub

TX = "0x" + "d" * 64
KEY = "kh_test_key_never_real"


class FakeResponse:
    def __init__(self, status_code: int = 202, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """Stands in for httpx.AsyncClient. Records what was sent so the request itself can be
    asserted, which is the only way to know a credential did not leak into a body."""

    sent: list = []

    def __init__(self, answer, *, timeout=None, raises: Exception | None = None):
        self.answer = answer
        self.raises = raises
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        FakeClient.sent.append({"url": url, "json": json, "headers": headers})
        if self.raises is not None:
            raise self.raises
        return self.answer


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Every test starts from a world that has not been configured for this."""
    FakeClient.sent = []
    for name in (
        keeperhub.ENABLED_ENV,
        keeperhub.API_KEY_ENV,
        keeperhub.RECIPIENT_ENV,
        keeperhub.CHAIN_ID_ENV,
        keeperhub.AMOUNT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _live(monkeypatch, answer=None, raises=None):
    """A world switched on, with a scripted answer from KeeperHub."""
    monkeypatch.setenv(keeperhub.ENABLED_ENV, "true")
    monkeypatch.setenv(keeperhub.API_KEY_ENV, KEY)
    monkeypatch.setenv(keeperhub.RECIPIENT_ENV, "0x" + "a" * 40)
    reply = answer if answer is not None else FakeResponse(
        202, {"executionId": "direct_1", "status": "completed", "transactionHash": TX}
    )
    monkeypatch.setattr(
        keeperhub.httpx,
        "AsyncClient",
        lambda **kw: FakeClient(reply, raises=raises, **{}),
    )
    return reply


def _fire(**kwargs):
    """The hash alone, which is what most of these assert on."""
    executed = asyncio.run(keeperhub.fire_action(**kwargs))
    return executed.tx_hash if executed is not None else None


def _execute(**kwargs):
    """The whole result, for the tests that care where the link came from."""
    return asyncio.run(keeperhub.fire_action(**kwargs))


# --- the switch ---------------------------------------------------------------------
#
# The idle world must cost $0 and must never surprise its operator by appearing on a chain
# explorer. Off is the default and off means no socket, not a call that is thrown away.


def test_a_world_that_was_never_switched_on_is_not_enabled():
    assert keeperhub.enabled() is False
    assert keeperhub.configured() is False


def test_disabled_attempts_no_network_call_at_all(monkeypatch):
    """Not merely "returns None" — the check is before the client is built, so a world
    nobody asked for this cannot be slowed down by a gateway it never wanted."""
    _live(monkeypatch)
    monkeypatch.setenv(keeperhub.ENABLED_ENV, "false")

    assert _fire(memo="clan 1 raid tick 5") is None
    assert FakeClient.sent == [], "a disabled world opened a connection"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_the_switch_accepts_the_usual_spellings(monkeypatch, raw):
    monkeypatch.setenv(keeperhub.ENABLED_ENV, raw)
    assert keeperhub.enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "maybe"])
def test_anything_else_leaves_it_off(monkeypatch, raw):
    """Unrecognised means off. A typo in a variable that puts transactions on a chain must
    fail towards doing nothing."""
    monkeypatch.setenv(keeperhub.ENABLED_ENV, raw)
    assert keeperhub.enabled() is False


def test_enabled_without_a_key_is_not_configured(monkeypatch):
    """Two different problems: nobody switched it on, and it is on but cannot
    authenticate. Reporting them as one leaves somebody looking in the wrong place."""
    monkeypatch.setenv(keeperhub.ENABLED_ENV, "true")
    assert keeperhub.enabled() is True
    assert keeperhub.configured() is False


def test_enabled_without_a_key_makes_no_call(monkeypatch):
    monkeypatch.setenv(keeperhub.ENABLED_ENV, "true")
    monkeypatch.setattr(keeperhub.httpx, "AsyncClient", lambda **kw: FakeClient(FakeResponse()))

    assert _fire(memo="clan 1 raid tick 5") is None
    assert FakeClient.sent == []


# --- the happy path -----------------------------------------------------------------


def test_a_completed_execution_returns_its_transaction_hash(monkeypatch):
    _live(monkeypatch)
    assert _fire(memo="clan 1 raid tick 5") == TX


def test_the_request_goes_to_the_documented_endpoint(monkeypatch):
    _live(monkeypatch)
    _fire(memo="clan 1 raid tick 5")

    sent = FakeClient.sent[0]
    assert sent["url"] == "https://app.keeperhub.com/api/execute/transfer"
    assert sent["headers"]["authorization"] == f"Bearer {KEY}"


def test_the_body_carries_only_documented_fields(monkeypatch):
    """KeeperHub documents chainId, recipientAddress, amount, tokenAddress, tokenConfig and
    gasLimitMultiplier. An undocumented field is a 400 waiting to happen, and worse, it
    changes the body hash so every retry becomes an idempotency conflict."""
    _live(monkeypatch)
    _fire(memo="clan 1 raid tick 5")

    allowed = {"chainId", "recipientAddress", "amount", "tokenAddress", "tokenConfig", "gasLimitMultiplier"}
    assert set(FakeClient.sent[0]["json"]) <= allowed


def test_the_target_is_base_sepolia_by_default(monkeypatch):
    """A testnet on purpose. The receipt proves the decision either way, and a chain where
    the funds are worthless cannot be drained by a bug in this world."""
    _live(monkeypatch)
    _fire(memo="clan 1 raid tick 5")

    assert FakeClient.sent[0]["json"]["chainId"] == 84532


def test_the_chain_can_be_moved_without_a_deploy(monkeypatch):
    """KeeperHub's own advice is to read GET /api/chains and pick one that is enabled and a
    testnet, and which chains qualify can change without us shipping anything."""
    _live(monkeypatch)
    monkeypatch.setenv(keeperhub.CHAIN_ID_ENV, "11155111")
    _fire(memo="clan 1 raid tick 5")

    assert FakeClient.sent[0]["json"]["chainId"] == 11155111


def test_a_nonsense_chain_id_falls_back_rather_than_guessing(monkeypatch):
    monkeypatch.setenv(keeperhub.CHAIN_ID_ENV, "base-sepolia-ish")
    assert keeperhub.chain_id() == keeperhub.DEFAULT_CHAIN_ID


def test_the_recipient_is_lowercased(monkeypatch):
    """KeeperHub validates with a strict EIP-55 checksum and accepts the exact mixed-case
    form or an all-lowercase one. A mixed-case address with a mangled checksum is refused
    even when its hex is right, which is how most copied addresses fail."""
    _live(monkeypatch)
    _fire(recipient_address="0xAbCdEf0123456789AbCdEf0123456789AbCdEf01", memo="m")

    assert FakeClient.sent[0]["json"]["recipientAddress"].islower()


# --- the credential ------------------------------------------------------------------


def test_the_key_never_reaches_the_request_body(monkeypatch):
    """It is an organization-wide credential that can move funds. It belongs in the
    authorization header and nowhere else."""
    _live(monkeypatch)
    _fire(memo="clan 1 raid tick 5")

    assert KEY not in str(FakeClient.sent[0]["json"])


def test_the_key_is_never_logged(monkeypatch, caplog):
    """A log line is the easiest way for a credential to reach a place nobody is guarding —
    a screen recording, a shipped log, a pasted traceback."""
    import logging

    _live(monkeypatch, answer=FakeResponse(403, None, "Daily spending cap exceeded"))
    with caplog.at_level(logging.DEBUG, logger="neociv"):
        _fire(memo="clan 1 raid tick 5")

    assert KEY not in caplog.text


# --- failing closed -------------------------------------------------------------------
#
# The caller is a background job whose failure would be invisible, so nothing here may
# raise. A world that cannot reach KeeperHub carries on exactly as it did before.


def test_an_unreachable_gateway_returns_none(monkeypatch):
    _live(monkeypatch, raises=httpx.ConnectError("no route"))
    assert _fire(memo="clan 1 raid tick 5") is None


def test_a_timeout_returns_none(monkeypatch):
    _live(monkeypatch, raises=httpx.ReadTimeout("too slow"))
    assert _fire(memo="clan 1 raid tick 5") is None


@pytest.mark.parametrize(
    "status,text",
    [
        (400, "Invalid recipient address"),
        (401, "unauthorized"),
        (403, "Daily spending cap exceeded"),
        (409, "idempotency_conflict"),
        (429, "rate limited"),
        (500, "boom"),
    ],
)
def test_every_refusal_returns_none_without_raising(monkeypatch, status, text):
    """The bodies here are deliberately well-formed json that *looks* like success.

    Written first with unparseable bodies, this passed with the status check deleted: the
    json parser rejected them and the status was never consulted. A refusal carrying a
    plausible body is the only version of this test that can fail.
    """
    body = {"executionId": "direct_1", "status": "completed", "transactionHash": TX, "error": text}
    _live(monkeypatch, answer=FakeResponse(status, body, text))
    assert _fire(memo="clan 1 raid tick 5") is None


def test_a_body_that_is_not_json_returns_none(monkeypatch):
    _live(monkeypatch, answer=FakeResponse(202, None, "<html>gateway</html>"))
    assert _fire(memo="clan 1 raid tick 5") is None


def test_a_failed_execution_returns_none(monkeypatch):
    """Documented: transactionHash is present only when status is completed. A failed
    execution is a real answer, not a malformed one, and must not be reported as proof."""
    _live(monkeypatch, answer=FakeResponse(202, {"executionId": "direct_1", "status": "failed"}))
    assert _fire(memo="clan 1 raid tick 5") is None


def test_a_completed_status_with_no_hash_returns_none(monkeypatch):
    _live(monkeypatch, answer=FakeResponse(202, {"status": "completed", "transactionHash": ""}))
    assert _fire(memo="clan 1 raid tick 5") is None


def test_an_unexpected_shape_returns_none(monkeypatch):
    _live(monkeypatch, answer=FakeResponse(202, ["not", "a", "dict"]))
    assert _fire(memo="clan 1 raid tick 5") is None


# --- not executing the same decision twice ---------------------------------------------


def test_the_same_decision_derives_the_same_key():
    """The key must identify the work, not the attempt. This process forgets what it has
    fired when it restarts, so the only thing standing between a restart and a duplicate
    transaction is that the same decision produces the same key."""
    memo = "nous clan 4 raid tick 900"
    assert keeperhub.idempotency_key(memo) == keeperhub.idempotency_key(memo)


def test_different_decisions_derive_different_keys():
    """Two genuinely different decisions colliding on one key would mean the second is
    answered from the first's stored response and never happens at all."""
    a = keeperhub.idempotency_key("nous clan 4 raid tick 900")
    b = keeperhub.idempotency_key("nous clan 5 raid tick 900")
    c = keeperhub.idempotency_key("nous clan 4 raid tick 901")

    assert len({a, b, c}) == 3


def test_the_key_is_sent(monkeypatch):
    _live(monkeypatch)
    _fire(memo="nous clan 4 raid tick 900")

    sent = FakeClient.sent[0]["headers"]["idempotency-key"]
    assert sent == keeperhub.idempotency_key("nous clan 4 raid tick 900")


def test_a_key_carries_no_clock(monkeypatch):
    """Derived from the decision alone. A timestamp or a random value would make every
    retry a new execution, which is the failure idempotency exists to prevent."""
    first = keeperhub.idempotency_key("nous clan 4 raid tick 900")
    second = keeperhub.idempotency_key("nous clan 4 raid tick 900")
    assert first == second


# --- the proof link ---------------------------------------------------------------------


def test_a_hash_becomes_a_link_anybody_can_open():
    assert keeperhub.explorer_link(TX) == f"https://sepolia.basescan.org/tx/{TX}"


def test_no_link_is_offered_for_a_chain_we_cannot_name(monkeypatch):
    """A base sepolia explorer url for some other chain is a link that silently shows
    nothing, which is worse than no link at all."""
    monkeypatch.setenv(keeperhub.CHAIN_ID_ENV, "11155111")
    assert keeperhub.explorer_link(TX) == ""


def test_no_hash_is_no_link():
    assert keeperhub.explorer_link("") == ""


# --- what earns a receipt -----------------------------------------------------------
#
# The first version of this scanned the decision log for `raid`, and it was wrong in a way
# no unit test caught: a settled world never raids. Five thousand ticks of the live world
# produced 101 rally, 99 gather_food and zero raids, so the feature was correct code wired
# to an event that does not happen — trap 10, shipped.
#
# What earns a receipt now is a decision somebody paid for. Rare by construction, because
# it costs money, and already recorded at a fixed tick by `apply_forced_decisions`.

from src.api.server import _decisions_owed_a_receipt, _what_the_leader_chose  # noqa: E402
from src.world.components import DecisionLog, ForceDecisionQueue  # noqa: E402
from src.world.config import WorldConfig  # noqa: E402
from src.world.ecs import World  # noqa: E402

WORLD_CONFIG = WorldConfig(seed=5, grid_width=32, grid_height=32, agent_count=8, resource_count=20)


def _paid_world(applied=(), decisions=()):
    """A world carrying only what the reader looks at."""
    world = World(WORLD_CONFIG)
    queue = ForceDecisionQueue()
    queue.applied.extend(applied)
    world.add(world.create_entity(), queue)
    log = DecisionLog()
    log.entries.extend(decisions)
    world.add(world.create_entity(), log)
    return world


def _payment(clan_id=3, tick=900):
    return {
        "clan_id": clan_id,
        "tick": tick,
        "proof": "tx:" + "b" * 64,
        "amount": "0.10",
        "currency": "USDG",
    }


def test_a_paid_decision_is_owed_a_receipt():
    world = _paid_world(applied=[_payment()])
    owed = _decisions_owed_a_receipt(world, set())
    assert [(e["clan_id"], e["tick"]) for e in owed] == [(3, 900)]


def test_the_payment_that_bought_it_is_carried_through():
    """The x402 proof is half the claim. A receipt that dropped it would prove a decision
    happened while losing the fact that somebody paid for it."""
    world = _paid_world(applied=[_payment()])
    owed = _decisions_owed_a_receipt(world, set())
    assert owed[0]["proof"] == "tx:" + "b" * 64
    assert owed[0]["currency"] == "USDG"


def test_a_receipt_is_never_owed_twice():
    """The list stays in world state for twenty entries. Without this the same payment
    would buy a fresh transaction every fifteen seconds until it rotated out."""
    world = _paid_world(applied=[_payment()])
    assert _decisions_owed_a_receipt(world, {(900, 3)}) == []


def test_two_payments_for_one_clan_are_two_receipts():
    """Same clan, different ticks: somebody paid twice and is owed two."""
    world = _paid_world(applied=[_payment(tick=900), _payment(tick=1400)])
    owed = _decisions_owed_a_receipt(world, {(900, 3)})
    assert [e["tick"] for e in owed] == [1400]


def test_an_ordinary_decision_earns_no_receipt():
    """The one that would have caught the raid bug.

    A world busy deciding things nobody paid for owes nothing. This is what keeps the
    chain free of a transaction every few seconds, and it is asserted against the goals
    the live world actually produces rather than the one it never does.
    """
    busy = [
        {"tick": 520000 + n * 28, "clan": n % 4, "goal": "rally" if n % 2 else "gather_food"}
        for n in range(50)
    ]
    world = _paid_world(applied=[], decisions=busy)
    assert _decisions_owed_a_receipt(world, set()) == []


def test_a_world_without_the_queue_owes_nothing():
    world = World(WORLD_CONFIG)
    assert _decisions_owed_a_receipt(world, set()) == []


# --- what the payment bought --------------------------------------------------------


def _clan(world, clan_id=3, goal="expand", reason="we have the wood", source="llm", at=900):
    from src.world.components import Clan

    clan = Clan(clan_id=clan_id)
    clan.goal = goal
    clan.goal_reason = reason
    clan.goal_source = source
    clan.goal_set_tick = at
    world.add(world.create_entity(), clan)
    return clan


def test_the_receipt_names_the_goal_the_payment_bought():
    world = _paid_world(applied=[_payment()])
    _clan(world)
    chose = _what_the_leader_chose(world, 3, 900)
    assert chose["goal"] == "expand"
    assert chose["reason"] == "we have the wood"
    assert chose["source"] == "llm"


def test_a_review_that_kept_its_goal_still_names_it():
    """The one the first live run got wrong.

    `social` logs a decision only when the goal *changes*, so a leader that reconsidered
    and stayed put left no entry — and a receipt built from that log said nothing at all.
    A paid review that confirms a goal is still a decision somebody paid for.
    """
    world = _paid_world(applied=[_payment()], decisions=[])
    _clan(world, goal="rally", reason="", source="rules")
    chose = _what_the_leader_chose(world, 3, 900)
    assert chose["goal"] == "rally"


def test_another_clan_is_never_credited():
    world = _paid_world(applied=[_payment()])
    _clan(world, clan_id=11, goal="raid")
    assert _what_the_leader_chose(world, 3, 900) == {}


def test_a_clan_that_no_longer_exists_names_nothing():
    """Empty is honest. A receipt naming the wrong goal is worse than one naming none."""
    world = _paid_world(applied=[_payment()])
    assert _what_the_leader_chose(world, 3, 900) == {}


def test_the_receipt_says_when_the_goal_was_actually_set():
    """A review that confirmed an older goal and one that set a new one are different
    outcomes, and the tick is what tells them apart."""
    world = _paid_world(applied=[_payment()])
    _clan(world, at=812)
    assert _what_the_leader_chose(world, 3, 900)["decided_at_tick"] == 812


# --- the endpoint -------------------------------------------------------------------
#
# Route bodies are not checked at import. Deleting a module constant the handler still
# referenced left a NameError that nothing caught until a request arrived, which on a
# world serving 24/7 means a stranger finds it first.

import pathlib  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

from src.api.server import create_app  # noqa: E402

APP_CONFIG = WorldConfig(seed=5, grid_width=32, grid_height=32, agent_count=8, resource_count=20)


def test_the_endpoint_answers_on_a_world_that_never_switched_this_on(tmp_path: pathlib.Path):
    """The common case by far: off, and still a real answer rather than a 500."""
    app = create_app(APP_CONFIG, tmp_path / "k.db")
    with TestClient(app) as client:
        response = client.get("/onchain")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["receipts"] == []


def test_the_endpoint_serves_a_receipt_once_there_is_one(tmp_path: pathlib.Path):
    from src.api.server import _remember_receipt

    app = create_app(APP_CONFIG, tmp_path / "k.db")
    with TestClient(app) as client:
        _remember_receipt(
            app,
            {"tick": 900, "clan": 3, "goal": "expand", "tx": TX, "link": "https://x/tx", "paid": {}},
        )
        body = client.get("/onchain").json()

    assert [r["tx"] for r in body["receipts"]] == [TX]


def test_the_kept_receipts_are_bounded(tmp_path: pathlib.Path):
    """An accumulator on a world that runs forever. Every unbounded one added here has
    eventually eaten the simulation."""
    from src.api.server import ONCHAIN_RECEIPT_LIMIT, _remember_receipt

    app = create_app(APP_CONFIG, tmp_path / "k.db")
    with TestClient(app) as client:
        for n in range(ONCHAIN_RECEIPT_LIMIT * 3):
            _remember_receipt(app, {"tick": n, "clan": 1, "tx": f"0x{n:064x}"})
        body = client.get("/onchain").json()

    assert len(body["receipts"]) == ONCHAIN_RECEIPT_LIMIT
    assert body["receipts"][-1]["tick"] == ONCHAIN_RECEIPT_LIMIT * 3 - 1


# --- where the link comes from ------------------------------------------------------
#
# KeeperHub returns `transactionLink` for the chain it actually used. Deriving the url
# here instead means guessing at an explorer, and a wrong explorer is a link that opens on
# a page showing nothing — worse than no link, because it looks like proof.


def test_the_link_keeperhub_returned_is_the_one_kept(monkeypatch):
    _live(
        monkeypatch,
        FakeResponse(
            202,
            {
                "executionId": "direct_1",
                "status": "completed",
                "transactionHash": TX,
                "transactionLink": "https://sepolia.basescan.org/tx/" + TX,
            },
        ),
    )
    assert _execute(memo="m").link == "https://sepolia.basescan.org/tx/" + TX


def test_a_link_for_an_overridden_chain_is_not_second_guessed(monkeypatch):
    """The fallback only knows Base Sepolia. When KeeperHub names the chain it used, that
    answer wins even though this world would never have built that url."""
    monkeypatch.setenv(keeperhub.CHAIN_ID_ENV, "42161")
    _live(
        monkeypatch,
        FakeResponse(
            202,
            {
                "status": "completed",
                "transactionHash": TX,
                "transactionLink": "https://arbiscan.io/tx/" + TX,
            },
        ),
    )
    assert _execute(memo="m").link == "https://arbiscan.io/tx/" + TX


def test_no_link_from_keeperhub_falls_back_to_the_one_chain_we_can_name(monkeypatch):
    _live(monkeypatch, FakeResponse(202, {"status": "completed", "transactionHash": TX}))
    assert _execute(memo="m").link == f"https://sepolia.basescan.org/tx/{TX}"


def test_no_link_and_a_chain_we_cannot_name_offers_nothing(monkeypatch):
    """Empty rather than a guess. The hash is still returned, so the receipt is not lost —
    only the convenience of a click."""
    monkeypatch.setenv(keeperhub.CHAIN_ID_ENV, "42161")
    _live(monkeypatch, FakeResponse(202, {"status": "completed", "transactionHash": TX}))
    executed = _execute(memo="m")
    assert executed.tx_hash == TX
    assert executed.link == ""


# --- a restart must not mint a second transaction ------------------------------------
#
# `seen` lives in memory; ForceDecisionQueue.applied lives on the volume. So a redeploy
# wakes up facing paid decisions it has no memory of having handled. KeeperHub's
# idempotency window covers that for 24 hours and stops covering it after — past the
# window the stored response is gone and the same key executes again, for real.
#
# The guard is therefore structural, not a bet on beating a deadline: a process only
# writes receipts for decisions it watched arrive.

from src.api.server import _run_onchain_receipts  # noqa: E402


class FakeSim:
    def __init__(self, world):
        self.world = world


async def _pump(app, passes):
    """Run the receipt job for a fixed number of passes, then stop it."""
    import src.api.server as server

    original = server.ONCHAIN_INTERVAL_SECONDS
    server.ONCHAIN_INTERVAL_SECONDS = 0
    task = asyncio.create_task(_run_onchain_receipts(app))
    try:
        for _ in range(passes):
            await asyncio.sleep(0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.ONCHAIN_INTERVAL_SECONDS = original


class FakeApp:
    """Only the two attributes the job touches."""

    class _State:
        pass

    def __init__(self, world):
        self.state = FakeApp._State()
        self.state.simulation = FakeSim(world)
        self.state.onchain_receipts = []


def test_a_restart_does_not_reissue_receipts_for_older_decisions(monkeypatch):
    """The live case: a deploy replaced the container while a paid decision was still in
    the queue. Without this the job asks KeeperHub to execute it again."""
    _live(monkeypatch)
    world = _paid_world(applied=[_payment(tick=900), _payment(clan_id=7, tick=950)])
    _clan(world)
    app = FakeApp(world)

    asyncio.run(_pump(app, passes=40))

    assert FakeClient.sent == []
    assert app.state.onchain_receipts == []


def test_a_decision_paid_for_while_running_still_earns_one(monkeypatch):
    """The catch-up must not become a permanent mute."""
    _live(monkeypatch)
    world = _paid_world(applied=[])
    _clan(world)
    app = FakeApp(world)

    import src.api.server as server

    original = server.ONCHAIN_INTERVAL_SECONDS
    server.ONCHAIN_INTERVAL_SECONDS = 0

    async def scenario():
        task = asyncio.create_task(_run_onchain_receipts(app))
        for _ in range(20):
            await asyncio.sleep(0)
        # Somebody pays now, with the job already watching.
        from src.world.components import ForceDecisionQueue

        queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)
        queue.applied.append(_payment())
        for _ in range(60):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(scenario())
    finally:
        server.ONCHAIN_INTERVAL_SECONDS = original

    assert len(FakeClient.sent) == 1
    assert [r["tx"] for r in app.state.onchain_receipts] == [TX]


# --- their audit trail, not ours -----------------------------------------------------
#
# The transfer response says a transaction was sent. KeeperHub is explicit that
# `transactionHash` is self-reported by the write path, while `receipts` on the status
# endpoint is re-fetched from the chain. Serving our reading of the first one to somebody
# who has no reason to trust this server is the trust-me problem one level up.


class FakeGetClient(FakeClient):
    """Same recorder, answering GET as well."""

    async def get(self, url, headers=None):
        FakeClient.sent.append({"url": url, "headers": headers, "method": "GET"})
        if self.raises is not None:
            raise self.raises
        return self.answer


def _status(monkeypatch, answer=None, raises=None):
    monkeypatch.setenv(keeperhub.ENABLED_ENV, "true")
    monkeypatch.setenv(keeperhub.API_KEY_ENV, KEY)
    reply = answer if answer is not None else FakeResponse(
        200, {"receipts": [{"verified": True, "receiptStatus": "success"}]}
    )
    monkeypatch.setattr(
        keeperhub.httpx, "AsyncClient", lambda **kw: FakeGetClient(reply, raises=raises)
    )


def _confirm(execution):
    return asyncio.run(keeperhub.confirm(execution))


def test_the_execution_id_is_kept(monkeypatch):
    """Dropped, there is nothing to ask the audit trail about."""
    _live(monkeypatch)
    assert _execute(memo="m").execution_id == "direct_1"


def test_the_chain_verdict_is_read_from_their_audit_trail(monkeypatch):
    _status(monkeypatch)
    checked = _confirm(keeperhub.Execution(tx_hash=TX, link="", execution_id="direct_1"))
    assert checked.verified is True
    assert checked.receipt_status == "success"


def test_the_status_call_goes_to_the_documented_endpoint(monkeypatch):
    _status(monkeypatch)
    _confirm(keeperhub.Execution(tx_hash=TX, link="", execution_id="direct_9"))
    assert FakeClient.sent[-1]["url"] == (
        "https://app.keeperhub.com/api/execute/direct_9/status"
    )


def test_a_failed_receipt_is_reported_as_such(monkeypatch):
    """`verified: false` is an answer, not an error. A receipt that hid it would be
    claiming more than the chain said."""
    _status(
        monkeypatch,
        FakeResponse(200, {"receipts": [{"verified": False, "receiptStatus": "reverted"}]}),
    )
    checked = _confirm(keeperhub.Execution(tx_hash=TX, link="", execution_id="direct_1"))
    assert checked.verified is False
    assert checked.receipt_status == "reverted"


@pytest.mark.parametrize(
    "answer,raises",
    [
        (FakeResponse(500, {}, "upstream"), None),
        (FakeResponse(200, None, "not json"), None),
        (FakeResponse(200, {"receipts": []}), None),
        (FakeResponse(200, {}), None),
        (None, httpx.ConnectError("down")),
    ],
)
def test_a_check_that_fails_never_costs_us_the_receipt(monkeypatch, answer, raises):
    """The whole point of doing this second: a transaction hash in hand is worth more than
    a verification we could not obtain."""
    _status(monkeypatch, answer, raises=raises)
    original = keeperhub.Execution(tx_hash=TX, link="L", execution_id="direct_1")
    checked = _confirm(original)
    assert checked.tx_hash == TX
    assert checked.link == "L"
    assert checked.verified is None


def test_nothing_is_asked_without_an_execution_id(monkeypatch):
    _status(monkeypatch)
    _confirm(keeperhub.Execution(tx_hash=TX, link=""))
    assert FakeClient.sent == []


def test_a_disabled_world_asks_nothing(monkeypatch):
    _status(monkeypatch)
    monkeypatch.setenv(keeperhub.ENABLED_ENV, "false")
    _confirm(keeperhub.Execution(tx_hash=TX, link="", execution_id="direct_1"))
    assert FakeClient.sent == []


def test_the_key_never_leaves_the_auth_header(monkeypatch):
    _status(monkeypatch)
    _confirm(keeperhub.Execution(tx_hash=TX, link="", execution_id="direct_1"))
    sent = FakeClient.sent[-1]
    assert sent["headers"]["authorization"] == f"Bearer {KEY}"
    assert KEY not in sent["url"]


# --- one url, two audiences ----------------------------------------------------------
#
# /onchain is the url published in the project's own writeup, so it has to answer a person
# who followed a link called "receipts" as well as a client that wants the data. Serving
# json to the first wastes the only thing the endpoint exists to communicate.


def test_a_browser_gets_a_page(tmp_path: pathlib.Path):
    app = create_app(APP_CONFIG, tmp_path / "k.db")
    with TestClient(app) as client:
        response = client.get("/onchain", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "onchain receipts" in response.text


def test_a_client_still_gets_json(tmp_path: pathlib.Path):
    """The published contract. A page that broke this would break every caller."""
    app = create_app(APP_CONFIG, tmp_path / "k.db")
    with TestClient(app) as client:
        response = client.get("/onchain", headers={"accept": "application/json"})

    assert response.status_code == 200
    assert response.json()["receipts"] == []


def test_a_caller_that_asks_for_nothing_gets_json(tmp_path: pathlib.Path):
    """curl sends */*. Defaulting that to html would hand a wall of markup to every script
    that ever read this endpoint."""
    app = create_app(APP_CONFIG, tmp_path / "k.db")
    with TestClient(app) as client:
        response = client.get("/onchain", headers={"accept": "*/*"})

    assert response.json()["chain_id"] == keeperhub.DEFAULT_CHAIN_ID


def test_the_page_never_carries_the_key():
    """It is served to anyone. The api key is read server-side and must not be near it."""
    page = pathlib.Path("src/viewer/onchain.html").read_text()
    assert "kh_" not in page
    assert "KEEPERHUB_API_KEY" not in page
