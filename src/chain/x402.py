"""x402 payment verification: prove a payment happened without holding value.

Two verifier implementations:

1. **header** — trusts an upstream proxy's verdict, read from `X402-Payment-Verified`.
   The existing seam. Useful in tests, behind a gateway that settles off-chain, or when
   the operator verifies batch and marks requests verified before they reach the sim.

2. **chain** — fetches a transaction receipt from the rpc and checks it against the
   configured recipient address. A verified payment is remembered in `ForceDecisionQueue.spent`
   so it cannot be replayed: the same tx hash buys one decision, not two.

The protocol is abstract so an operator can plug in whatever payment rail they run
without changing the world or the api routes.
"""

from __future__ import annotations

import logging
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Protocol

from .rpc import Rpc
from . import settings as chain_settings
from .settings import USDG_DECIMALS, usdg_address, x402_recipient

logger = logging.getLogger("neociv")

# keccak256("Transfer(address,address,uint256)") — the topic every ERC-20 transfer logs.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def price_units(price: str, decimals: int) -> int:
    """Turn a human price like "0.10" into integer token units.

    Decimal rather than float: 0.10 has no exact binary representation, and rounding a
    payment threshold down by one unit is the kind of bug that only shows up in an
    argument with a user.
    """
    try:
        return int((Decimal(str(price)) * (10**decimals)).to_integral_value(ROUND_FLOOR))
    except (InvalidOperation, ValueError):
        return -1


def _topic_address(topic: str) -> str:
    """Event topics are 32 bytes; an address is the low 20. Compare like with like."""
    cleaned = (topic or "").lower().removeprefix("0x")
    return "0x" + cleaned[-40:] if len(cleaned) >= 40 else ""


def transferred_to(receipt: dict, token: str, recipient: str) -> int:
    """Total tokens moved to `recipient` by `token` in this transaction.

    Reads the receipt's own logs rather than the transaction's `to` field, because a
    token payment is a call to the *contract* — the recipient never appears as `to`.
    Sums rather than taking the first match: one transaction may split a payment across
    several transfers, and each of them counts.
    """
    if not token or not recipient:
        return 0
    token = token.lower()
    recipient = recipient.lower()
    total = 0
    for log in receipt.get("logs") or []:
        if (log.get("address") or "").lower() != token:
            continue
        topics = log.get("topics") or []
        # topics: [event, from, to]. A malformed log is skipped, never guessed at.
        if len(topics) < 3 or (topics[0] or "").lower() != TRANSFER_TOPIC:
            continue
        if _topic_address(topics[2]) != recipient:
            continue
        try:
            total += int(log.get("data") or "0x0", 16)
        except ValueError:
            continue
    return total


class PaymentProof:
    """Evidence a payment happened. What counts as proof depends on the verifier."""

    def __init__(self, tx_hash: str = "", header_value: str = "", chain_id: int = 0) -> None:
        self.tx_hash = tx_hash.strip()
        self.header_value = header_value.strip()
        # Which chain the caller says it paid on. Zero means "did not say", which is how
        # every client behaved before there was more than one, and must keep working.
        self.chain_id = int(chain_id or 0)

    def fingerprint(self) -> str:
        """A stable string the spent set can test against. Lowercase so the same
        transaction with different casing is recognised as one payment.

        The chain is part of it. A transaction hash is unique only *within* a chain —
        nothing stops the same 32 bytes existing on two — so the moment a second rail is
        accepted, one spent hash could be presented again against the chain that had not
        seen it. Keying on both closes that before it can be reached.

        A proof naming no chain keeps the old, unprefixed form, so every payment already
        recorded in world state still matches itself.
        """
        if self.tx_hash:
            body = f"tx:{self.tx_hash.lower()}"
            return f"{self.chain_id}:{body}" if self.chain_id else body
        if self.header_value:
            return f"hdr:{self.header_value.lower()}"
        return ""


class X402Verifier(Protocol):
    """How to tell whether a payment actually happened."""

    async def verify(self, proof: PaymentProof, price: str, currency: str) -> bool:
        """True when the proof is valid and has not been spent. False otherwise, which
        includes a network failure — better to deny a real payment than to grant a fake
        one, because granting is recorded in durable state and a mistake cannot be undone
        by a restart."""
        ...

    def record_spent(self, proof: PaymentProof) -> None:
        """Mark this proof consumed. A verifier that checks the chain still needs to
        remember what it has honoured so the same transaction cannot pay twice."""
        ...

    def is_spent(self, proof: PaymentProof) -> bool:
        """Whether this proof has already been accepted."""
        ...


