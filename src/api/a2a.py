"""A2A: let another agent discover this world and act in it without a human.

Everything Nous can do from outside was already an http route, which is fine for a person
reading a readme and useless to somebody else's agent. A2A closes that gap with a manifest
at a predictable url: a foreign agent fetches the card, learns the skills and what they
cost, and calls them — no registration, no account, nobody explaining the api to it.

Payment is the interesting half. Base A2A has no notion of it at all; its security schemes
are api keys, oauth and mtls, all of which assume somebody once filled in a form. The x402
extension carries payment in task metadata instead, which is what makes a call like "pay
this clan's leader to reconsider" expressible to a caller that has a wallet and no hands.

    https://github.com/google-a2a/a2a-x402/v0.1

This is a thin binding, not a second api. Every skill resolves to the same code path the
http routes use, so an agent and a browser get the same world and the same payment guard.

Known gap: the price is USDG on Robinhood Chain, and the agent wallets in circulation hold
USDC on Base. A foreign agent can discover Nous and read the price today; paying it needs
an asset it is likely to hold. That is a settlement decision, not a protocol one, and it is
deliberately not guessed at here.
"""

from __future__ import annotations

from typing import Any

# The extension the card advertises. Version is part of the identifier on purpose: a client
# that only knows v0.1 must be able to tell that it does.
X402_EXTENSION_URI = "https://github.com/google-a2a/a2a-x402/v0.1"

# Metadata keys from the extension. Namespaced, because they ride in the same bag as
# anything else a caller attached to the message.
PAYMENT_STATUS = "x402.payment.status"
PAYMENT_REQUIRED = "x402.payment.required"
PAYMENT_PAYLOAD = "x402.payment.payload"

PROTOCOL_VERSION = "0.3.0"

# Skills the card advertises. Kept here rather than inline so the card and the dispatcher
# cannot disagree about what exists — a card that offers a skill nothing implements is
# worse than one that offers less.
FREE_SKILLS = ("observe-world", "read-decisions")
# Owner-only: they need the token handed out when a mind was attached.
MIND_SKILLS = ("observe-my-agent", "steer-my-agent")
PAID_SKILLS = ("nudge-clan",)


def agent_card(*, base_url: str, config, world_ready: bool = True) -> dict:
    """The manifest at /.well-known/agent-card.json.

    Served unauthenticated and cheap: discovery has to work before any relationship
    exists, which is the whole point of a well-known uri.
    """
    paid = bool(config.x402_enabled and config.llm_force_decision_enabled)
    skills: list[dict] = [
        {
            "id": "observe-world",
            "name": "Observe the world",
            "description": (
                "Read the current state: agents, clans, their goals and the reason each "
                "leader gave for its last decision."
            ),
            "tags": ["read", "free"],
            "examples": ["What is clan 12 doing?", "How many agents are alive?"],
        },
        {
            "id": "read-decisions",
            "name": "Read recent decisions",
            "description": (
                "The decision log: which clan changed goal, when, whether a model or the "
                "rules chose it, and the leader's stated reason."
            ),
            "tags": ["read", "free"],
            "examples": ["Which clans did a model decide for recently?"],
        },
    ]
    if getattr(config, "attached_minds_enabled", False):
        skills.extend(
            [
                {
                    "id": "observe-my-agent",
                    "name": "See what my agent sees",
                    "description": (
                        "Perception for an agent you deployed and attached a mind to. "
                        "Needs the token issued when the mind was attached."
                    ),
                    "tags": ["read", "owner"],
                    "examples": ["What is my agent doing right now?"],
                },
                {
                    "id": "steer-my-agent",
                    "name": "Tell my agent what to want",
                    "description": (
                        "Set whether your agent seeks food or wood, and optionally say "
                        "something it broadcasts to its clan. Applied on the next tick. "
                        "Confers no advantage: both choices were already available to it."
                    ),
                    "tags": ["write", "owner"],
                    "examples": ["Go for wood", "Say: meet me at the river"],
                },
            ]
        )

    if paid:
        skills.append(
            {
                "id": "nudge-clan",
                "name": "Pay a clan leader to reconsider",
                "description": (
                    f"Interrupt a clan's current goal and make its leader decide again. "
                    f"Costs {config.x402_price} {config.x402_currency}, settled on-chain. "
                    "The world records the transaction it was bought with."
                ),
                "tags": ["write", "paid", "x402"],
                "examples": ["Make clan 12 reconsider"],
            }
        )

    card: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "name": "Nous",
        "description": (
            "A persistent world of agents that forage, build, form clans and raid each "
            "other. Clan leaders are a language model and publish their reasoning. The "
            "world runs continuously and is not paused between calls."
        ),
        "url": f"{base_url}/a2a",
        "version": "0.1.0",
        "provider": {"organization": "Nous", "url": base_url},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "capabilities": {
            # No streaming: a caller that wants to watch the world has a websocket already,
            # and claiming a transport that is not implemented is worse than omitting it.
            "streaming": False,
            "pushNotifications": False,
        },
        "skills": skills,
        # Reading is open. Nothing here is private — the same state is on the website.
        "securitySchemes": {},
        "security": [],
    }
    if paid:
        card["capabilities"]["extensions"] = [
            {
                "uri": X402_EXTENSION_URI,
                "description": (
                    "Some skills require payment. Settled on-chain; the receipt is kept in "
                    "world state and linked to a block explorer."
                ),
                # Not required to *talk* to this agent — the free skills work without it.
                # Marking it required would tell a caller with no wallet to go away when
                # most of what is here costs nothing.
                "required": False,
            }
        ]
    if not world_ready:
        card["description"] += " (The world is starting up; skills may be briefly refused.)"
    return card


