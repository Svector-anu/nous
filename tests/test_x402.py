"""x402 payment verification and the escrow settlement ledger.

No test here makes a network call or a paid api call. The chain verifier is exercised
against a stub rpc, which is the only way to assert what it does on a reverted
transaction or a wrong recipient without spending money.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from src.api.server import create_app
from src.chain import escrow
from src.chain.x402 import ChainVerifier, HeaderVerifier, PaymentProof, build_verifier
from src.world.components import EscrowBook, ForceDecisionQueue
from src.world.config import WorldConfig
from src.world.tick import create_world

TX = "0x" + "a" * 64


# --- proofs ------------------------------------------------------------------


def test_a_proof_fingerprints_by_transaction_first():
    proof = PaymentProof(tx_hash=TX, header_value="true")
    assert proof.fingerprint() == f"tx:{TX}"


def test_fingerprints_ignore_case():
    """The same transaction in two casings is one payment, not two."""
    upper = PaymentProof(tx_hash="0x" + "A" * 64)
    lower = PaymentProof(tx_hash="0x" + "a" * 64)
    assert upper.fingerprint() == lower.fingerprint()


def test_an_empty_proof_has_no_fingerprint():
    assert PaymentProof().fingerprint() == ""


# --- the header verifier -----------------------------------------------------


def test_the_header_verifier_accepts_a_verified_header():
    verifier = HeaderVerifier()
    proof = PaymentProof(tx_hash=TX, header_value="true")
    assert asyncio.run(verifier.verify(proof, "0.10", "USDG")) is True


def test_the_header_verifier_refuses_a_missing_header():
    verifier = HeaderVerifier()
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is False


def test_the_header_verifier_refuses_a_header_that_says_false():
    verifier = HeaderVerifier()
    proof = PaymentProof(tx_hash=TX, header_value="false")
    assert asyncio.run(verifier.verify(proof, "0.10", "USDG")) is False


def test_a_proof_cannot_be_spent_twice():
    """The replay guard. Without it one payment buys unlimited decisions."""
    verifier = HeaderVerifier()
    proof = PaymentProof(tx_hash=TX, header_value="true")

    assert asyncio.run(verifier.verify(proof, "0.10", "USDG")) is True
    verifier.record_spent(proof)
    assert verifier.is_spent(proof) is True
    assert asyncio.run(verifier.verify(proof, "0.10", "USDG")) is False


def test_a_different_proof_still_works_after_one_is_spent():
    verifier = HeaderVerifier()
    first = PaymentProof(tx_hash=TX, header_value="true")
    second = PaymentProof(tx_hash="0x" + "b" * 64, header_value="true")

    asyncio.run(verifier.verify(first, "0.10", "USDG"))
    verifier.record_spent(first)
    assert asyncio.run(verifier.verify(second, "0.10", "USDG")) is True


# --- the chain verifier ------------------------------------------------------


class _StubRpc:
    """Stands in for the node. Records what was asked so a test can prove the verifier
    actually looked, rather than guessing from a return value."""

    def __init__(self, receipt: dict | None = None, error: Exception | None = None) -> None:
        self.receipt = receipt
        self.error = error
        self.asked: list[str] = []

    async def transaction_receipt(self, tx_hash: str) -> dict | None:
        self.asked.append(tx_hash)
        if self.error is not None:
            raise self.error
        return self.receipt


def test_the_chain_verifier_refuses_without_a_recipient(monkeypatch):
    """No configured recipient means "paid to whom?" — it must deny, not pass."""
    monkeypatch.delenv("X402_RECIPIENT_ADDRESS", raising=False)
    verifier = ChainVerifier(rpc=_StubRpc(receipt={"status": "0x1", "to": "0xabc"}))
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is False


def test_the_chain_verifier_accepts_a_successful_transfer(monkeypatch):
    recipient = "0x" + "c" * 40
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", recipient)
    rpc = _StubRpc(receipt={"status": "0x1", "to": recipient})
    verifier = ChainVerifier(rpc=rpc)

    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is True
    assert rpc.asked == [TX]


def test_the_chain_verifier_refuses_a_reverted_transaction(monkeypatch):
    """status 0x0 is a transaction that ran and failed. Money did not move."""
    recipient = "0x" + "c" * 40
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", recipient)
    verifier = ChainVerifier(rpc=_StubRpc(receipt={"status": "0x0", "to": recipient}))
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is False


def test_the_chain_verifier_refuses_a_payment_to_someone_else(monkeypatch):
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    verifier = ChainVerifier(rpc=_StubRpc(receipt={"status": "0x1", "to": "0x" + "d" * 40}))
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is False


def test_the_chain_verifier_refuses_an_unknown_transaction(monkeypatch):
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    verifier = ChainVerifier(rpc=_StubRpc(receipt=None))
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is False


def test_a_node_failure_denies_rather_than_crashing(monkeypatch):
    """Contained at the boundary, like every other outside-the-sim call here. A denial
    is recoverable; granting a payment that never happened is not."""
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    verifier = ChainVerifier(rpc=_StubRpc(error=RuntimeError("node is down")))
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is False


def test_the_factory_defaults_to_the_header_verifier():
    assert isinstance(build_verifier("header"), HeaderVerifier)
    assert isinstance(build_verifier("chain"), ChainVerifier)
    assert isinstance(build_verifier("nonsense"), HeaderVerifier)


# --- the api route -----------------------------------------------------------


def _client(tmp_path, **overrides):
    config = WorldConfig(agent_count=4, resource_count=8, **overrides)
    return TestClient(create_app(config, tmp_path / "x402.db"))


def test_force_decision_is_refused_while_the_feature_is_off(tmp_path):
    with _client(tmp_path) as client:
        assert client.post("/clans/1/force-decision").status_code == 403


def test_force_decision_is_refused_while_x402_is_off(tmp_path):
    """Both flags have to be on. `llm_force_decision_enabled` alone must not open a
    payment path that nothing is configured to verify."""
    with _client(tmp_path, llm_force_decision_enabled=True) as client:
        assert client.post("/clans/1/force-decision").status_code == 403


def test_an_unpaid_request_gets_a_402_and_changes_nothing(tmp_path):
    config = WorldConfig(
        agent_count=4, resource_count=8, llm_force_decision_enabled=True, x402_enabled=True
    )
    app = create_app(config, tmp_path / "x402.db")
    with TestClient(app) as client:
        response = client.post("/clans/1/force-decision")
        assert response.status_code == 402
        assert response.headers.get("X402-Payment-Required") == "true"
        assert response.headers.get("X402-Currency") == "USDG"
        assert response.json()["detail"]["payment"]["scheme"] == "x402"

        # The point of the 402: nothing was queued.
        world = app.state.simulation.world
        queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)
        assert queue.pending == []


def test_a_paid_request_is_queued(tmp_path):
    config = WorldConfig(
        agent_count=4, resource_count=8, llm_force_decision_enabled=True, x402_enabled=True
    )
    app = create_app(config, tmp_path / "x402.db")
    with TestClient(app) as client:
        response = client.post(
            "/clans/1/force-decision",
            headers={"X402-Payment-Verified": "true", "X402-Transaction-Hash": TX},
        )
        assert response.status_code == 202
        assert response.json()["queued"] is True


def test_the_same_payment_cannot_buy_two_decisions(tmp_path):
    """End to end replay guard, through the route rather than the verifier alone."""
    config = WorldConfig(
        agent_count=4, resource_count=8, llm_force_decision_enabled=True, x402_enabled=True
    )
    app = create_app(config, tmp_path / "x402.db")
    with TestClient(app) as client:
        headers = {"X402-Payment-Verified": "true", "X402-Transaction-Hash": TX}
        assert client.post("/clans/1/force-decision", headers=headers).status_code == 202
        assert client.post("/clans/1/force-decision", headers=headers).status_code == 409


def test_a_verified_payment_is_recorded_in_durable_world_state(tmp_path):
    """The spent set has to be world state, not memory: an in-memory guard resets on
    restart, and a crash loop would let one payment through repeatedly."""
    config = WorldConfig(
        agent_count=4, resource_count=8, llm_force_decision_enabled=True, x402_enabled=True
    )
    app = create_app(config, tmp_path / "x402.db")
    with TestClient(app) as client:
        client.post(
            "/clans/1/force-decision",
            headers={"X402-Payment-Verified": "true", "X402-Transaction-Hash": TX},
        )
        world = app.state.simulation.world
        queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)
        assert f"tx:{TX}" in queue.spent


def test_the_spent_set_is_bounded(tmp_path):
    config = WorldConfig(
        agent_count=4,
        resource_count=8,
        llm_force_decision_enabled=True,
        x402_enabled=True,
        x402_spent_limit=3,
    )
    app = create_app(config, tmp_path / "x402.db")
    with TestClient(app) as client:
        for index in range(6):
            client.post(
                "/clans/1/force-decision",
                headers={
                    "X402-Payment-Verified": "true",
                    "X402-Transaction-Hash": f"0x{index:064x}",
                },
            )
        world = app.state.simulation.world
        queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)
        assert len(queue.spent) == 3


# --- escrow ------------------------------------------------------------------


def test_a_world_starts_with_an_escrow_book():
    assert create_world(WorldConfig(seed=1, agent_count=0)).first(EscrowBook) is not None


def test_escrow_is_inert_while_real_money_is_off():
    """The default. Nothing here may do anything until an operator turns it on."""
    world = create_world(WorldConfig(seed=1, agent_count=0))
    assert escrow.enabled(world) is False
    assert escrow.record_payouts(world, 1) == []
    assert escrow.mark_paid(world, 1, TX) is False
    with pytest.raises(ValueError):
        escrow.deposit(world, "0xabc", 100)


def test_a_deposit_is_recorded_when_real_money_is_on():
    world = create_world(WorldConfig(seed=1, agent_count=0, real_money_enabled=True))
    assert escrow.deposit(world, "0xABC", 500) == 500
    assert escrow.deposit(world, "0xabc", 250) == 750
    assert escrow.balance(world, "0xABC") == 750


def test_a_deposit_must_be_positive():
    world = create_world(WorldConfig(seed=1, agent_count=0, real_money_enabled=True))
    with pytest.raises(ValueError):
        escrow.deposit(world, "0xabc", 0)
    with pytest.raises(ValueError):
        escrow.deposit(world, "0xabc", -5)


def test_a_deposit_needs_an_address():
    world = create_world(WorldConfig(seed=1, agent_count=0, real_money_enabled=True))
    with pytest.raises(ValueError):
        escrow.deposit(world, "   ", 100)


def test_the_demo_credit_book_and_the_escrow_book_never_share_a_balance():
    """The claim the market card makes to the user. Demo credits are not money."""
    from src.world.systems import markets

    world = create_world(WorldConfig(seed=1, agent_count=0, real_money_enabled=True))
    escrow.deposit(world, "0xabc", 500)

    book = markets.book(world)
    assert book is not None
    assert "0xabc" not in book.balances
    assert escrow.balance(world, "0xabc") == 500
