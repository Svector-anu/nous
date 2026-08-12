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


def test_the_llm_can_be_switched_on_for_a_running_world(monkeypatch):
    """A persisted world restores llm_enabled from its save, so without this a world
    created with the model off could never have it turned on."""
    monkeypatch.setenv("LLM_ENABLED", "true")
    config, changed = apply_chain_env(WorldConfig(agent_count=0))
    assert config.llm_enabled is True
    assert "llm_enabled=True" in changed


def test_the_llm_stays_off_when_nobody_asks(monkeypatch):
    """The $0 default. Unset must never be read as "on"."""
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    config, _ = apply_chain_env(WorldConfig(agent_count=0))
    assert config.llm_enabled is False


def test_the_model_and_provider_can_be_swapped_from_env(monkeypatch):
    """A persisted world is otherwise stuck with the provider it was born with, so a
    dead key could never be swapped for a working one without a new world."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "claude-opus-4-6")
    config, changed = apply_chain_env(WorldConfig(agent_count=0))
    assert config.llm_provider == "anthropic"
    assert config.llm_model == "claude-opus-4-6"
    assert "llm_provider=anthropic" in changed


def test_an_unset_provider_leaves_the_configured_one_alone(monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY_ENV"):
        monkeypatch.delenv(name, raising=False)
    config, changed = apply_chain_env(WorldConfig(agent_count=0))
    assert config.llm_provider == "dgrid"
    assert changed == []


def test_the_spend_cap_can_be_raised(monkeypatch):
    """AdvisorState.calls_made is durable, so the cap is a total for the world's life.
    Raising it is the only way to get more calls out of a world that spent its budget."""
    monkeypatch.setenv("LLM_MAX_CALLS_PER_SESSION", "2000")
    config, changed = apply_chain_env(WorldConfig(agent_count=0))
    assert config.llm_max_calls_per_session == 2000
    assert "llm_max_calls_per_session=2000" in changed


def test_the_spend_cap_can_be_dropped_to_zero(monkeypatch):
    """Zero is a real answer — it stops spending on a running world without a restart."""
    monkeypatch.setenv("LLM_MAX_CALLS_PER_SESSION", "0")
    config, _ = apply_chain_env(WorldConfig(agent_count=0))
    assert config.llm_max_calls_per_session == 0


def test_a_nonsense_spend_cap_is_refused(monkeypatch):
    """A mistake here is somebody's money, so it is ignored rather than guessed at."""
    for bad in ("lots", "-5", ""):
        monkeypatch.setenv("LLM_MAX_CALLS_PER_SESSION", bad)
        config, _ = apply_chain_env(WorldConfig(agent_count=0))
        assert config.llm_max_calls_per_session == 200


def test_the_call_rate_can_be_slowed_without_a_new_world(monkeypatch):
    """The cooldown sets the *rate*, and the rate is what a world costs to run. A
    persisted world keeps the config it was born with, so without this a deployment
    that turned out twenty times more expensive than expected could only be fixed by
    throwing the world away."""
    monkeypatch.setenv("LLM_MIN_TICKS_BETWEEN_CALLS", "6000")
    config, changed = apply_chain_env(WorldConfig(agent_count=0))
    assert config.llm_min_ticks_between_calls == 6000
    assert "llm_min_ticks_between_calls=6000" in changed


def test_a_zero_cooldown_is_refused(monkeypatch):
    """Zero means every clan on every tick — the single most expensive thing this world
    can be told to do, one typo away from a sensible number."""
    for bad in ("0", "-1", "soon", ""):
        monkeypatch.setenv("LLM_MIN_TICKS_BETWEEN_CALLS", bad)
        config, changed = apply_chain_env(WorldConfig(agent_count=0))
        assert config.llm_min_ticks_between_calls == 3000
        assert changed == []


# --- replaying a payment ---------------------------------------------------------
#
# A transaction hash is public on the block explorer by design, so the only thing making
# it worth anything is the server refusing it the second time. On the live world that
# refusal was in memory alone: every redeploy made every past payment spendable again,
# and a proof spent at tick 55604 was honoured again at tick 100426. These cover the two
# ways it leaked.