class HeaderVerifier:
    """Trusts an upstream proxy. The header says "verified" → we believe it.

    Still remembers what has been spent so one downstream request carrying the header
    cannot loop through the api and buy two decisions.
    """

    def __init__(self) -> None:
        self._spent: set[str] = set()

    async def verify(self, proof: PaymentProof, price: str, currency: str) -> bool:
        if not proof.header_value or proof.header_value.lower() != "true":
            return False
        fingerprint = proof.fingerprint()
        if not fingerprint or self.is_spent(proof):
            return False
        return True

    def record_spent(self, proof: PaymentProof) -> None:
        fingerprint = proof.fingerprint()
        if fingerprint:
            self._spent.add(fingerprint)

    def is_spent(self, proof: PaymentProof) -> bool:
        fingerprint = proof.fingerprint()
        return bool(fingerprint) and fingerprint in self._spent


class ChainVerifier:
    """Verifies a transaction receipt against the configured recipient, token and price.

    Checks the receipt's USDG Transfer logs, not the transaction's `to` field. A token
    payment is a call to the token contract, so the recipient never appears as `to` —
    matching on it both rejected genuine USDG payments and accepted any bare transfer
    regardless of size.

    A payment whose amount cannot be established is refused rather than assumed good.
    """

    def __init__(
        self,
        rpc: Rpc | None = None,
        *,
        chain_id: int = 4663,
        testnet: bool = False,
        rails: list | None = None,
    ) -> None:
        # An injected rpc wins outright and is used for every rail. That is how every test
        # here avoids the network, and it has to keep working now there is more than one
        # chain to reach.
        self.rpc = rpc
        self.chain_id = chain_id
        self.testnet = testnet
        self._rails = rails
        self._rpcs: dict[int, Rpc] = {}
        self._spent: set[str] = set()

    def rails(self) -> list:
        """Every way this world will take a payment. Read at call time rather than at
        construction, so adding a rail does not need a restart."""
        if self._rails is not None:
            return self._rails
        return chain_settings.rails(testnet=self.testnet)

    def _rpc_for(self, rail) -> Rpc:
        """One client per chain, made once."""
        if self.rpc is not None:
            return self.rpc
        if rail.chain_id not in self._rpcs:
            self._rpcs[rail.chain_id] = Rpc(rail.rpc_url)
        return self._rpcs[rail.chain_id]

    async def verify(self, proof: PaymentProof, price: str, currency: str) -> bool:
        """True if this transaction paid the asking price on a rail we accept.

        A caller that named its chain is checked against that one only. Trying every
        configured chain for an unnamed proof is deliberate — it keeps old clients
        working — but doing it for a *named* one would let a payment made on a cheap
        chain be judged against an expensive one's price.
        """
        if not proof.tx_hash:
            return False
        fingerprint = proof.fingerprint()
        if not fingerprint or self.is_spent(proof):
            return False

        rails = self.rails()
        if proof.chain_id:
            rails = [rail for rail in rails if rail.chain_id == proof.chain_id]
            if not rails:
                logger.info("x402 proof names chain %s, which is not accepted", proof.chain_id)
                return False
        if not rails:
            # No recipient configured anywhere. Refusing is the point: a payment whose
            # destination cannot be established is not a payment.
            logger.warning("no payment rail is configured; the chain verifier cannot work")
            return False

        for rail in rails:
            if await self._verify_on(rail, proof, price):
                return True
        return False

    async def _verify_on(self, rail, proof: PaymentProof, price: str) -> bool:
        try:
            receipt = await self._rpc_for(rail).transaction_receipt(proof.tx_hash)
        except Exception as error:  # noqa: BLE001 - a network failure is a normal denial
            logger.info("could not fetch %s on %s: %r", proof.tx_hash, rail.chain_name, error)
            return False

        if receipt is None:
            return False
        # A receipt exists but status != 1 means the transaction reverted.
        if receipt.get("status") != "0x1":
            return False

        # Decimals come from the rail, not from a constant. USDG and USDC both happen to
        # use six, and hardcoding that would be a silent mispricing the first time a rail
        # does not.
        required = price_units(price, rail.decimals)
        if required <= 0:
            logger.warning("x402 price %r is not a usable amount", price)
            return False

        paid = transferred_to(receipt, rail.asset, rail.recipient)
        if paid < required:
            # Logged at info: underpaying is a normal client error, not a fault here.
            logger.info(
                "x402 payment %s short on %s: %d of %d units",
                proof.tx_hash,
                rail.chain_name,
                paid,
                required,
            )
            return False
        return True

    def record_spent(self, proof: PaymentProof) -> None:
        fingerprint = proof.fingerprint()
        if fingerprint:
            self._spent.add(fingerprint)

    def is_spent(self, proof: PaymentProof) -> bool:
        fingerprint = proof.fingerprint()
        return bool(fingerprint) and fingerprint in self._spent


def build_verifier(kind: str, *, chain_id: int = 4663, testnet: bool = False) -> X402Verifier:
    """Factory: returns the verifier the config names, or header as the safe default."""
    if kind == "chain":
        return ChainVerifier(chain_id=chain_id, testnet=testnet)
    return HeaderVerifier()
