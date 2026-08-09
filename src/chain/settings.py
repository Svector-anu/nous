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


# --- rails ---------------------------------------------------------------------
#
# A rail is one way to pay: a chain, a token on it, the address that collects, and the
# node to check against. There was exactly one, hardcoded, and that was the whole problem
# — the world charged USDG on Robinhood Chain and the wallets that might want to pay it
# hold USDC on Base. Discoverable is not the same as payable.
#
# Kept here rather than in WorldConfig for the same reason as everything else in this
# file: a rail carries an rpc url, an rpc url can carry a provider key, and WorldConfig is
# persisted verbatim into world_meta.

# USDC on Base. Public contract, 6 decimals, verified against the live chain and against
# the payment challenges of two independent x402 services that settle in it.
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
BASE_CHAIN_ID = 8453
BASE_PUBLIC_RPC = "https://mainnet.base.org"


class Rail:
    """One (chain, token, recipient, node). Compared by chain id everywhere."""

    def __init__(
        self,
        *,
        chain_id: int,
        chain_name: str,
        currency: str,
        asset: str,
        decimals: int,
        recipient: str,
        rpc_url: str,
    ) -> None:
        self.chain_id = chain_id
        self.chain_name = chain_name
        self.currency = currency
        self.asset = asset
        self.decimals = decimals
        self.recipient = recipient
        self.rpc_url = rpc_url

    @property
    def payable(self) -> bool:
        """Whether this rail could actually take a payment. A rail missing a recipient or
        a token contract is advertised to nobody: a 402 offering an option that cannot be
        verified is a locked door with a painted-on keyhole."""
        return bool(self.recipient and self.asset)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Rail({self.currency} on {self.chain_name}/{self.chain_id})"


def base_recipient() -> str:
    """Where USDC on Base is collected. Falls back to the Robinhood recipient, because the
    same wallet address works on both chains and requiring two is friction with no
    security benefit — an operator who wants them separate sets this."""
    explicit = os.getenv("BASE_RECIPIENT_ADDRESS", "").strip()
    return explicit or x402_recipient()


def base_rpc_url() -> str:
    return os.getenv("BASE_RPC_URL", "").strip() or BASE_PUBLIC_RPC


def usdc_address() -> str:
    return os.getenv("USDC_CONTRACT_ADDRESS", "").strip() or USDC_BASE


def rails(config=None, testnet: bool = False) -> list[Rail]:
    """Every way this world will accept a payment, best-known first.

    USDG on Robinhood Chain stays first: it is what the world has always charged, it is
    what the existing receipts settled in, and a client that reads only `accepts[0]`
    keeps working. Base is added, not substituted.

    A rail with no recipient is dropped rather than advertised. `BASE_ENABLED=false`
    removes Base outright, for a deployment that wants one chain.
    """
    chain_id = getattr(config, "chain_id", 4663) if config is not None else 4663
    chain_name = getattr(config, "chain_name", "Robinhood Chain") if config is not None else "Robinhood Chain"
    currency = getattr(config, "x402_currency", "USDG") if config is not None else "USDG"

    found = [
        Rail(
            chain_id=chain_id,
            chain_name=chain_name,
            currency=currency,
            asset=usdg_address(testnet),
            decimals=USDG_DECIMALS,
            recipient=x402_recipient(),
            rpc_url=rpc_url(testnet),
        )
    ]
    if os.getenv("BASE_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off"):
        found.append(
            Rail(
                chain_id=BASE_CHAIN_ID,
                chain_name="Base",
                currency="USDC",
                asset=usdc_address(),
                decimals=USDC_DECIMALS,
                recipient=base_recipient(),
                rpc_url=base_rpc_url(),
            )
        )
    return [rail for rail in found if rail.payable]