def _paid_headers(tx: str = TX) -> dict:
    return {"X402-Payment-Verified": "true", "X402-Transaction-Hash": tx}


def test_a_spent_payment_stays_spent_across_a_restart(tmp_path):
    """The durable set has to be *read*, not merely written. It was written from the
    start; nothing consulted it, so a restart forgave every payment ever made."""
    config = WorldConfig(
        agent_count=4, resource_count=8, llm_force_decision_enabled=True, x402_enabled=True
    )
    app = create_app(config, tmp_path / "x402.db")
    with TestClient(app) as client:
        assert client.post("/clans/1/force-decision", headers=_paid_headers()).status_code == 202

        # Exactly what a redeploy does: the process-local set goes, world state stays.
        app.state.x402._spent.clear()

        assert client.post("/clans/1/force-decision", headers=_paid_headers()).status_code == 409


def test_the_payment_is_claimed_before_it_is_verified(tmp_path):
    """Verification is a network round trip. Requests arriving during it used to all pass
    the spent check and all be honoured — one payment bought five clan decisions in a
    single tick that way. Asserting the claim is already durable *while* verify runs is
    what makes those later arrivals lose."""
    config = WorldConfig(
        agent_count=4, resource_count=8, llm_force_decision_enabled=True, x402_enabled=True
    )
    app = create_app(config, tmp_path / "x402.db")
    seen: list[bool] = []

    with TestClient(app) as client:
        world = app.state.simulation.world
        queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)
        real = app.state.x402

        class Watching:
            def is_spent(self, proof):
                return real.is_spent(proof)

            def record_spent(self, proof):
                real.record_spent(proof)

            async def verify(self, proof, price, currency):
                # The window a concurrent request would arrive in.
                seen.append(f"tx:{TX}" in queue.spent)
                return await real.verify(proof, price, currency)

        app.state.x402 = Watching()
        assert client.post("/clans/1/force-decision", headers=_paid_headers()).status_code == 202

    assert seen == [True], "the proof was still unclaimed while the network call ran"


def test_a_refused_payment_does_not_burn_the_hash(tmp_path):
    """Claiming before verifying must not let one bad request permanently spend a hash
    somebody goes on to pay with for real."""
    config = WorldConfig(
        agent_count=4, resource_count=8, llm_force_decision_enabled=True, x402_enabled=True
    )
    app = create_app(config, tmp_path / "x402.db")
    with TestClient(app) as client:
        world = app.state.simulation.world
        queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)

        # No verification header: the proof is presented and refused.
        refused = client.post(
            "/clans/1/force-decision", headers={"X402-Transaction-Hash": TX}
        )
        assert refused.status_code == 402
        assert f"tx:{TX}" not in queue.spent, "a refused proof was left claimed"

        # The same hash, actually paid for this time.
        assert client.post("/clans/1/force-decision", headers=_paid_headers()).status_code == 202


def test_a_receipt_records_what_was_paid(tmp_path):
    """A receipt that cannot say how much was paid leaves the viewer to assume the
    configured price is still the price."""
    config = WorldConfig(
        agent_count=4, resource_count=8, llm_force_decision_enabled=True, x402_enabled=True
    )
    app = create_app(config, tmp_path / "x402.db")
    with TestClient(app) as client:
        assert client.post("/clans/1/force-decision", headers=_paid_headers()).status_code == 202
        world = app.state.simulation.world
        leadership.apply_forced_decisions(world)
        queue = world.get(world.first(ForceDecisionQueue), ForceDecisionQueue)
        receipt = queue.applied[-1]

    assert receipt["amount"] == config.x402_price
    assert receipt["currency"] == config.x402_currency
    assert receipt["proof"] == f"tx:{TX}"


def test_attached_minds_can_be_turned_on_without_a_new_world(monkeypatch):
    """A persisted world keeps the config it was born with, so a feature with no override
    is unreachable on exactly the world it was built for."""
    monkeypatch.setenv("ATTACHED_MINDS_ENABLED", "true")
    config, changed = apply_chain_env(WorldConfig(agent_count=0))
    assert config.attached_minds_enabled is True
    assert "attached_minds_enabled=True" in changed


