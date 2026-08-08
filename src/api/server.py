"""FastAPI app: serves the viewer, runs the sim loop, streams state over WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import a2a
from ..chain import escrow, siwe
from ..chain import settings as chain_settings
from ..chain import x402
from ..chain.x402 import PaymentProof, build_verifier
from ..chain.x402 import price_units
from ..persistence.sqlite_store import SqliteWorldStore
from ..world.components import (
    Agent,
    AttachedMind,
    Clan,
    ClanRef,
    ForceDecisionQueue,
    Position,
    RestQueue,
)
from ..world.config import WorldConfig
from ..llm.advisor import build_advisor
from ..world.systems import identity, leadership, markets, minds, resting, spawning
from ..world.tick import Simulation, create_world, ensure_singletons


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
    # Optional. With it, the agent is owned from the tick it exists rather than claimed
    # afterwards — closing the window in which anyone else could claim it first.
    session: str = Field(default="", description="session token from /chain/verify")


class RestRequest(BaseModel):
    """Toggle a user-deployed agent's rest / play-safe mode."""

    rest: bool = Field(..., description="true to rest, false to resume normal behaviour")


class NonceRequest(BaseModel):
    """Ask for a challenge to sign. The address is a hint for the message text; the
    signature is what actually proves anything."""

    address: str = Field(default="", description="wallet address the user intends to prove")


class VerifyRequest(BaseModel):
    """A signed challenge. The server recovers the address from the signature — it never
    trusts the `address` field on its own."""

    address: str = Field(..., description="claimed wallet address")
    message: str = Field(..., description="the exact message that was signed")
    signature: str = Field(..., description="signature produced by the wallet")


class LinkRequest(BaseModel):
    """Attach a verified wallet address to a user-deployed agent as a label.

    Linking confers nothing in-world. A linked agent starves like every other agent.
    """

    address: str = Field(..., description="verified wallet address")
    session: str = Field(..., description="session token returned by /chain/verify")


logger = logging.getLogger("neociv")

VIEWER_INDEX = Path(__file__).resolve().parent.parent / "viewer" / "index.html"
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "world.db"

# A proven wallet is remembered for this long, then must be proven again.
SESSION_TTL_SECONDS = 24 * 60 * 60
MAX_SESSIONS = 512


class SessionStore:
    """Session token -> proven address. In memory, bounded, expiring.

    Not persisted: a restart signs everyone out, which costs one click and avoids
    keeping a table of long-lived bearer tokens on disk. The *link* itself is durable
    world state; the session is only the proof that let you make it.
    """

    def __init__(self, ttl: float = SESSION_TTL_SECONDS, limit: int = MAX_SESSIONS) -> None:
        self.ttl = ttl
        self.limit = limit
        self._sessions: dict[str, tuple[str, float]] = {}

    def open(self, address: str, now: float | None = None) -> str:
        moment = time.time() if now is None else now
        token = secrets.token_urlsafe(24)
        self._sessions[token] = (siwe.normalise(address), moment)
        self._prune(moment)
        return token

    def address(self, token: str, now: float | None = None) -> str:
        moment = time.time() if now is None else now
        entry = self._sessions.get((token or "").strip())
        if entry is None:
            return ""
        address, issued = entry
        if moment - issued > self.ttl:
            self._sessions.pop(token, None)
            return ""
        return address

    def _prune(self, now: float) -> None:
        stale = [t for t, (_, issued) in self._sessions.items() if now - issued > self.ttl]
        for token in stale:
            self._sessions.pop(token, None)
        while len(self._sessions) > self.limit:
            self._sessions.pop(next(iter(self._sessions)), None)

    def __len__(self) -> int:
        return len(self._sessions)


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


def _nonce_in(message: str) -> str:
    """Pull the nonce out of an EIP-4361 message.

    The nonce must come from the signed text rather than a separate request field. If a
    caller could name the nonce independently of what was signed, one wallet's signature
    could be replayed against a freshly issued challenge.
    """
    for line in (message or "").splitlines():
        if line.startswith("Nonce: "):
            return line[len("Nonce: ") :].strip()
    return ""


