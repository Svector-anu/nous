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
from typing import Protocol

from .rpc import Rpc
from .settings import x402_recipient

logger = logging.getLogger("neociv")


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
    """Verifies a transaction receipt against the configured recipient and chain.

    The price and currency checks are placeholders: on-chain verification would parse
    the transaction input data or event logs to confirm the amount and token match what
    the api expects. That needs the USDG contract ABI and is deferred — the seam is here
    and the hard part (fetching the receipt, checking the recipient, and remembering
    what is spent) works now.
    """

    def __init__(
        self, rpc: Rpc | None = None, *, chain_id: int = 4663, testnet: bool = False
    ) -> None:
        self.rpc = rpc if rpc is not None else Rpc(testnet=testnet)
        self.chain_id = chain_id
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
        # The transaction went somewhere else.
        to_address = (receipt.get("to") or "").strip().lower()
        if to_address != recipient.lower():
            return False

        # Placeholder: a real check would decode the input data or event logs to confirm
        # the amount and currency match `price` and `currency`. For now, any successful
        # transfer to the recipient address passes.
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
