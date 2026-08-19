"""Execute one onchain action through KeeperHub when somebody pays for a decision.

A visitor can pay this world over x402 to make a clan leader reconsider its goal. That is
the one moment here where real money meets an autonomous decision, and until now it left
no trace anybody outside could check. This puts a transaction on a public chain when it
happens, so the decision has a receipt that does not depend on trusting this server's own
log.

An earlier version fired on raids instead, and was wrong in a way no unit test caught: a
settled world never raids, so it was correct code wired to an event that does not occur.
Whatever earns a receipt has to be something that actually happens *and* stays rare.

Three things constrain the design, all of them from AGENTS.md rather than preference.

**The idle world must cost $0.** So this is off unless `KEEPERHUB_ENABLED` says otherwise,
and when it is off nothing here opens a socket — the gate is checked before the client is
built, not inside the request. The target is a testnet, where the transaction costs
nothing real.

**No network call from the simulation core.** Nothing in this module may be called from a
system. A system runs inside the tick loop, and a round trip there would stall the world
and make its history depend on someone else's uptime. The caller is a background job
beside the loop, reading decisions the world already recorded.

**Fail closed and stay quiet.** Every failure returns `None`. A world that cannot reach
KeeperHub is a world that carries on exactly as it did before this existed, because the
transaction is a receipt for a decision the world already made on its own.

The api key is read from the environment, never stored in world state, and never logged —
it is an organization-wide credential that can move funds.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, replace

import httpx

logger = logging.getLogger("neociv")

BASE_URL = "https://app.keeperhub.com"
TRANSFER_PATH = "/api/execute/transfer"

API_KEY_ENV = "KEEPERHUB_API_KEY"
ENABLED_ENV = "KEEPERHUB_ENABLED"
CHAIN_ID_ENV = "KEEPERHUB_CHAIN_ID"
RECIPIENT_ENV = "KEEPERHUB_RECIPIENT"
AMOUNT_ENV = "KEEPERHUB_AMOUNT"

# Base Sepolia. A testnet on purpose: the point is a verifiable receipt, and a receipt on a
# chain where the funds are worthless proves exactly as much about the decision as one that
# costs real money. Overridable because KeeperHub's own advice is to read GET /api/chains
# and pick a chain whose `isEnabled` and `isTestnet` are both true, which can change under
# us without a deploy here.
DEFAULT_CHAIN_ID = 84532

# Small and fixed. The amount carries no meaning — the transaction is the signal, not its
# size — so there is nothing to gain by making it a variable the world can influence.
DEFAULT_AMOUNT = "0.000001"

# KeeperHub's documented ceiling is 60 requests per minute per key, and a world with
# nineteen clans can resolve several goals in the same tick. This is not a rate limiter; it
# is the timeout that stops a slow gateway from holding a background pass open.
DEFAULT_TIMEOUT = 20.0

_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Execution:
    """What one executed action leaves behind.

    The link is carried rather than derived. KeeperHub returns `transactionLink` for the
    chain it actually used, which is the only source that stays correct when the chain is
    overridden — building the url here means guessing at an explorer, and a wrong explorer
    is a link that quietly shows nothing.
    """

    tx_hash: str
    link: str
    # Kept so the execution can be checked against KeeperHub's own audit trail later.
    # Dropping it meant the only account of what happened was ours, which is the problem
    # this whole feature exists to solve, one level up.
    execution_id: str = ""
    # What KeeperHub's audit trail says once asked. Empty until `confirm` fills them, and
    # empty is honest: it means nobody has checked, not that the check failed.
    verified: bool | None = None
    receipt_status: str = ""


def enabled() -> bool:
    """Whether this world may put anything onchain at all.

    Default off, like `llm_enabled`, and for the same reason: an operator who has not asked
    for this must never discover it by looking at a chain explorer.
    """
    return os.getenv(ENABLED_ENV, "").strip().lower() in _TRUE


def api_key() -> str:
    return os.getenv(API_KEY_ENV, "").strip()


def configured() -> bool:
    """Enabled *and* able to authenticate. Kept separate from `enabled` so the reason a
    world is not writing onchain can be reported accurately: a missing key and a switched
    off feature are different problems with different fixes."""
    return enabled() and bool(api_key()) and bool(recipient())


def chain_id() -> int:
    raw = os.getenv(CHAIN_ID_ENV, "").strip()
    if not raw:
        return DEFAULT_CHAIN_ID
    try:
        return int(raw)
    except ValueError:
        logger.warning("ignoring %s=%r: not a chain id", CHAIN_ID_ENV, raw)
        return DEFAULT_CHAIN_ID


def recipient() -> str:
    """Where the proof transaction is sent.

    Lowercased before it leaves here. KeeperHub validates recipients with a strict EIP-55
    checksum and accepts either the exact mixed-case form or an all-lowercase one — a
    mixed-case address with a mangled checksum is refused even when its hex is correct,
    which is the most common way a copied address fails.
    """
    return os.getenv(RECIPIENT_ENV, "").strip().lower()


def amount() -> str:
    return os.getenv(AMOUNT_ENV, "").strip() or DEFAULT_AMOUNT


def idempotency_key(memo: str) -> str:
    """A key that identifies the work rather than the attempt.

    KeeperHub replays a stored response for 24 hours when the same key arrives with the
    same body, and executes again when the key is new. So this is derived from the memo —
    which names the clan, the goal and the tick — and never from a clock or a random
    value: a retry of the same decision must not become a second transaction, and two
    genuinely different decisions must not collide onto one.
    """
    return "nous-" + hashlib.sha256(memo.encode("utf-8")).hexdigest()[:32]


def _body(to: str, value: str) -> dict:
    """The request, in one place so the simulated and broadcast forms cannot drift.

    Deliberately does not carry the memo. KeeperHub's transfer endpoint documents
    `chainId`, `recipientAddress`, `amount`, `tokenAddress`, `tokenConfig` and
    `gasLimitMultiplier` and nothing else, and an undocumented field is a 400 waiting to
    happen — worse, it would change the body hash and turn every retry into an
    idempotency conflict. The memo rides in the idempotency key instead, where it does
    real work.
    """
    return {"chainId": chain_id(), "recipientAddress": to, "amount": value}


async def fire_action(
    recipient_address: str = "",
    value: str = "",
    memo: str = "",
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Execution | None:
    """Put one transaction onchain. Returns the hash and a link, or None if anything at all
    went wrong.

    Never raises. The caller is a background job whose failure would be invisible, and a
    world that cannot reach KeeperHub must behave exactly like a world that was never
    configured for it.
    """
    if not enabled():
        # Checked before anything is built, so a disabled world opens no socket, resolves
        # no dns, and cannot be slowed down by a gateway it was never asked to call.
        return None

    key = api_key()
    to = (recipient_address or recipient()).strip().lower()
    if not key or not to:
        logger.warning(
            "keeperhub is enabled but not configured (key: %s, recipient: %s)",
            "set" if key else "missing",
            "set" if to else "missing",
        )
        return None

    payload = _body(to, value or amount())
    headers = {
        "authorization": f"Bearer {key}",
        "content-type": "application/json",
        "idempotency-key": idempotency_key(memo or to),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{BASE_URL}{TRANSFER_PATH}", json=payload, headers=headers
            )
    except Exception as error:  # noqa: BLE001 - a network failure must not reach the world
        logger.warning("keeperhub unreachable (%s)", error)
        return None

    if response.status_code >= 400:
        # The body says why and carries no credential of ours, so it is worth logging: a
        # spending cap, a bad recipient and an expired key are three different problems
        # that look identical from a status code alone.
        detail = response.text[:200]
        logger.warning("keeperhub refused the action: %s %s", response.status_code, detail)
        return None

    try:
        answer = response.json()
    except ValueError:
        logger.warning("keeperhub returned a body that was not json")
        return None
    if not isinstance(answer, dict):
        return None

    tx_hash = str(answer.get("transactionHash") or "").strip()
    status = str(answer.get("status") or "").strip()
    if not tx_hash:
        # Documented: transactionHash is present only when status is "completed". A failed
        # execution is a real answer, not a malformed one.
        logger.warning("keeperhub execution did not complete (status: %s)", status or "unknown")
        return None

    if answer.get("idempotentReplay"):
        # The same decision reached KeeperHub twice inside its 24 hour window and the
        # second call was answered from the first one's stored response. No second
        # transaction happened, and saying so is the difference between a receipt and a
        # double count.
        logger.info("keeperhub replayed an earlier execution for this decision")

    logger.info("keeperhub executed %s", tx_hash)
    # Their link when they gave one, ours when they did not. The fallback only knows one
    # chain, which is why it is the fallback.
    link = str(answer.get("transactionLink") or "").strip() or explorer_link(tx_hash)
    return Execution(
        tx_hash=tx_hash,
        link=link,
        execution_id=str(answer.get("executionId") or "").strip(),
    )


STATUS_PATH = "/api/execute/{execution_id}/status"


async def confirm(execution: Execution, *, timeout: float = DEFAULT_TIMEOUT) -> Execution:
    """Ask KeeperHub's audit trail what actually happened onchain.

    The transfer response tells us a transaction was sent, but KeeperHub is explicit that
    `transactionHash` and `transactionLink` are *self-reported by the write path*, while
    the `receipts` array on the status endpoint is re-fetched from the chain. So this is
    the difference between "we believe we sent it" and "the chain says it happened", and
    only the second is worth showing to somebody who has no reason to trust this server.

    Additive and fail-soft: on any problem the execution is returned exactly as it came
    in. A receipt with a transaction hash and no verification is still a receipt; losing
    the hash because a second call failed would be strictly worse than not making it.
    """
    if not execution.execution_id or not enabled():
        return execution
    key = api_key()
    if not key:
        return execution

    url = BASE_URL + STATUS_PATH.format(execution_id=execution.execution_id)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers={"authorization": f"Bearer {key}"})
    except Exception as error:  # noqa: BLE001 - a failed check must not lose the receipt
        logger.warning("keeperhub status unreachable (%s)", error)
        return execution

    if response.status_code >= 400:
        logger.warning("keeperhub status refused: %s", response.status_code)
        return execution
    try:
        answer = response.json()
    except ValueError:
        return execution
    if not isinstance(answer, dict):
        return execution

    receipts = answer.get("receipts")
    if not isinstance(receipts, list) or not receipts:
        return execution
    first = receipts[0]
    if not isinstance(first, dict):
        return execution

    return replace(
        execution,
        verified=bool(first.get("verified")),
        receipt_status=str(first.get("receiptStatus") or "").strip(),
    )


def explorer_link(tx_hash: str) -> str:
    """A link anybody can open. Base Sepolia only — a wrong explorer for a chain override
    would be a link that silently shows nothing, so anything else returns empty rather
    than guessing."""
    if not tx_hash:
        return ""
    if chain_id() != DEFAULT_CHAIN_ID:
        return ""
    return f"https://sepolia.basescan.org/tx/{tx_hash}"