# Deployment switches an operator can flip without starting a new world.
#
# The rest of WorldConfig is simulation rules and is restored from the save verbatim, which
# is correct — changing them mid-world would break replay. But these decide whether *this
# deployment* accepts wallets and payments, and a world persisted before the flags existed
# would otherwise be stuck forever on the defaults it was created under. There is no way to
# turn payments on for a running world except this.
#
# Credentials are not here. Addresses and rpc urls stay in chain/settings.py, read straight
# from the environment, because WorldConfig is written into world_meta verbatim.
CHAIN_FLAG_ENV = {
    "chain_identity_enabled": "CHAIN_IDENTITY_ENABLED",
    "x402_enabled": "X402_ENABLED",
    "real_money_enabled": "REAL_MONEY_ENABLED",
    # Named for the llm because that is where it started, but a forced decision no longer
    # needs one: leadership drains the queue and the rule-based goal choice does the work.
    "llm_force_decision_enabled": "FORCE_DECISION_ENABLED",
    # Clan leaders consult a model. Off by default because the world runs 24/7 and every
    # call costs money — but a persisted world would otherwise be stuck on whatever this
    # was when it was created, which is the whole reason these overrides exist.
    "llm_enabled": "LLM_ENABLED",
    # Visitors driving their own agent from outside. Off by default, and needs an override
    # for the same reason as the rest: a persisted world is otherwise stuck with whatever
    # this was when it was created, which would have made the feature unreachable on
    # exactly the world it was built for.
    "attached_minds_enabled": "ATTACHED_MINDS_ENABLED",
}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class Spend(NamedTuple):
    """The result of trying to take a payment.

    This exists because the first version returned `(outcome, detail)` as plain strings,
    and every refusal — "replayed", "unverified" — is a non-empty string and therefore
    truthy. The natural thing to write at a call site is `if not result`, and against
    that shape the natural thing silently treated a refused payment as an accepted one.
    An a2a caller replaying a spent transaction got `completed` back.

    So the type carries its own answer: `bool(spend)` is whether the payment was taken,
    and the obvious call site is now the correct one. `outcome` stays for the caller that
    needs to distinguish a replay (409) from an unverified proof (402), which the http
    route does and the a2a binding does not.
    """

    outcome: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.outcome == "queued"


def _remember_spent(queue_comp, fingerprint: str, limit: int) -> None:
    """Add a proof to the durable spent set, oldest evicted first.

    Bounded because this is world state and grows forever otherwise. The bound is also
    the one real weakness left in the guard: a proof pushed out of the window becomes
    spendable again. `x402_spent_limit` therefore wants to stay comfortably larger than
    the number of payments a world sees between restarts, not merely non-zero.
    """
    if not fingerprint or fingerprint in queue_comp.spent:
        return
    queue_comp.spent.append(fingerprint)
    while len(queue_comp.spent) > max(1, limit):
        queue_comp.spent.pop(0)


