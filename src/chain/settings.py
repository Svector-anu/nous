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


# USDG on Robinhood Chain mainnet — Paxos's stablecoin, bridged as a LayerZero OFT.
# Public contract address, not a credential, so it lives in source rather than env.
# Verified against the live chain: symbol() returns "USDG" and decimals() returns 6.
USDG_MAINNET = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
USDG_DECIMALS = 6


def usdg_address(testnet: bool = False) -> str:
    """The USDG contract to check payments against.

    Paxos publishes no testnet deployment, so the testnet address must come from the
    environment. An empty return means "no token contract configured", and the verifier
    treats that as a refusal rather than skipping the amount check — accepting a payment
    whose size cannot be established is the hole this exists to close.
    """
    override = os.getenv("USDG_CONTRACT_ADDRESS", "").strip()
    if override:
        return override
    return "" if testnet else USDG_MAINNET


def x402_recipient() -> str:
    """The address that collects x402 payments. Required when x402_verifier="chain".

    A chain-verified payment is a transfer to this address, so no address means the
    verifier cannot work — the api returns 503 and logs the gap.
    """
    return os.getenv("X402_RECIPIENT_ADDRESS", "").strip()
