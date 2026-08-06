"""A very small JSON-RPC client for Robinhood Chain.

Deliberately not a web3 dependency: three methods are needed and `httpx` is already a
requirement. Every call is bounded by a timeout and every failure is contained, because
this reaches outside the process and the world must keep running when it fails.

**Never call this from a system.** Systems run once per tick inside the loop; a network
round-trip there would stall the world and make history depend on the network. Callers
are api routes and background jobs only.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import settings

logger = logging.getLogger("neociv")

DEFAULT_TIMEOUT = 8.0


class RpcError(RuntimeError):
    """The node answered with an error, or could not be reached."""


class Rpc:
    def __init__(
        self,
        url: str | None = None,
        *,
        testnet: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url if url is not None else settings.rpc_url(testnet)
        self.timeout = timeout
        self._id = 0

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params if params is not None else [],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.url, json=payload)
                response.raise_for_status()
                body = response.json()
        except Exception as error:  # noqa: BLE001 - the caller decides what a failure means
            raise RpcError(f"{method} failed: {error!r}") from error

        if isinstance(body, dict) and body.get("error"):
            raise RpcError(f"{method} returned {body['error']!r}")
        if not isinstance(body, dict) or "result" not in body:
            raise RpcError(f"{method} returned no result")
        return body["result"]

    async def chain_id(self) -> int:
        return int(await self.call("eth_chainId"), 16)

    async def block_number(self) -> int:
        return int(await self.call("eth_blockNumber"), 16)

    async def transaction_receipt(self, tx_hash: str) -> dict | None:
        """None means the node has not seen it yet — pending, or simply not ours."""
        result = await self.call("eth_getTransactionReceipt", [tx_hash])
        return result if isinstance(result, dict) else None

    async def transaction(self, tx_hash: str) -> dict | None:
        result = await self.call("eth_getTransactionByHash", [tx_hash])
        return result if isinstance(result, dict) else None
