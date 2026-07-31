"""FastAPI app: serves the viewer, runs the sim loop, streams state over WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..persistence.sqlite_store import SqliteWorldStore
from ..world.components import Agent, ClanRef, Position
from ..world.config import WorldConfig
from ..llm.advisor import build_advisor
from ..world.systems import leadership, markets, spawning
from ..world.tick import Simulation, create_world


class PositionRequest(BaseModel):
    """A spectator taking a side. Demo credits only — there is no real money here."""

    user: str = Field(..., description="display name of the bettor")
    side: str = Field(..., description="yes or no")
    stake: int = Field(..., description="credits to stake")


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
        """One bad client must never be able to stop the world.

        This used to catch only WebSocketDisconnect and RuntimeError. A browser that dies
        abruptly instead surfaces as ConnectionResetError, or anyio's BrokenResourceError,
        and that escaped here, propagated out of the tick loop, and killed its asyncio
        task. Nothing awaited that task until shutdown, so the failure was silent: the http
        api carried on serving a frozen snapshot and the simulation looked alive while it
        had stopped dead. Observed in the wild — the world sat on tick 21200 while dozens
        of short-lived test browsers came and went.
        """
        for websocket in list(self._connections):
            try:
                await websocket.send_json(payload)
            except asyncio.CancelledError:
                # Shutdown, not a client fault — must not be swallowed.
                raise
            except Exception:
                logger.debug("dropping a websocket that failed to receive", exc_info=True)
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
        simulation.last_tick_at = None
        app.state.simulation = simulation
        loop_task = asyncio.create_task(_run_loop(simulation, manager))
        app.state.loop_task = loop_task

        def _report_stall(task: asyncio.Task) -> None:
            """Nothing awaits the loop task until shutdown, so without this a crash inside
            it leaves no trace at all."""
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                logger.error("simulation loop stopped: %r", error, exc_info=error)

        loop_task.add_done_callback(_report_stall)

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
    # The viewer is plain files: the focus-3d module and a vendored copy of three.js.
    # Vendored rather than fetched from a cdn so the viewer works offline, which is the
    # same rule the procedural-materials reference holds itself to.
    app.mount("/static", StaticFiles(directory=VIEWER_INDEX.parent), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(VIEWER_INDEX)

    @app.get("/state")
    async def state() -> JSONResponse:
        return JSONResponse(app.state.simulation.snapshot())

    @app.get("/health")
    async def health() -> JSONResponse:
        """Is the world actually advancing? /state alone cannot answer that — a stopped
        loop keeps serving a perfectly well-formed snapshot of a frozen world."""
        task = getattr(app.state, "loop_task", None)
        simulation = app.state.simulation
        running = task is not None and not task.done()
        error = None
        if task is not None and task.done() and not task.cancelled():
            exception = task.exception()
            error = repr(exception) if exception is not None else None
        return JSONResponse(
            {
                "running": running,
                "tick": simulation.world.tick,
                "error": error,
            },
            status_code=200 if running else 503,
        )

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

    @app.get("/markets")
    async def market_list() -> JSONResponse:
        """Open and settled markets, plus balances and a leaderboard.

        Reading is cheap and the book is bounded, so this serves the whole thing rather
        than paginating something that cannot grow.
        """
        world = app.state.simulation.world
        book = markets.book(world)
        if book is None:
            return JSONResponse({"markets": [], "balances": {}, "leaderboard": []})

        live = [m for m in book.markets if m["state"] != markets.RESOLVED]
        settled = [m for m in book.markets if m["state"] == markets.RESOLVED]
        leaderboard = sorted(
            ({"user": u, "credits": c} for u, c in book.balances.items()),
            key=lambda row: (-row["credits"], row["user"]),
        )
        return JSONResponse(
            {
                "tick": world.tick,
                "open": live,
                "settled": settled[-20:],
                "balances": dict(sorted(book.balances.items())),
                "leaderboard": leaderboard,
                "starting_balance": world.config.market_starting_balance,
                "max_stake": world.config.market_max_stake,
                "pending": len(book.pending),
            }
        )

    @app.post("/markets/{market_id}/positions", status_code=202)
    async def take_position(market_id: int, request: PositionRequest) -> JSONResponse:
        world = app.state.simulation.world
        config = world.config
        book = markets.book(world)
        if book is None:
            raise HTTPException(503, "world is not ready")

        user = request.user.strip()
        if not user:
            raise HTTPException(422, "user must not be empty")
        if len(user) > config.max_name_length:
            raise HTTPException(422, f"user must be at most {config.max_name_length} characters")
        if request.side not in (markets.YES, markets.NO):
            raise HTTPException(422, "side must be 'yes' or 'no'")
        if request.stake <= 0:
            raise HTTPException(422, "stake must be positive")
        if request.stake > config.market_max_stake:
            raise HTTPException(422, f"stake must be at most {config.market_max_stake}")

        entry = next((m for m in book.markets if m["id"] == market_id), None)
        if entry is None:
            raise HTTPException(404, f"no market {market_id}")
        if entry["state"] != markets.OPEN:
            raise HTTPException(409, f"market {market_id} is {entry['state']}")

        balance = book.balances.get(user, config.market_starting_balance)
        if balance < request.stake:
            raise HTTPException(409, f"{user} holds {balance} credits, not {request.stake}")

        # Queued, not applied: a position is an input to the simulation in exactly the way
        # a deployment is, so it lands at a fixed point in the next tick.
        markets.place(world, user, market_id, request.side, request.stake)
        return JSONResponse(
            {"queued": True, "market": market_id, "user": user,
             "side": request.side, "stake": request.stake,
             "applies_at_tick": world.tick + 1},
            status_code=202,
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
        try:
            simulation.step()
            await manager.broadcast(simulation.snapshot())
        except asyncio.CancelledError:
            raise
        except Exception:
            # A genuine fault in a system is a bug worth stopping on rather than repeating
            # once a second forever — but it must be loud. Silence here is what let a dead
            # loop masquerade as a running world.
            logger.exception(
                "tick %d raised; stopping the simulation loop", simulation.world.tick
            )
            raise
        simulation.last_tick_at = asyncio.get_running_loop().time()

        next_deadline += interval
        delay = next_deadline - asyncio.get_running_loop().time()
        if delay < 0:
            next_deadline = asyncio.get_running_loop().time()
            delay = 0
        await asyncio.sleep(delay)
