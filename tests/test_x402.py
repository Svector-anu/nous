"""x402 payment verification and the escrow settlement ledger.

No test here makes a network call or a paid api call. The chain verifier is exercised
against a stub rpc, which is the only way to assert what it does on a reverted
transaction or a wrong recipient without spending money.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from src.api.server import apply_chain_env, create_app
from src.chain import escrow
from src.chain.settings import USDG_MAINNET
from src.chain.x402 import (
    TRANSFER_TOPIC,
    ChainVerifier,
    HeaderVerifier,
    PaymentProof,
    price_units,
    build_verifier,
    transferred_to,
)
from src.world.components import EscrowBook, ForceDecisionQueue
from src.world.config import WorldConfig
from src.world.systems import leadership
from src.world.tick import Simulation, create_world, state_hash

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


def _transfer_log(to: str, units: int, token: str = USDG_MAINNET) -> dict:
    """One ERC-20 Transfer log, shaped the way a node returns it.

    `to` is an indexed parameter, so it arrives as a 32-byte topic with the address in
    the low 20 bytes; the amount is unindexed and arrives in `data`.
    """
    return {
        "address": token,
        "topics": [
            TRANSFER_TOPIC,
            "0x" + "0" * 24 + "a" * 40,          # from — irrelevant to the check
            "0x" + "0" * 24 + to.removeprefix("0x"),
        ],
        "data": hex(units),
    }


def _paid_receipt(to: str, units: int, token: str = USDG_MAINNET) -> dict:
    """A successful USDG payment. `to` on the receipt is the *token contract*, which is
    what a real token transfer looks like — the recipient appears only in the logs."""
    return {"status": "0x1", "to": token, "logs": [_transfer_log(to, units, token)]}


def test_the_chain_verifier_accepts_a_successful_transfer(monkeypatch):
    recipient = "0x" + "c" * 40
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", recipient)
    # 0.10 USDG at 6 decimals = 100000 units.
    rpc = _StubRpc(receipt=_paid_receipt(recipient, 100_000))
    verifier = ChainVerifier(rpc=rpc)

    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is True
    assert rpc.asked == [TX]


def test_a_payment_smaller_than_the_price_is_refused(monkeypatch):
    """The hole this closes: the verifier used to accept any successful transfer to the
    recipient, so one unit of USDG bought a decision priced at 0.10."""
    recipient = "0x" + "c" * 40
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", recipient)
    verifier = ChainVerifier(rpc=_StubRpc(receipt=_paid_receipt(recipient, 1)))
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is False


def test_paying_more_than_asked_is_still_a_payment(monkeypatch):
    recipient = "0x" + "c" * 40
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", recipient)
    verifier = ChainVerifier(rpc=_StubRpc(receipt=_paid_receipt(recipient, 250_000)))
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is True


def test_transfers_in_one_transaction_add_up(monkeypatch):
    """A payment split across two transfers is still the full amount."""
    recipient = "0x" + "c" * 40
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", recipient)
    receipt = {
        "status": "0x1",
        "to": USDG_MAINNET,
        "logs": [_transfer_log(recipient, 60_000), _transfer_log(recipient, 40_000)],
    }
    verifier = ChainVerifier(rpc=_StubRpc(receipt=receipt))
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is True


def test_a_transfer_of_some_other_token_does_not_count(monkeypatch):
    """Anyone can mint a worthless token and send a lot of it. Only USDG pays."""
    recipient = "0x" + "c" * 40
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", recipient)
    fake = "0x" + "e" * 40
    verifier = ChainVerifier(rpc=_StubRpc(receipt=_paid_receipt(recipient, 10**12, token=fake)))
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is False


def test_a_transfer_to_somebody_else_does_not_count(monkeypatch):
    """A real USDG transfer in the same transaction, but not to us."""
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    verifier = ChainVerifier(rpc=_StubRpc(receipt=_paid_receipt("0x" + "d" * 40, 100_000)))
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is False


def test_without_a_token_contract_the_amount_cannot_be_checked_so_it_is_refused(monkeypatch):
    """Paxos publishes no testnet USDG, so a testnet verifier has no contract to read.
    Refusing beats accepting a payment of unknown size."""
    recipient = "0x" + "c" * 40
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", recipient)
    monkeypatch.delenv("USDG_CONTRACT_ADDRESS", raising=False)
    verifier = ChainVerifier(rpc=_StubRpc(receipt=_paid_receipt(recipient, 100_000)), testnet=True)
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is False


def test_a_testnet_token_can_be_named_in_the_environment(monkeypatch):
    recipient = "0x" + "c" * 40
    token = "0x" + "9" * 40
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", recipient)
    monkeypatch.setenv("USDG_CONTRACT_ADDRESS", token)
    receipt = _paid_receipt(recipient, 100_000, token=token)
    verifier = ChainVerifier(rpc=_StubRpc(receipt=receipt), testnet=True)
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is True


def test_the_price_converts_without_float_error(monkeypatch):
    """0.10 has no exact binary form. Rounding it down by one unit would let every
    payment underpay by a hair and still pass."""
    assert price_units("0.10", 6) == 100_000
    assert price_units("1", 6) == 1_000_000
    assert price_units("0.000001", 6) == 1
    assert price_units("not a price", 6) == -1


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


def test_the_402_tells_a_wallet_how_to_pay(tmp_path, monkeypatch):
    """A price alone is not actionable. Without the recipient, the token contract and
    the chain id, a wallet cannot build the transfer and the 402 is a locked door with
    no keyhole."""
    recipient = "0x" + "c" * 40
    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", recipient)
    config = WorldConfig(
        agent_count=4, resource_count=8, llm_force_decision_enabled=True, x402_enabled=True
    )
    with TestClient(create_app(config, tmp_path / "x402.db")) as client:
        payment = client.post("/clans/1/force-decision").json()["detail"]["payment"]

        assert payment["recipient"] == recipient
        assert payment["token"] == USDG_MAINNET
        assert payment["chain_id"] == 4663
        assert payment["decimals"] == 6
        # The amount a wallet actually passes to transfer(): 0.10 USDG at 6 decimals.
        assert payment["amount_units"] == "100000"
        assert payment["payable"] is True


def test_the_402_says_so_when_payment_is_not_configured(tmp_path, monkeypatch):
    """An operator who enabled x402 without naming a recipient gets `payable: false`,
    so the viewer explains itself instead of offering a button that cannot work."""
    monkeypatch.delenv("X402_RECIPIENT_ADDRESS", raising=False)
    config = WorldConfig(
        agent_count=4, resource_count=8, llm_force_decision_enabled=True, x402_enabled=True
    )
    with TestClient(create_app(config, tmp_path / "x402.db")) as client:
        payment = client.post("/clans/1/force-decision").json()["detail"]["payment"]
        assert payment["recipient"] == ""
        assert payment["payable"] is False


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


# --- turning payments on without starting a new world -------------------------
#
# A resumed world restores its config from the save (sqlite_store: WorldConfig(**meta)),
# so editing config.py does nothing to a world that already exists. These flags are the
# only way to change a running deployment.


def test_env_turns_payments_on(monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "true")
    monkeypatch.setenv("CHAIN_IDENTITY_ENABLED", "1")
    config = WorldConfig(agent_count=0)
    assert config.x402_enabled is False

    config, changed = apply_chain_env(config)
    assert config.x402_enabled is True
    assert config.chain_identity_enabled is True
    assert "x402_enabled=True" in changed


def test_an_unset_flag_is_left_alone(monkeypatch):
    """Unset means "leave it", not "false". An operator who sets only X402_ENABLED must
    not silently switch identity off."""
    for name in ("X402_ENABLED", "CHAIN_IDENTITY_ENABLED", "REAL_MONEY_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    config = WorldConfig(agent_count=0, chain_identity_enabled=True)

    config, changed = apply_chain_env(config)
    assert changed == []
    assert config.chain_identity_enabled is True


def test_a_flag_can_be_turned_back_off(monkeypatch):
    monkeypatch.setenv("X402_ENABLED", "false")
    config, _ = apply_chain_env(WorldConfig(agent_count=0, x402_enabled=True))
    assert config.x402_enabled is False


def test_an_unparseable_flag_changes_nothing(monkeypatch):
    """Guessing here turns real payments on or off. It refuses to guess."""
    monkeypatch.setenv("REAL_MONEY_ENABLED", "maybe")
    config, changed = apply_chain_env(WorldConfig(agent_count=0))
    assert changed == []
    assert config.real_money_enabled is False


def test_the_verifier_can_be_switched_to_chain(monkeypatch):
    monkeypatch.setenv("X402_VERIFIER", "chain")
    config, _ = apply_chain_env(WorldConfig(agent_count=0))
    assert config.x402_verifier == "chain"


def test_a_nonsense_verifier_is_ignored(monkeypatch):
    monkeypatch.setenv("X402_VERIFIER", "trustmebro")
    config, _ = apply_chain_env(WorldConfig(agent_count=0))
    assert config.x402_verifier == "header"


# --- a paid decision actually does something ---------------------------------
#
# The hole this closes: the api verified payment and queued the request, and no system
# ever read the queue. Real money bought a no-op.


def _clan_world():
    """A world with at least one clan that has settled on a goal."""
    world = create_world(WorldConfig(seed=7, agent_count=24, resource_count=40))
    simulation = Simulation(world)
    simulation.run(120)
    return world, simulation


def _a_clan(world):
    from src.world.components import Clan

    for entity in world.query(Clan):
        clan = world.get(entity, Clan)
        if clan.members:
            return clan
    return None


def test_a_forced_decision_makes_a_clan_reconsider_now(monkeypatch):
    """Paying resets the clan's review clock, so the rules choose a goal on this tick
    rather than whenever its turn came round."""
    world, simulation = _clan_world()
    clan = _a_clan(world)
    assert clan is not None
    settled_at = clan.goal_set_tick
    assert settled_at > 0, "clan should have decided at least once by tick 120"

    queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)
    queue.pending.append({"clan_id": clan.clan_id, "tick": world.tick})

    simulation.step()

    assert queue.pending == [], "the queue must be drained, not left to grow"
    assert clan.goal_set_tick > settled_at, "the clan should have decided again"


def test_a_forced_decision_works_without_an_llm():
    """The whole point: the rules are the floor, so somebody who paid gets a real
    decision on a world with no advisor configured and no api cost."""
    world, simulation = _clan_world()
    assert world.config.llm_enabled is False
    clan = _a_clan(world)
    before = clan.goal_set_tick

    queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)
    queue.pending.append({"clan_id": clan.clan_id, "tick": world.tick})
    assert leadership.apply_forced_decisions(world) == [clan.clan_id]
    assert clan.goal_set_tick == 0, "marked due"

    simulation.step()
    assert clan.goal_set_tick > before


def test_forcing_a_clan_that_does_not_exist_changes_nothing():
    world, simulation = _clan_world()
    goals = {}
    from src.world.components import Clan

    for entity in world.query(Clan):
        c = world.get(entity, Clan)
        goals[c.clan_id] = c.goal_set_tick

    queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)
    queue.pending.append({"clan_id": 99999, "tick": world.tick})
    forced = leadership.apply_forced_decisions(world)

    assert forced == []
    for entity in world.query(Clan):
        c = world.get(entity, Clan)
        assert c.goal_set_tick == goals[c.clan_id]


def test_the_queue_is_drained_even_when_nothing_matches():
    """Otherwise a request for a dead clan sits in durable state for ever."""
    world, _ = _clan_world()
    queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)
    queue.pending.append({"clan_id": 99999, "tick": world.tick})
    leadership.apply_forced_decisions(world)
    assert queue.pending == []


def test_forced_decisions_do_not_change_a_world_nobody_paid_for():
    """Determinism: the drain must be a no-op when the queue is empty, or every world
    would diverge from its replay."""
    def run(force: bool) -> str:
        world = create_world(WorldConfig(seed=11, agent_count=16, resource_count=30))
        simulation = Simulation(world)
        simulation.run(60)
        if force:
            leadership.apply_forced_decisions(world)  # empty queue
        simulation.run(20)
        return state_hash(world)

    assert run(True) == run(False)
