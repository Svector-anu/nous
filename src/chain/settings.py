"""Chain network settings from the environment.

Credentials never go into WorldConfig — it is persisted verbatim into the world_meta
table, so it cannot carry an rpc url that might include a provider key. All chain
settings live here as env variables and stay out of the save.
"""

from __future__ import annotations

import os


def rpc_url(testnet: bool = False) -> str:
    """Robinhood Chain RPC endpoint. Prefers an Alchemy URL when set; falls back to
    the public endpoint."""
    key = "ROBINHOOD_TESTNET_RPC_URL" if testnet else "ROBINHOOD_RPC_URL"
    url = os.getenv(key, "").strip()
    if url:
        return url
    # Public endpoints — no key required, but they rate-limit.
    return (
        "https://rpc.testnet.chain.robinhood.com"
        if testnet
        else "https://rpc.mainnet.chain.robinhood.com"
    )


def x402_recipient() -> str:
    """The address that collects x402 payments. Required when x402_verifier="chain".

    A chain-verified payment is a transfer to this address, so no address means the
    verifier cannot work — the api returns 503 and logs the gap.
    """
    return os.getenv("X402_RECIPIENT_ADDRESS", "").strip()
