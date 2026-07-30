"""A broken viewer must not be able to stop the world.

This is not hypothetical. The tick loop was observed frozen on tick 21200 with the http api
still happily serving that snapshot, after dozens of short-lived browsers had connected and
died. `broadcast` caught only WebSocketDisconnect and RuntimeError; an abrupt reset raises
ConnectionResetError (or anyio's BrokenResourceError), which escaped, propagated out of the
loop, and killed its asyncio task. Nothing awaited that task until shutdown, so there was no
log line and no failing request — the simulation simply stopped and looked fine.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.server import ConnectionManager, create_app
from src.world.config import WorldConfig


class _Socket:
    """Stands in for a starlette WebSocket. `raises` is what its send blows up with."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.raises = raises
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        if self.raises is not None:
            raise self.raises
        self.sent.append(payload)


def _manager(*sockets: _Socket) -> ConnectionManager:
    manager = ConnectionManager()
    for socket in sockets:
        manager._connections.add(socket)  # type: ignore[arg-type]
    return manager


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("peer went away"),
        BrokenPipeError("write on a closed pipe"),
        OSError("socket is gone"),
        RuntimeError('Cannot call "send" once a close message has been sent'),
        ValueError("payload could not be encoded"),
    ],
    ids=["reset", "broken-pipe", "oserror", "runtime", "value"],
)
def test_a_failing_client_never_propagates(error):
    """Whatever a dead socket raises, broadcast has to absorb it. The specific type depends
    on how the peer died and on the server stack, which is exactly why this cannot be a
    whitelist."""
    bad = _Socket(raises=error)
    manager = _manager(bad)
    asyncio.run(manager.broadcast({"tick": 1}))
    assert bad not in manager._connections


def test_a_failing_client_does_not_starve_the_healthy_ones():
    good_before = _Socket()
    bad = _Socket(raises=ConnectionResetError())
    good_after = _Socket()
    manager = _manager(good_before, bad, good_after)

    asyncio.run(manager.broadcast({"tick": 7}))

    assert good_before.sent == [{"tick": 7}]
    assert good_after.sent == [{"tick": 7}]
    assert bad not in manager._connections
    assert good_before in manager._connections
    assert good_after in manager._connections


def test_cancellation_still_propagates():
    """Shutdown cancels the loop task. Swallowing CancelledError here would hang it."""
    manager = _manager(_Socket(raises=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager.broadcast({"tick": 1}))


def test_health_reports_a_running_world(tmp_path):
    config = WorldConfig(seed=99, grid_width=16, grid_height=16, agent_count=6, resource_count=12)
    app = create_app(config=config, db_path=tmp_path / "world.db")
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        body = health.json()
        assert body["running"] is True
        assert body["error"] is None


def test_health_reports_a_stalled_world(tmp_path):
    """The point of the endpoint: /state cannot distinguish a frozen world from a live one,
    because a stopped loop still serves a well-formed snapshot."""
    config = WorldConfig(seed=99, grid_width=16, grid_height=16, agent_count=6, resource_count=12)
    app = create_app(config=config, db_path=tmp_path / "world.db")
    with TestClient(app) as client:
        app.state.loop_task.cancel()
        # A cancelled task is done but carries no exception; the endpoint must still refuse
        # to call that healthy.
        health = client.get("/health")
        assert health.status_code == 503
        assert health.json()["running"] is False
        # /state, by contrast, looks entirely normal — which is the trap.
        assert client.get("/state").status_code == 200
