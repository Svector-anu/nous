"""Putting a clan's decision on a public chain.

A leader deciding to raid is the most consequential thing that happens here, and until now
it left no trace anybody outside could check. This writes a transaction when one happens,
so the decision has a receipt that does not depend on trusting this server's own log.

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
