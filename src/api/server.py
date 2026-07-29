"""FastAPI app: serves the viewer, runs the sim loop, streams state over WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from ..persistence.sqlite_store import SqliteWorldStore
from ..world.config import WorldConfig
from ..world.tick import Simulation, create_world

logger = logging.getLogger("neociv")

VIEWER_INDEX = Path(__file__).resolve().parent.parent / "viewer" / "index.html"
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "world.db"


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, payload: dict) -> None:
        for websocket in list(self._connections):
            try:
                await websocket.send_json(payload)
            except (WebSocketDisconnect, RuntimeError):
                self.disconnect(websocket)


def create_app(
    config: WorldConfig | None = None,
    db_path: Path | None = None,
) -> FastAPI:
    world_config = config if config is not None else WorldConfig()
    store_path = db_path if db_path is not None else DEFAULT_DB_PATH
    manager = ConnectionManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = SqliteWorldStore(store_path)
        if store.has_save():
            world = store.load()
            logger.info("resumed world from %s at tick %d", store_path, world.tick)
        else:
            world = create_world(world_config)
            store.save(world)
            logger.info("created new world with seed %d", world_config.seed)

        simulation = Simulation(world, store=store)
        app.state.simulation = simulation
        loop_task = asyncio.create_task(_run_loop(simulation, manager))

        try:
            yield
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
            store.save(simulation.world)
            store.close()
            logger.info("saved world at tick %d on shutdown", simulation.world.tick)

    app = FastAPI(title="Neo-Civilization", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(VIEWER_INDEX)

    @app.get("/state")
    async def state() -> JSONResponse:
        return JSONResponse(app.state.simulation.snapshot())

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await manager.connect(websocket)
        try:
            await websocket.send_json(app.state.simulation.snapshot())
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)
        except RuntimeError:
            manager.disconnect(websocket)

    return app


async def _run_loop(simulation: Simulation, manager: ConnectionManager) -> None:
    """Fixed timestep, drift-corrected: a slow tick shortens the next sleep."""
    interval = simulation.world.config.tick_seconds
    next_deadline = asyncio.get_running_loop().time()

    while True:
        simulation.step()
        await manager.broadcast(simulation.snapshot())

        next_deadline += interval
        delay = next_deadline - asyncio.get_running_loop().time()
        if delay < 0:
            next_deadline = asyncio.get_running_loop().time()
            delay = 0
        await asyncio.sleep(delay)