def payment_required_task(
    *, task_id: str, context_id: str, skill: str, config, recipient: str, asset: str
) -> dict:
    """A task parked in `input-required`, carrying what a wallet needs to pay.

    The x402 extension does not invent a task state for this: the task is waiting on the
    caller exactly like one waiting on a missing argument, and what makes it a payment is
    the metadata. That distinction matters for a client that does not speak the extension
    — it still sees a well-formed task it cannot satisfy, rather than a protocol error.
    """
    units = _units(config.x402_price)
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": "input-required",
            "message": {
                "kind": "message",
                "role": "agent",
                "messageId": f"{task_id}-payment",
                "parts": [
                    {
                        "kind": "text",
                        "text": (
                            f"{config.x402_price} {config.x402_currency} is required to "
                            f"use {skill}."
                        ),
                    }
                ],
                "metadata": {
                    PAYMENT_STATUS: "payment-required",
                    PAYMENT_REQUIRED: {
                        "x402Version": 1,
                        "accepts": [
                            {
                                "scheme": "exact",
                                "network": config.chain_name,
                                "chainId": config.chain_id,
                                "resource": f"a2a:nudge-clan",
                                "asset": asset,
                                "payTo": recipient,
                                "maxAmountRequired": str(units),
                                "maxTimeoutSeconds": 600,
                                "description": (
                                    f"{config.x402_price} {config.x402_currency}"
                                ),
                            }
                        ],
                    },
                },
            },
        },
    }


def completed_task(*, task_id: str, context_id: str, text: str, data: Any = None) -> dict:
    parts: list[dict] = [{"kind": "text", "text": text}]
    if data is not None:
        parts.append({"kind": "data", "data": data})
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {"state": "completed"},
        "artifacts": [{"artifactId": f"{task_id}-result", "parts": parts}],
    }


def failed_task(*, task_id: str, context_id: str, text: str) -> dict:
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": "failed",
            "message": {
                "kind": "message",
                "role": "agent",
                "messageId": f"{task_id}-error",
                "parts": [{"kind": "text", "text": text}],
            },
        },
    }


def rpc_error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def rpc_result(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _units(price: str) -> int:
    """Price in the token's smallest unit. Decimal rather than float for the same reason
    the payment verifier uses it: 0.10 has no exact binary form and this number decides
    whether somebody's payment is judged sufficient."""
    from ..chain.x402 import price_units
    from ..chain.settings import USDG_DECIMALS

    return price_units(price, USDG_DECIMALS)


def text_of(message: dict) -> str:
    """Flatten a message's text parts. A caller may split a sentence across parts and
    nothing about the split is meaningful to us."""
    out = []
    for part in (message or {}).get("parts", []) or []:
        if part.get("kind") == "text" and isinstance(part.get("text"), str):
            out.append(part["text"])
    return " ".join(out).strip()


def data_of(message: dict) -> dict:
    """The first data part, which is where a machine caller puts arguments. Text is for
    humans watching; this is what a skill actually reads."""
    for part in (message or {}).get("parts", []) or []:
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            return part["data"]
    return {}


def payment_payload(message: dict) -> dict:
    meta = (message or {}).get("metadata") or {}
    payload = meta.get(PAYMENT_PAYLOAD)
    return payload if isinstance(payload, dict) else {}