def test_the_user_agent_limit_can_be_raised_without_a_new_world(monkeypatch):
    """Reaching it refuses every new deploy with a 409, so the one setting that decides
    whether anybody new can join must not need a new world to change."""
    monkeypatch.setenv("MAX_USER_AGENTS", "200")
    config, changed = apply_chain_env(WorldConfig(agent_count=0))
    assert config.max_user_agents == 200
    assert "max_user_agents=200" in changed


def test_a_zero_user_agent_limit_is_refused(monkeypatch):
    """Zero would lock every visitor out of a world that looks fine from the outside."""
    monkeypatch.setenv("MAX_USER_AGENTS", "0")
    config, changed = apply_chain_env(WorldConfig(agent_count=0))
    assert config.max_user_agents == 50
    assert changed == []


# --- two rails -------------------------------------------------------------------
#
# The world charged USDG on Robinhood Chain and the wallets that might pay it hold USDC on
# Base. Accepting both is what turns a discoverable payment endpoint into a payable one —
# and every chain added is a new replay surface, because a transaction hash is unique only
# within a chain.


def _rail(chain_id, asset, recipient, decimals=6, name="Test"):
    from src.chain.settings import Rail

    return Rail(
        chain_id=chain_id, chain_name=name, currency="TST", asset=asset,
        decimals=decimals, recipient=recipient, rpc_url="http://stub",
    )


def test_the_same_hash_on_two_chains_is_two_payments():
    """A transaction hash is unique only *within* a chain. Keyed on the hash alone, one
    spent proof could be presented again against the chain that had not seen it."""
    robinhood = PaymentProof(tx_hash=TX, chain_id=4663)
    base = PaymentProof(tx_hash=TX, chain_id=8453)
    assert robinhood.fingerprint() != base.fingerprint()


def test_a_proof_that_names_no_chain_keeps_its_old_fingerprint():
    """Every payment already recorded in world state has to still match itself."""
    assert PaymentProof(tx_hash=TX).fingerprint() == f"tx:{TX}"


def test_a_payment_verifies_on_the_second_rail(monkeypatch):
    """The whole point: an agent holding the other asset can pay."""
    recipient = "0x" + "c" * 40
    other = "0x" + "d" * 40
    rails = [
        _rail(4663, USDG_MAINNET, recipient),
        _rail(8453, other, recipient, name="Base"),
    ]
    # A receipt that only moves the *second* rail's token.
    rpc = _StubRpc(receipt=_paid_receipt(recipient, 100_000, token=other))
    verifier = ChainVerifier(rpc=rpc, rails=rails)
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is True


def test_naming_a_chain_pins_the_check_to_it():
    """Otherwise a payment made on a cheap chain could be judged against an expensive
    one's price, by presenting it against whichever rail happens to accept it."""
    recipient = "0x" + "c" * 40
    other = "0x" + "d" * 40
    rails = [
        _rail(4663, USDG_MAINNET, recipient),
        _rail(8453, other, recipient, name="Base"),
    ]
    rpc = _StubRpc(receipt=_paid_receipt(recipient, 100_000, token=other))
    verifier = ChainVerifier(rpc=rpc, rails=rails)

    # The receipt only satisfies the Base rail, and the caller says Robinhood.
    named = PaymentProof(tx_hash=TX, chain_id=4663)
    assert asyncio.run(verifier.verify(named, "0.10", "USDG")) is False


def test_a_proof_naming_an_unaccepted_chain_is_refused():
    rails = [_rail(4663, USDG_MAINNET, "0x" + "c" * 40)]
    verifier = ChainVerifier(rpc=_StubRpc(receipt=None), rails=rails)
    proof = PaymentProof(tx_hash=TX, chain_id=999999)
    assert asyncio.run(verifier.verify(proof, "0.10", "USDG")) is False


def test_each_rail_is_priced_in_its_own_decimals():
    """Both assets here use six. Hardcoding that would be a silent mispricing the first
    time one does not."""
    recipient = "0x" + "c" * 40
    token = "0x" + "d" * 40
    # 18 decimals: 0.10 is 10^17 units, so 100000 units is far short.
    rails = [_rail(8453, token, recipient, decimals=18)]
    rpc = _StubRpc(receipt=_paid_receipt(recipient, 100_000, token=token))
    verifier = ChainVerifier(rpc=rpc, rails=rails)
    assert asyncio.run(verifier.verify(PaymentProof(tx_hash=TX), "0.10", "USDG")) is False


