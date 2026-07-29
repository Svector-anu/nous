"""FastAPI app: serves the viewer, runs the sim loop, streams state over WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ..persistence.sqlite_store import SqliteWorldStore
from ..world.components import Agent, ClanRef, Position
from ..world.config import WorldConfig
from ..llm.advisor import build_advisor
from ..world.systems import leadership, spawning
from ..world.tick import Simulation, create_world


class DeployRequest(BaseModel):
    """A user deploying an agent. Personality is a note the agent carries, not a prompt —
    there is no llm in the loop yet."""

    name: str = Field(..., description="display name for the agent")
    personality: str = Field(default="", description="short personality note")

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

        world.advisor = build_advisor(world.config)
        logger.info("clan advisor: %s", type(world.advisor).__name__)

        simulation = Simulation(world, store=store)
        app.state.simulation = simulation
        loop_task = asyncio.create_task(_run_loop(simulation, manager))

        try:
            yield
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
            if world.advisor is not None:
                world.advisor.close()
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

    @app.get("/decisions")
    async def decisions() -> JSONResponse:
        """Every clan-goal decision, rule-based and model-based, newest last."""
        world = app.state.simulation.world
        current = leadership.log(world)
        advisor = world.advisor
        return JSONResponse(
            {
                "llm_enabled": world.config.llm_enabled,
                "advisor": type(advisor).__name__ if advisor is not None else None,
                "pending": advisor.pending() if advisor is not None else 0,
                "entries": list(current.entries) if current is not None else [],
            }
        )

    @app.get("/agents")
    async def agent_cards() -> JSONResponse:
        world = app.state.simulation.world
        cards = []
        for entity in world.query(Agent, Position):
            agent = world.get(entity, Agent)
            if not agent.user_deployed:
                continue
            position = world.get(entity, Position)
            reference = world.try_get(entity, ClanRef)
            cards.append(
                {
                    "id": entity,
                    "name": agent.name,
                    "personality": agent.personality,
                    "state": agent.state.value,
                    "x": position.x,
                    "y": position.y,
                    "clan": reference.clan_id if reference is not None else None,
                    "alive": True,
                }
            )
        pending = spawning.queue(world)
        return JSONResponse(
            {
                "agents": cards,
                "pending": list(pending.pending) if pending is not None else [],
                "limit": world.config.max_user_agents,
            }
        )

    @app.post("/agents", status_code=202)
    async def deploy_agent(request: DeployRequest) -> JSONResponse:
        world = app.state.simulation.world
        config = world.config

        name = request.name.strip()
        personality = request.personality.strip()
        if not name:
            raise HTTPException(422, "name must not be empty")
        if len(name) > config.max_name_length:
            raise HTTPException(422, f"name must be at most {config.max_name_length} characters")
        if len(personality) > config.max_personality_length:
            raise HTTPException(
                422, f"personality must be at most {config.max_personality_length} characters"
            )

        pending = spawning.queue(world)
        if pending is None:
            raise HTTPException(503, "world is not ready")
        if len(pending.pending) >= config.max_pending_spawns:
            raise HTTPException(429, "spawn queue is full, try again in a moment")
        if spawning.user_agent_count(world) + len(pending.pending) >= config.max_user_agents:
            raise HTTPException(409, f"world is at its limit of {config.max_user_agents} user agents")

        spawning.enqueue(world, name, personality)
        return JSONResponse(
            {
                "queued": True,
                "name": name,
                "personality": personality,
                "arrives_at_tick": world.tick + 1,
            },
            status_code=202,
        )

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