def apply_chain_env(config: WorldConfig) -> tuple[WorldConfig, list[str]]:
    """Return `config` with deployment flags overridden from the environment, and a list
    of what changed.

    WorldConfig is frozen, so this replaces rather than mutates — which is the right shape
    anyway: the caller decides whether to adopt the result.

    Unset means "leave it alone", not "false" — an operator who sets only X402_ENABLED
    must not silently switch identity off. An unparseable value is ignored and logged
    rather than guessed at, because guessing wrong here turns real payments on or off.
    """
    overrides: dict[str, object] = {}
    changed: list[str] = []

    for field, name in CHAIN_FLAG_ENV.items():
        raw = os.getenv(name, "").strip().lower()
        if not raw:
            continue
        if raw in _TRUE:
            value = True
        elif raw in _FALSE:
            value = False
        else:
            logger.warning("%s=%r is not a boolean; leaving %s unchanged", name, raw, field)
            continue
        if getattr(config, field) != value:
            overrides[field] = value
            changed.append(f"{field}={value}")

    verifier = os.getenv("X402_VERIFIER", "").strip().lower()
    if verifier in ("header", "chain") and config.x402_verifier != verifier:
        overrides["x402_verifier"] = verifier
        changed.append(f"x402_verifier={verifier}")

    # Which model answers, and where it lives. Settable from env for the same reason the
    # flags are: a persisted world is otherwise stuck with whatever provider it was born
    # with, and swapping a dead key for a working one should not need a new world.
    #
    # Not credentials. The key itself is resolved by the sdk from its own env var — the
    # anthropic client reads ANTHROPIC_AUTH_TOKEN and ANTHROPIC_BASE_URL, which is what
    # makes an anthropic-compatible gateway work without a code change here.
    for field, name in (
        ("llm_provider", "LLM_PROVIDER"),
        ("llm_model", "LLM_MODEL"),
        ("llm_base_url", "LLM_BASE_URL"),
        ("llm_api_key_env", "LLM_API_KEY_ENV"),
    ):
        value = os.getenv(name, "").strip()
        if value and getattr(config, field) != value:
            overrides[field] = value
            changed.append(f"{field}={value}")

    # Both of these decide how much the world costs to run, so both are refused rather
    # than guessed at when unparseable or out of range: a mistake here is somebody's money.
    #
    # The spend cap is a total for the world's whole life, not per process — `calls_made`
    # is durable world state. Raising it is the only way to get more calls out of a world
    # that has already spent its budget, and lowering it stops one immediately.
    #
    # The cooldown is the stronger of the two levers, because it sets the *rate*. One call
    # per clan per cooldown means a world with twenty clans spends twenty times what a
    # world with one does at the same setting, which is why the default cannot be right
    # for every world and this has to be reachable without a new one. A floor of 1 tick is
    # enforced: zero would ask every clan on every tick.
    for field, name, floor in (
        ("llm_max_calls_per_session", "LLM_MAX_CALLS_PER_SESSION", 0),
        ("llm_min_ticks_between_calls", "LLM_MIN_TICKS_BETWEEN_CALLS", 1),
    ):
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        try:
            number = int(raw)
        except ValueError:
            logger.warning("%s=%r is not a number; ignored", name, raw)
            continue
        if number < floor:
            logger.warning("%s=%d is below the minimum of %d; ignored", name, number, floor)
            continue
        if getattr(config, field) != number:
            overrides[field] = number
            changed.append(f"{field}={number}")

    return (replace(config, **overrides) if overrides else config), changed


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
            # A world saved before a component existed has no entity carrying it, and
            # nothing else would ever create one.
            restored = ensure_singletons(world)
            if restored:
                logger.info("added missing singletons to the resumed world: %s", ", ".join(restored))
        else:
            world = create_world(world_config)
            store.save(world)
            logger.info("created new world with seed %d", world_config.seed)

        # After load: a resumed world carries the flags it was saved with, and this is
        # the only way to change them without starting over.
        world.config, changed = apply_chain_env(world.config)
        if changed:
            logger.info("chain flags from environment: %s", ", ".join(changed))
        # Built from the world's config, not the module default, so an env override of
        # the verifier actually takes effect.
        app.state.x402 = build_verifier(
            world.config.x402_verifier, chain_id=world.config.chain_id
        )

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

    app = FastAPI(title="Nous", lifespan=lifespan)
    # Per-app rather than module-level so two apps in one test process cannot see each
    # other's challenges, sessions or spent payments.
    app.state.nonces = siwe.NonceStore()
    app.state.sessions = SessionStore()
    # A default so the attribute always exists; lifespan replaces it with one built from
    # the *loaded* world's config, which is what an env override actually changes.
    app.state.x402 = build_verifier(
        world_config.x402_verifier,
        chain_id=world_config.chain_id,
    )
    # The viewer is plain files: the focus-3d module and a vendored copy of three.js.
    # Vendored rather than fetched from a cdn so the viewer works offline, which is the
    # same rule the procedural-materials reference holds itself to.
    # The vendored three.js library never changes, so it can be cached for a year.
    # Other static files (world3d.js, director.js, index.html) are updated by deploys,
    # so they get a short revalidate window rather than immutable.
    app.mount("/static/vendor", StaticFiles(directory=VIEWER_INDEX.parent / "vendor"), name="vendor")
    app.mount("/static", StaticFiles(directory=VIEWER_INDEX.parent), name="static")

    @app.middleware("http")
    async def cache_control_header(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static/vendor/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/static/") or path == "/favicon.ico":
            response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
        return response

    # The Nous mark: ring, four cardinal ticks, four-pointed star. Same shape the boot
    # screen and top bar carry, redrawn on a 32-unit grid rather than reused verbatim —
    # the 24-unit version uses 1.3 stroke and long thin ticks, which mush into grey at
    # the 16px a browser tab actually renders. Heavier strokes and a larger star survive
    # the downscale; the silhouette stays the same.
    _FAVICON = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<rect width='32' height='32' rx='7' fill='#080c10'/>"
        "<g stroke='#6ee274' stroke-width='2.2' stroke-linecap='round' fill='none'>"
        "<circle cx='16' cy='16' r='8.6'/>"
        "<path d='M16 2.4v3.2M16 26.4v3.2M2.4 16h3.2M26.4 16h3.2'/>"
        "</g>"
        "<path d='M16 10.8l1.9 3.3 3.3 1.9-3.3 1.9-1.9 3.3-1.9-3.3-3.3-1.9 3.3-1.9z'"
        " fill='#6ee274'/>"
        "</svg>"
    )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(_FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.get("/favicon.svg")
    async def favicon_svg() -> Response:
        """Same mark under the extension browsers expect for an svg icon.

        `/favicon.ico` serves svg bytes, which every current browser accepts because the
        content type is what it honours — but a `.svg` url is what a `<link rel="icon">`
        should point at, and some tooling sniffs the extension rather than the header.
        """
        return Response(_FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            VIEWER_INDEX,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/state")
    async def state() -> JSONResponse:
        return JSONResponse(
            app.state.simulation.snapshot(),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

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
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    # --- chain identity ------------------------------------------------------
    #
    # Linking a wallet is a label, not a privilege. Nothing in src/world reads
    # `owner_address` to decide what an agent does, and nothing may: ownership without
    # in-world advantage is a settled decision (NEXT.md, "do not reopen"). These routes
    # exist so a spectator can prove which deployed agents are theirs — which is also
    # what a future ownership check on rest/delete will bind to.

    async def _spend_for_decision(world, proof: PaymentProof, clan_id: int) -> Spend:
        """Take a payment and queue a forced decision. The whole guard, in one place.

        Both the http route and the a2a skill go through here. A second copy of this
        would be a second replay guard to keep correct, and the ownership predicate
        already taught this codebase what happens when the same question gets answered
        in more than one place.

        Returns a `Spend`, whose truthiness *is* whether the payment was taken — see the
        class for why it is not a plain string.
        """
        verifier = app.state.x402
        entity = world.first(ForceDecisionQueue)
        if entity is None:
            return Spend("unavailable", "world is not ready")
        queue_comp = world.get(entity, ForceDecisionQueue)

        # Check spent first — no point verifying what has already been honoured.
        #
        # Both sets are consulted. The verifier's lives in memory and dies with the
        # process; `queue_comp.spent` is world state and survives on the volume. Reading
        # only the first meant every restart made every past payment spendable again, and
        # a transaction hash is public on the explorer by design — so anyone could copy
        # one and buy decisions forever. This happened: a proof spent at tick 55604 was
        # honoured again at tick 100426, after a redeploy.
        fingerprint = proof.fingerprint()
        if verifier.is_spent(proof) or (fingerprint and fingerprint in queue_comp.spent):
            return Spend("replayed", "this payment has already been used")

        # Claim it now, before the await. Verification is a network round trip, and
        # requests that arrive during it would all pass the check above and all be
        # honoured — one payment bought five clan decisions in the same tick that way.
        # Nothing else awaits between here and the check, so the claim is atomic.
        if fingerprint:
            _remember_spent(queue_comp, fingerprint, world.config.x402_spent_limit)

        verified = False
        try:
            verified = await verifier.verify(
                proof, world.config.x402_price, world.config.x402_currency
            )
        except Exception as error:  # noqa: BLE001 - a verifier failure is a denial
            logger.warning("x402 verifier failed for clan %d: %r", clan_id, error)

        if not verified:
            if fingerprint and fingerprint in queue_comp.spent:
                # Give the claim back: an unverified proof never bought anything, and
                # holding it would let one bad request permanently burn a hash somebody
                # may go on to pay with for real.
                queue_comp.spent.remove(fingerprint)
            return Spend("unverified", "payment could not be verified")

        # The durable claim was taken before verifying; this is the in-memory twin, which
        # is what keeps the check cheap for the overwhelming majority of requests.
        verifier.record_spent(proof)

        # Cap the queue so the seam cannot be used as an unbounded accumulator.
        if len(queue_comp.pending) >= 16:
            queue_comp.pending.pop(0)
        queue_comp.pending.append(
            {
                "clan_id": clan_id,
                "tick": world.tick,
                "proof": fingerprint,
                # What was actually charged. The world used to record only that something
                # was paid, so a receipt could never say how much and the viewer had to
                # assume the configured price was still the price.
                "amount": world.config.x402_price,
                "currency": world.config.x402_currency,
            }
        )
        return Spend("queued")

    # --- a2a ------------------------------------------------------------------------
    #
    # Everything below this world was already reachable over http, which serves a person
    # reading the readme and nobody else's agent. The card makes the same actions
    # discoverable without a human in between: fetch a well-known url, learn the skills
    # and what they cost, call them.

    def _base_url(request: Request) -> str:
        # Behind a proxy the request url is the internal one, so the forwarded headers
        # win where present. A card that advertises http://0.0.0.0:8080 is discoverable
        # by nothing.
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if not host:
            host = request.url.netloc
        return f"{proto}://{host}"

    async def _agent_card(request: Request) -> JSONResponse:
        world = app.state.simulation.world
        card = a2a.agent_card(
            base_url=_base_url(request),
            config=world.config,
            world_ready=app.state.simulation is not None,
        )
        return JSONResponse(card, headers={"Cache-Control": "public, max-age=300"})

    # Both paths: 0.3 renamed the file and clients in the wild still ask for the old one.
    # Answering only the current name makes discovery fail for a caller that is not wrong,
    # merely older.
    app.add_api_route("/.well-known/agent-card.json", _agent_card, methods=["GET"])
    app.add_api_route("/.well-known/agent.json", _agent_card, methods=["GET"])

    @app.post("/a2a")
    async def a2a_rpc(request: Request) -> JSONResponse:
        """JSON-RPC 2.0, `message/send`. One method, because one method is what the
        skills here need — a task that finishes within the request or asks for payment.

        Skills resolve to the same code the http routes use. An agent and a browser must
        not be able to get different answers out of the same world, and a second
        implementation of the payment guard is exactly how that starts.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a malformed body is a normal client error
            return JSONResponse(a2a.rpc_error(None, -32700, "parse error"), status_code=400)

        request_id = body.get("id")
        if body.get("jsonrpc") != "2.0":
            return JSONResponse(a2a.rpc_error(request_id, -32600, "jsonrpc must be 2.0"))
        method = body.get("method")
        if method != "message/send":
            return JSONResponse(
                a2a.rpc_error(request_id, -32601, f"unsupported method: {method}")
            )

        params = body.get("params") or {}
        message = params.get("message") or {}
        task_id = str(params.get("taskId") or secrets.token_hex(8))
        context_id = str(params.get("contextId") or secrets.token_hex(8))

        arguments = a2a.data_of(message)
        skill = str(arguments.get("skill") or "").strip()
        if not skill:
            # A caller that sent only prose gets told what exists rather than guessed at.
            return JSONResponse(
                a2a.rpc_result(
                    request_id,
                    a2a.failed_task(
                        task_id=task_id,
                        context_id=context_id,
                        text=(
                            "Send a data part with a 'skill' field. Available: "
                            + ", ".join(a2a.FREE_SKILLS + a2a.PAID_SKILLS)
                        ),
                    ),
                )
            )

        world = app.state.simulation.world

        if skill == "observe-world":
            snapshot = app.state.simulation.snapshot()
            summary = {
                "day": snapshot["day"],
                "tick": snapshot["tick"],
                "stats": snapshot["stats"],
                "advisor": snapshot["advisor"],
                "clans": snapshot["clans"],
            }
            return JSONResponse(
                a2a.rpc_result(
                    request_id,
                    a2a.completed_task(
                        task_id=task_id,
                        context_id=context_id,
                        text=(
                            f"Day {snapshot['day']}: {snapshot['stats']['agents']} agents "
                            f"in {snapshot['stats']['clans']} clans."
                        ),
                        data=summary,
                    ),
                )
            )

        if skill == "read-decisions":
            current = leadership.log(world)
            entries = list(current.entries) if current is not None else []
            return JSONResponse(
                a2a.rpc_result(
                    request_id,
                    a2a.completed_task(
                        task_id=task_id,
                        context_id=context_id,
                        text=f"{len(entries)} recent decisions.",
                        data={"decisions": entries},
                    ),
                )
            )

        if skill in ("observe-my-agent", "steer-my-agent"):
            # The whole of #22's transport. The mind calls in, so the world never makes a
            # request to an address a stranger chose — no forgery to defend against, no
            # dns to re-resolve, no redirect to follow, nothing to pin.
            if not world.config.attached_minds_enabled:
                return JSONResponse(
                    a2a.rpc_result(
                        request_id,
                        a2a.failed_task(
                            task_id=task_id,
                            context_id=context_id,
                            text="attached minds are not enabled on this world.",
                        ),
                    )
                )
            try:
                agent_id = int(arguments.get("agent_id"))
            except (TypeError, ValueError):
                agent_id = -1
            mind = world.try_get(agent_id, AttachedMind) if agent_id >= 0 else None
            token = str(arguments.get("token") or "")
            # One message for a bad id and a bad token alike: telling a caller which agents
            # exist is a way of enumerating them.
            if mind is None or not mind.enabled or not minds.token_matches(mind, token):
                return JSONResponse(
                    a2a.rpc_result(
                        request_id,
                        a2a.failed_task(
                            task_id=task_id,
                            context_id=context_id,
                            text="no mind is attached to that agent with that token.",
                        ),
                    )
                )

            if skill == "observe-my-agent":
                view = minds.perception(world, agent_id)
                return JSONResponse(
                    a2a.rpc_result(
                        request_id,
                        a2a.completed_task(
                            task_id=task_id,
                            context_id=context_id,
                            text=f"{view.get('name', 'your agent')} is {view.get('state', 'somewhere')}.",
                            data=view,
                        ),
                    )
                )

            queued = minds.enqueue_steer(
                world, agent_id, wants=arguments.get("wants", ""), say=arguments.get("say", "")
            )
            return JSONResponse(
                a2a.rpc_result(
                    request_id,
                    a2a.completed_task(
                        task_id=task_id,
                        context_id=context_id,
                        # Queued, not applied: a steer is an input to the simulation, and
                        # applying it when the request lands would make history depend on
                        # wall clock.
                        text="queued for the next tick." if queued else "the world is not ready.",
                        data={"agent_id": agent_id, "queued": queued, "tick": world.tick},
                    ),
                )
            )

        if skill == "nudge-clan":
            if not (world.config.x402_enabled and world.config.llm_force_decision_enabled):
                return JSONResponse(
                    a2a.rpc_result(
                        request_id,
                        a2a.failed_task(
                            task_id=task_id,
                            context_id=context_id,
                            text="Paid decisions are not enabled on this world.",
                        ),
                    )
                )
            try:
                clan_id = int(arguments.get("clan_id"))
            except (TypeError, ValueError):
                return JSONResponse(
                    a2a.rpc_result(
                        request_id,
                        a2a.failed_task(
                            task_id=task_id,
                            context_id=context_id,
                            text="nudge-clan needs an integer 'clan_id'.",
                        ),
                    )
                )

            payload = a2a.payment_payload(message)
            proof = PaymentProof(
                tx_hash=str(payload.get("transaction") or payload.get("txHash") or ""),
                header_value=str(payload.get("verified") or ""),
            )
            if not proof.fingerprint():
                return JSONResponse(
                    a2a.rpc_result(
                        request_id,
                        a2a.payment_required_task(
                            task_id=task_id,
                            context_id=context_id,
                            skill=skill,
                            config=world.config,
                            recipient=chain_settings.x402_recipient(),
                            asset=chain_settings.usdg_address(testnet=False),
                        ),
                    )
                )

            # Compared against "queued" explicitly. The helper returns an outcome *string*,
            # and every non-success value is truthy — reading it as a boolean made a
            # replayed payment come back `completed`, which is the hole the http route
            # had closed hours earlier.
            spend = await _spend_for_decision(world, proof, clan_id)
            if not spend:
                return JSONResponse(
                    a2a.rpc_result(
                        request_id,
                        a2a.failed_task(
                            task_id=task_id, context_id=context_id, text=spend.detail
                        ),
                    )
                )
            return JSONResponse(
                a2a.rpc_result(
                    request_id,
                    a2a.completed_task(
                        task_id=task_id,
                        context_id=context_id,
                        text=f"Clan {clan_id} will decide again next tick.",
                        data={
                            "clan_id": clan_id,
                            "applies_at_tick": world.tick + 1,
                            a2a.PAYMENT_STATUS: "payment-completed",
                        },
                    ),
                )
            )

        return JSONResponse(
            a2a.rpc_result(
                request_id,
                a2a.failed_task(
                    task_id=task_id, context_id=context_id, text=f"unknown skill: {skill}"
                ),
            )
        )

    @app.get("/chain/config")
    async def chain_config() -> JSONResponse:
        """What the viewer needs to render a connect button, and whether it should.

        `ready` is deliberately conservative: identity has to be enabled *and* the
        signature library actually importable, so an operator who enabled the flag
        without installing eth-account sees a disabled button rather than a dead one.
        """
        world_cfg = app.state.simulation.world.config
        return JSONResponse(
            {
                "chain_id": world_cfg.chain_id,
                "chain_name": world_cfg.chain_name,
                "explorer_url": world_cfg.chain_explorer_url,
                "identity_enabled": world_cfg.chain_identity_enabled,
                "signature_verification_available": siwe.available(),
                "ready": world_cfg.chain_identity_enabled and siwe.available(),
                "x402_enabled": world_cfg.x402_enabled,
                "x402_price": world_cfg.x402_price,
                "x402_currency": world_cfg.x402_currency,
                # Stated plainly so the viewer never has to guess whether money is real.
                "real_money_enabled": world_cfg.real_money_enabled,
                "markets_are_demo": not world_cfg.real_money_enabled,
            },
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.post("/chain/nonce")
    async def chain_nonce(request: NonceRequest, http_request: Request) -> JSONResponse:
        """Issue a single-use challenge for the wallet to sign.

        `http_request` is the http request, distinct from the pydantic body — the message
        has to name the host the browser is actually on.
        """
        world_cfg = app.state.simulation.world.config
        if not world_cfg.chain_identity_enabled:
            raise HTTPException(403, "wallet identity is not enabled")

        address = request.address.strip()
        if address and not siwe.is_address(address):
            raise HTTPException(422, "address is not a well-formed 0x address")

        challenge = app.state.nonces.issue(address)
        # The domain has to be the site the user is actually on. eip-4361 binds a signature
        # to a domain, and a wallet compares the one in the message against the page that
        # asked — a mismatch is what a phishing site looks like, so metamask warns and the
        # user cancels. hardcoding "nous" meant every deployment except a host literally
        # called "nous" produced a message the wallet distrusted.
        origin = http_request.headers.get("origin", "")
        host = origin.split("://", 1)[-1] if origin else (http_request.url.hostname or "nous")
        scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
        message = siwe.build_message(
            domain=host,
            address=address or "0x…",
            nonce=challenge.nonce,
            chain_id=world_cfg.chain_id,
            uri=f"{scheme}://{host}",
        )
        return JSONResponse(
            {"nonce": challenge.nonce, "message": message, "chain_id": world_cfg.chain_id},
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.post("/chain/verify")
    async def chain_verify(request: VerifyRequest) -> JSONResponse:
        """Recover the signer from the signature and open a session.

        The claimed address is never trusted on its own — it only has to *match* what the
        signature recovers to. The nonce is consumed here, so one challenge opens at most
        one session.
        """
        world_cfg = app.state.simulation.world.config
        if not world_cfg.chain_identity_enabled:
            raise HTTPException(403, "wallet identity is not enabled")
        if not siwe.available():
            raise HTTPException(503, "signature verification is unavailable on this server")
        if not siwe.is_address(request.address):
            raise HTTPException(422, "address is not a well-formed 0x address")

        nonce = _nonce_in(request.message)
        if not nonce:
            raise HTTPException(422, "message does not carry a nonce")
        if app.state.nonces.consume(nonce) is None:
            raise HTTPException(401, "challenge is unknown or expired")
        if not siwe.verify(request.message, request.signature, request.address):
            raise HTTPException(401, "signature does not match the claimed address")

        address = siwe.normalise(request.address)
        token = app.state.sessions.open(address)
        return JSONResponse(
            {
                "address": address,
                "session": token,
                "agents": identity.agents_owned_by(app.state.simulation.world, address),
            },
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.post("/agents/{agent_id}/link", status_code=202)
    async def link_agent(agent_id: int, request: LinkRequest) -> JSONResponse:
        """Attach a proven wallet address to a deployed agent, applied on the next tick.

        Refuses to relabel an agent that already carries a *different* address, so a
        second visitor cannot take someone else's agent by linking over it.
        """
        world = app.state.simulation.world
        if not world.config.chain_identity_enabled:
            raise HTTPException(403, "wallet identity is not enabled")

        proven = app.state.sessions.address(request.session)
        if not proven:
            raise HTTPException(401, "session is unknown or expired")
        if proven != siwe.normalise(request.address):
            raise HTTPException(403, "session does not own that address")

        agent = world.try_get(agent_id, Agent)
        if agent is None or not agent.user_deployed:
            raise HTTPException(404, f"agent {agent_id} is not a deployed agent")
        if agent.owner_address and agent.owner_address != proven:
            raise HTTPException(409, f"agent {agent_id} is already linked to another wallet")

        identity.enqueue(world, agent_id, proven)
        return JSONResponse(
            {
                "queued": True,
                "agent_id": agent_id,
                "address": proven,
                "applies_at_tick": world.tick + 1,
            },
            status_code=202,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
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
            },
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
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
            },
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
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
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
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
                    "rest_mode": agent.rest_mode,
                }
            )
        pending = spawning.queue(world)
        return JSONResponse(
            {
                "agents": cards,
                "pending": list(pending.pending) if pending is not None else [],
                "limit": world.config.max_user_agents,
            },
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
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

        # An agent deployed by somebody who proved a wallet belongs to them from the
        # moment it exists. Claiming afterwards left a window — however short — in which
        # a stranger could claim it first, and "unclaimed means anyone may claim it" is
        # only safe while nobody else is watching.
        owner = ""
        if request.session and world.config.chain_identity_enabled:
            owner = app.state.sessions.address(request.session)
            if owner:
                identity.enqueue_by_name(world, name, owner)

        return JSONResponse(
            {
                "queued": True,
                "owned": bool(owner),
                "name": name,
                "personality": personality,
                "arrives_at_tick": world.tick + 1,
            },
            status_code=202,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    def _authorize_agent(agent: Agent, agent_id: int, http_request: Request) -> None:
        """Only the linked owner may control a linked agent.

        Unlinked agents stay open to anyone, which is what keeps the demo playable
        without a wallet. The moment an agent carries an address, that address is the
        only thing allowed to rest or delete it.
        """
        world = app.state.simulation.world
        if not world.config.chain_identity_enabled or not agent.owner_address:
            return
        token = http_request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        proven = app.state.sessions.address(token) if token else ""
        if proven != agent.owner_address:
            raise HTTPException(403, f"agent {agent_id} is linked to another wallet")

    @app.post("/agents/{agent_id}/mind", status_code=201)
    async def attach_mind(agent_id: int, http_request: Request) -> JSONResponse:
        """Attach a mind to an agent, and hand back the token that drives it.

        The token is returned exactly once and stored only as a sha256. World state is
        saved to disk and shipped in backups, and a credential that exists only as a hash
        cannot leak from either.

        Nothing here lets the world call out. The mind uses this token against /a2a to ask
        what its agent sees and to say what it should want — so there is no url for a
        visitor to supply, and therefore no request for anybody to forge.
        """
        world = app.state.simulation.world
        if not world.config.attached_minds_enabled:
            raise HTTPException(403, "attached minds are not enabled")

        agent = world.try_get(agent_id, Agent)
        if agent is None or not agent.user_deployed:
            raise HTTPException(404, f"agent {agent_id} is not a deployed agent")
        _authorize_agent(agent, agent_id, http_request)

        token = secrets.token_urlsafe(32)
        existing = world.try_get(agent_id, AttachedMind)
        if existing is None:
            world.add(agent_id, AttachedMind(owner=agent.owner_address, token_hash=minds.hash_token(token)))
        else:
            # Re-issuing revokes the previous token, which is the only way to rotate one
            # that leaked.
            existing.owner = agent.owner_address
            existing.token_hash = minds.hash_token(token)
            existing.enabled = True

        return JSONResponse(
            {
                "agent_id": agent_id,
                "token": token,
                "endpoint": "/a2a",
                "skills": ["observe-my-agent", "steer-my-agent"],
                "note": "shown once; re-attach to rotate",
            },
            status_code=201,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.delete("/agents/{agent_id}/mind")
    async def detach_mind(agent_id: int, http_request: Request) -> JSONResponse:
        world = app.state.simulation.world
        agent = world.try_get(agent_id, Agent)
        if agent is None or not agent.user_deployed:
            raise HTTPException(404, f"agent {agent_id} is not a deployed agent")
        _authorize_agent(agent, agent_id, http_request)

        mind = world.try_get(agent_id, AttachedMind)
        if mind is not None:
            mind.enabled = False
            mind.token_hash = ""
        return JSONResponse({"detached": True, "agent_id": agent_id})

    @app.post("/agents/{agent_id}/rest", status_code=202)
    async def set_rest(
        agent_id: int, request: RestRequest, http_request: Request
    ) -> JSONResponse:
        """Queue a rest-mode toggle for a user-deployed agent. Applied on the next tick.

        When identity is enabled and the agent is linked, only its owner may toggle it.
        """
        world = app.state.simulation.world
        agent = world.try_get(agent_id, Agent)
        if agent is None or not agent.user_deployed:
            raise HTTPException(404, f"agent {agent_id} is not a deployed agent")
        _authorize_agent(agent, agent_id, http_request)

        if world.first(RestQueue) is None:
            raise HTTPException(503, "world is not ready")
        resting.enqueue(world, agent_id, request.rest)
        return JSONResponse(
            {
                "queued": True,
                "agent_id": agent_id,
                "rest": request.rest,
                "applies_at_tick": world.tick + 1,
            },
            status_code=202,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.delete("/agents/{agent_id}", status_code=200)
    async def delete_agent(agent_id: int, http_request: Request) -> JSONResponse:
        """Remove a user-deployed agent from the world.

        This is an administrative seam for cleaning up test agents; it does not
        represent a simulation event. The agent is removed from any clan and then
        destroyed. Markets opened on the agent's survival resolve as NO.

        When identity is enabled and the agent is linked, only its owner may delete it —
        deletion is irreversible, so this is the check that matters most.
        """
        world = app.state.simulation.world
        agent = world.try_get(agent_id, Agent)
        if agent is None or not agent.user_deployed:
            raise HTTPException(404, f"agent {agent_id} is not a deployed agent")
        _authorize_agent(agent, agent_id, http_request)

        for entity, clan in world.store(Clan):
            clan.remove(agent_id)
        world.destroy_entity(agent_id)
        return JSONResponse(
            {"deleted": True, "agent_id": agent_id},
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.post("/clans/{clan_id}/force-decision", status_code=202)
    async def force_decision(clan_id: int, request: Request) -> JSONResponse:
        """x402 pay-to-force-decision: verify payment, then queue the request.

        The verifier checks proof against the configured price and recipient. A verified
        payment is remembered so the same proof cannot buy two decisions — the spent set
        is bounded by `x402_spent_limit` and lives in `ForceDecisionQueue.spent`.
        """
        world = app.state.simulation.world
        if not world.config.llm_force_decision_enabled:
            raise HTTPException(403, "force decision is not enabled")
        if not world.config.x402_enabled:
            raise HTTPException(403, "x402 is not enabled")

        proof = PaymentProof(
            tx_hash=request.headers.get("X402-Transaction-Hash", ""),
            header_value=request.headers.get("X402-Payment-Verified", ""),
        )

        spend = await _spend_for_decision(world, proof, clan_id)
        if spend.outcome == "unavailable":
            raise HTTPException(503, spend.detail)
        if spend.outcome == "replayed":
            raise HTTPException(409, spend.detail)

        if not spend:
            # Everything a wallet needs to construct the payment itself. Price and
            # currency alone are not actionable: without the recipient, the token
            # contract and the chain id, a client cannot build the transfer, and the
            # 402 is a locked door with no keyhole.
            recipient = chain_settings.x402_recipient()
            token = chain_settings.usdg_address(testnet=False)
            raise HTTPException(
                status_code=402,
                detail={
                    "payment": {
                        "scheme": "x402",
                        "network": world.config.chain_name,
                        "chain_id": world.config.chain_id,
                        "amount": world.config.x402_price,
                        "currency": world.config.x402_currency,
                        # Integer token units — what transfer() actually takes. Sending
                        # the decimal string alone invites a client to guess at decimals.
                        "amount_units": str(
                            price_units(world.config.x402_price, chain_settings.USDG_DECIMALS)
                        ),
                        "decimals": chain_settings.USDG_DECIMALS,
                        "recipient": recipient,
                        "token": token,
                        # False means the operator has not finished configuring payment;
                        # the viewer shows why rather than offering a button that cannot work.
                        "payable": bool(recipient and token),
                    },
                    "clan_id": clan_id,
                },
                headers={
                    "X402-Payment-Required": "true",
                    "X402-Version": "0.1",
                    "X402-Price": world.config.x402_price,
                    "X402-Currency": world.config.x402_currency,
                    "X402-Chain-Id": str(world.config.chain_id),
                    "X402-Recipient": recipient,
                    "X402-Token": token,
                },
            )

        return JSONResponse(
            {
                "queued": True,
                "clan_id": clan_id,
                "applies_at_tick": world.tick + 1,
            },
            status_code=202,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
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