def test_a_rail_with_no_recipient_is_never_offered(monkeypatch):
    """A payment option with an empty payTo is a locked door with a painted-on keyhole."""
    from src.chain import settings as chain_settings

    monkeypatch.delenv("X402_RECIPIENT_ADDRESS", raising=False)
    monkeypatch.delenv("BASE_RECIPIENT_ADDRESS", raising=False)
    assert chain_settings.rails() == []


def test_base_can_be_switched_off(monkeypatch):
    from src.chain import settings as chain_settings

    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    monkeypatch.setenv("BASE_ENABLED", "false")
    chains = {rail.chain_id for rail in chain_settings.rails()}
    assert chains == {4663}


def test_the_original_rail_stays_first(monkeypatch):
    """A client that reads only accepts[0] keeps working. Base is added, not substituted."""
    from src.chain import settings as chain_settings

    monkeypatch.setenv("X402_RECIPIENT_ADDRESS", "0x" + "c" * 40)
    monkeypatch.delenv("BASE_ENABLED", raising=False)
    assert chain_settings.rails()[0].chain_id == 4663


def test_funding_can_be_turned_on_without_a_new_world(monkeypatch):
    """A flag with no override is a feature that can never be switched on where it
    matters — the live world keeps the config it was created with."""
    monkeypatch.setenv("WORLD_FUNDING_ENABLED", "true")
    config, changed = apply_chain_env(WorldConfig(agent_count=0))
    assert config.world_funding_enabled is True
    assert "world_funding_enabled=True" in changed


# --- every knob has a way in ------------------------------------------------------


def test_the_cost_of_a_call_can_be_retuned_without_a_new_world(monkeypatch):
    """It is an estimate of what a decision costs, not a reading of the tokens it used,
    so it will be wrong and will need correcting against a real bill. A world keeps the
    config it was born with, so a wrong number that needs a deploy to fix is a wrong
    number that stays."""
    monkeypatch.setenv("LLM_COST_UNITS_PER_CALL", "12000")
    config, changed = apply_chain_env(WorldConfig(agent_count=0))
    assert config.llm_cost_units_per_call == 12000
    assert "llm_cost_units_per_call=12000" in changed


def test_every_setting_that_sets_the_rate_of_spending_is_reachable():
    """Five settings in a row shipped with no way to read them: the flag was added, the
    world was deployed, and the operator's only lever was throwing the world away and
    starting a new one. Each was found in production, one at a time — the rate of calls,
    attached minds, world funding, the agent cap, hut decay.

    So this asserts the property rather than the instances, over the class all five
    belonged to: what the world spends and how fast. Most of the config is simulation
    tuning that can wait for a deploy. These cannot, because the reason to reach for one
    is that the world is being expensive right now and the world is persistent — it keeps
    the config it was born with, so unreachable means unreachable for its whole life.

    A new one is either listed as deliberately fixed, or it is settable on a running
    world.
    """
    import re
    from pathlib import Path

    # Plumbing bounds rather than spending controls: concurrency, retries, and the size of
    # two in-memory logs. Turning any of these down saves nothing.
    FIXED = {
        "llm_max_inflight",
        "llm_max_recovery_attempts",
        "llm_log_limit",
        "llm_only_clan_id",
        "llm_effort",
        "llm_timeout_seconds",
    }

    source = Path("src/api/server.py").read_text()
    reachable = set(re.findall(r'"(\w+)"[,:]\s*"[A-Z0-9_]+"', source))

    spending = {
        name
        for name in WorldConfig.__dataclass_fields__
        if name.startswith("llm_") or "cost_units" in name or name == "x402_price"
    }
    unreachable = sorted(spending - reachable - FIXED)
    assert not unreachable, (
        f"no environment variable can set {unreachable} — add it to the overrides in "
        "src/api/server.py, or to FIXED if it truly cannot change on a running world"
    )
