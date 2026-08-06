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
from .settings import USDG_DECIMALS, usdg_address, x402_recipient

logger = logging.getLogger("neociv")

# keccak256("Transfer(address,address,uint256)") — the topic every ERC-20 transfer logs.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _units(price: str, decimals: int) -> int:
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

    def __init__(self, tx_hash: str = "", header_value: str = "") -> None:
        self.tx_hash = tx_hash.strip()
        self.header_value = header_value.strip()

    def fingerprint(self) -> str:
        """A stable string the spent set can test against. Lowercase so the same
        transaction with different casing is recognised as one payment."""
        if self.tx_hash:
            return f"tx:{self.tx_hash.lower()}"
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
        self, rpc: Rpc | None = None, *, chain_id: int = 4663, testnet: bool = False
    ) -> None:
        self.rpc = rpc if rpc is not None else Rpc(testnet=testnet)
        self.chain_id = chain_id
        self.testnet = testnet
        self._spent: set[str] = set()

    async def verify(self, proof: PaymentProof, price: str, currency: str) -> bool:
        if not proof.tx_hash:
            return False
        fingerprint = proof.fingerprint()
        if not fingerprint or self.is_spent(proof):
            return False

        recipient = x402_recipient()
        if not recipient:
            logger.warning(
                "X402_RECIPIENT_ADDRESS is not set; chain verifier cannot work"
            )
            return False

        try:
            receipt = await self.rpc.transaction_receipt(proof.tx_hash)
        except Exception as error:  # noqa: BLE001 - a network failure is a normal denial
            logger.info("could not fetch receipt for %s: %r", proof.tx_hash, error)
            return False

        if receipt is None:
            return False
        # A receipt exists but status != 1 means the transaction reverted.
        if receipt.get("status") != "0x1":
            return False

        token = usdg_address(self.testnet)
        if not token:
            # Refusing is the point. Without a token contract the amount cannot be read,
            # and a payment of unknown size is not a payment — the previous behaviour
            # accepted one wei as readily as the asking price.
            logger.warning(
                "no USDG contract configured for chain %s; set USDG_CONTRACT_ADDRESS",
                self.chain_id,
            )
            return False

        required = _units(price, USDG_DECIMALS)
        if required <= 0:
            logger.warning("x402 price %r is not a usable amount", price)
            return False

        paid = transferred_to(receipt, token, recipient)
        if paid < required:
            # Logged at info: underpaying is a normal client error, not a fault here.
            logger.info(
                "x402 payment %s short: %d of %d units to %s", proof.tx_hash, paid, required, recipient
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
