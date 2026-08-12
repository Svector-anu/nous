"""Sign-In With Ethereum: prove an address without ever holding a key.

The server issues a nonce, the wallet signs a message containing it, and the server
recovers the signing address from the signature. No private key, no seed phrase and no
custody ever touches this process.

`eth_account` is an *optional* dependency, imported lazily and only when a verification
is actually attempted — the simulation runs at $0 without any of this, and an operator
who never enables identity should not have to install a crypto stack. When it is missing,
verification fails closed with a clear reason rather than passing anything through.

Nonces live in memory on purpose. A restart invalidates outstanding challenges, which
costs a user one extra click and closes the replay window rather than widening it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger("neociv")

# A challenge is short-lived: long enough to sign in a wallet popup, short enough that a
# leaked message is worthless by the time anyone finds it.
NONCE_TTL_SECONDS = 600
# Bounded like every other accumulator here.
MAX_NONCES = 512

_ADDRESS_LENGTH = 42


def is_address(value: str) -> bool:
    """Shape check only — this says nothing about whether the account exists."""
    text = (value or "").strip()
    if len(text) != _ADDRESS_LENGTH or not text.startswith("0x"):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in text[2:])


def normalise(address: str) -> str:
    return (address or "").strip().lower()


@dataclass
class Challenge:
    nonce: str
    issued_at: float
    address: str = ""


class NonceStore:
    """Issued-but-unspent challenges. In memory, bounded, single-use."""

    def __init__(self, ttl: float = NONCE_TTL_SECONDS, limit: int = MAX_NONCES) -> None:
        self.ttl = ttl
        self.limit = limit
        self._issued: dict[str, Challenge] = {}

    def _prune(self, now: float) -> None:
        stale = [n for n, c in self._issued.items() if now - c.issued_at > self.ttl]
        for nonce in stale:
            self._issued.pop(nonce, None)
        # Oldest first if we are still over the cap. dict preserves insertion order.
        while len(self._issued) > self.limit:
            self._issued.pop(next(iter(self._issued)), None)

    def issue(self, address: str = "", now: float | None = None) -> Challenge:
        moment = time.time() if now is None else now
        nonce = hashlib.blake2b(os.urandom(32), digest_size=12).hexdigest()
        challenge = Challenge(nonce=nonce, issued_at=moment, address=normalise(address))
        self._issued[nonce] = challenge
        self._prune(moment)
        return challenge

    def consume(self, nonce: str, now: float | None = None) -> Challenge | None:
        """Single use: a nonce that verifies once can never verify again."""
        moment = time.time() if now is None else now
        challenge = self._issued.pop(nonce, None)
        if challenge is None:
            return None
        if moment - challenge.issued_at > self.ttl:
            return None
        return challenge

    def __len__(self) -> int:
        return len(self._issued)


def build_message(
    *,
    domain: str,
    address: str,
    nonce: str,
    chain_id: int,
    statement: str = "Link this wallet to your Nous agent. This grants no in-world advantage.",
    uri: str = "",
    issued_at: str = "",
) -> str:
    """An EIP-4361 message. The statement is deliberately explicit about what linking
    does *not* buy, because the wallet popup is the one place a user reads carefully.

    `Issued At` is required by the spec, not optional. A wallet parses this text: metamask
    recognises a sign-in request and renders it as one, and a message that looks like
    EIP-4361 but fails the parse is treated as suspicious rather than shown plainly. So
    omitting a required field does not produce a plainer prompt — it produces a rejected
    one, which reaches the app as error 4001 and reads as "the user cancelled".
    """
    target = uri or f"https://{domain}"
    when = issued_at or (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    return "\n".join(
        [
            f"{domain} wants you to sign in with your Ethereum account:",
            address,
            "",
            statement,
            "",
            f"URI: {target}",
            "Version: 1",
            f"Chain ID: {chain_id}",
            f"Nonce: {nonce}",
            f"Issued At: {when}",
        ]
    )


def recover_address(message: str, signature: str) -> str:
    """Address that produced this signature, or "" if it cannot be determined.

    Contained the same way advisor calls are: a malformed signature, a missing library
    or a third-party exception must all resolve to "no identity", never to a crash and
    never to a pass.
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError:
        logger.warning(
            "eth_account is not installed; wallet verification is unavailable. "
            "pip install eth-account to enable it."
        )
        return ""

    try:
        encoded = encode_defunct(text=message)
        return normalise(Account.recover_message(encoded, signature=signature))
    except Exception as error:  # noqa: BLE001 - a bad signature is a normal outcome
        logger.info("signature did not recover: %r", error)
        return ""


def verify(message: str, signature: str, expected: str) -> bool:
    recovered = recover_address(message, signature)
    return bool(recovered) and recovered == normalise(expected)


def available() -> bool:
    """Whether signature verification can actually run in this process."""
    try:
        import eth_account  # noqa: F401
    except ImportError:
        return False
    return True
