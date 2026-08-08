"""Phase 0 components.

Needs carries all four drives named in planish/AGENT_SYSTEM.md. Phase 0 only
decays energy and hunger; social and safety are stored so the FSM can start
consuming them in Phase 1 without a schema change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .ecs import Entity


class AgentState(str, Enum):
    IDLE = "IDLE"
    SEEK_NEED = "SEEK_NEED"
    GATHER = "GATHER"
    BUILD = "BUILD"
    REST = "REST"
    FLEE = "FLEE"
    FOLLOW = "FOLLOW"
    MEET = "MEET"


class MessageType(str, Enum):
    INFO = "info"
    REQUEST = "request"
    OFFER = "offer"
    ALERT = "alert"


BROADCAST_CLAN = "clan"
BROADCAST_ALL = "all"


class ResourceKind(str, Enum):
    FOOD = "FOOD"
    WOOD = "WOOD"


@dataclass
class Position:
    x: int
    y: int


@dataclass
class Needs:
    energy: int
    hunger: int
    social: int
    safety: int


@dataclass
class Inventory:
    food: int = 0
    wood: int = 0

    def total(self) -> int:
        return self.food + self.wood


@dataclass
class ClanRef:
    clan_id: int | None = None


@dataclass
class Agent:
    name: str
    state: AgentState = AgentState.IDLE
    target_entity: Entity | None = None
    target_x: int | None = None
    target_y: int | None = None
    gather_progress: int = 0
    wants: ResourceKind = ResourceKind.FOOD
    huts_owned: int = 0
    last_request_tick: int = -1
    received: int = 0
    personality: str = ""
    user_deployed: bool = False
    rest_mode: bool = False
    raids_won: int = 0
    raids_lost: int = 0
    spawn_tick: int = 0
    # A linked wallet address, lowercased, or "" for the overwhelming majority of agents.
    # This is a *label*: nothing in src/world/systems reads it, and nothing may. Ownership
    # is not advantage — a registered agent starves exactly like the rest (NEXT.md, "do
    # not reopen"). It lives on the component rather than in a side table so it survives
    # a save without a second persistence path.
    owner_address: str = ""

    def clear_target(self) -> None:
        self.target_entity = None
        self.target_x = None
        self.target_y = None
        self.gather_progress = 0


@dataclass
class Standing:
    """Reputation of an agent. Only real recorded actions increase this; it is never
    granted for free. Stored as durable world state and bounded by the thresholds in
    WorldConfig."""

    value: int = 0
    action_value: int = 0  # standing from actions, separate from time-based portion
    rank: str = "member"  # member | trusted | officer
    given_food: int = 0
    given_wood: int = 0
    join_tick: int = -1   # tick the agent joined its current clan; -1 if clanless


@dataclass
class ResourceNode:
    """Nodes are never destroyed. A fully foraged node goes dormant at amount 0
    and climbs back toward max_amount, one unit every regrow_every ticks."""

    kind: ResourceKind
    amount: int
    max_amount: int
    regrow_every: int

    @property
    def is_dormant(self) -> bool:
        return self.amount <= 0


@dataclass
class Building:
    kind: str
    owner: Entity | None = None


class ClanGoal(str, Enum):
    GATHER_FOOD = "gather_food"
    GATHER_WOOD = "gather_wood"
    EXPAND = "expand"
    RALLY = "rally"
    RAID = "raid"


KEY_FOOD_LOCATIONS = "food_locations"
KEY_WOOD_LOCATIONS = "wood_locations"


def clan_rally_key(clan_id: int) -> str:
    return f"clan:{clan_id}:rally"


def clan_goal_key(clan_id: int) -> str:
    return f"clan:{clan_id}:goal"


def clan_centre_key(clan_id: int) -> str:
    return f"clan:{clan_id}:centre"


@dataclass
class Blackboard:
    """Global key-value store, held by a single world entity.

    Each entry is [value, written_by, written_tick]. The tick stamp is what the
    blackboard system uses to expire stale knowledge — a food sighting from 400 ticks
    ago is worse than no sighting at all.
    """

    entries: dict[str, list] = field(default_factory=dict)

    def write(self, key: str, value: object, written_by: Entity, tick: int) -> None:
        # Keys are kept in sorted order. A save round-trips through
        # json.dumps(sort_keys=True), so insertion order would otherwise differ between
        # a live world and a reloaded one — identical content, different iteration.
        is_new = key not in self.entries
        self.entries[key] = [value, written_by, tick]
        if is_new:
            self.entries = dict(sorted(self.entries.items()))

    def read(self, key: str) -> object | None:
        entry = self.entries.get(key)
        return entry[0] if entry is not None else None

    def written_by(self, key: str) -> Entity | None:
        entry = self.entries.get(key)
        return entry[1] if entry is not None else None

    def age(self, key: str, tick: int) -> int | None:
        entry = self.entries.get(key)
        return tick - entry[2] if entry is not None else None

    def expire(self, tick: int, ttl: int) -> list[str]:
        stale = sorted(key for key, e in self.entries.items() if tick - e[2] > ttl)
        for key in stale:
            del self.entries[key]
        return stale


@dataclass
class AdvisorState:
    """Durable advisor bookkeeping. This is world state, not runtime state.

    Money already spent is a fact about the world, not about the process that spent it.
    Keeping `calls_made` on the in-memory advisor meant a restart silently reset the
    session budget to zero, so a crash loop could spend without limit. And an in-flight
    request vanished on reload while the clan's `last_advisor_tick` persisted — so the
    request was never answered *and* never retried.

    Both counters live here now, saved and reloaded with everything else.
    """

    calls_made: int = 0
    pending: list[dict] = field(default_factory=list)

    def is_pending(self, clan_id: int) -> bool:
        return any(entry["clan_id"] == clan_id for entry in self.pending)

    def add_pending(self, clan_id: int, tick: int) -> None:
        if not self.is_pending(clan_id):
            self.pending.append({"clan_id": clan_id, "tick": tick, "attempts": 1})

    def drop_pending(self, clan_id: int) -> None:
        self.pending = [e for e in self.pending if e["clan_id"] != clan_id]


@dataclass
class DecisionLog:
    """Every leader decision, rule-based or model-based, for inspection.

    Bounded: an append-only log inside a 24/7 world is an unbounded accumulator, and
    those have eaten this simulation more than once.
    """

    entries: list[dict] = field(default_factory=list)

    def record(self, entry: dict, limit: int) -> None:
        self.entries.append(entry)
        if limit > 0 and len(self.entries) > limit:
            del self.entries[: len(self.entries) - limit]


@dataclass
class MessageLog:
    """Recent messages delivered in the world, for the viewer to show as captions.

    Bounded: the inbox itself is bounded, but a separate log is needed because messages
    are consumed within the same tick they arrive.
    """

    entries: list[dict] = field(default_factory=list)

    def record(self, entry: dict, limit: int) -> None:
        self.entries.append(entry)
        if limit > 0 and len(self.entries) > limit:
            del self.entries[: len(self.entries) - limit]


@dataclass
class SpawnQueue:
    """Deployment requests waiting to enter the world, held by a single world entity.

    A user deploying an agent is an *input* to the simulation, so it cannot be applied
    the moment an http request lands — that would make history depend on wall clock.
    Requests queue here and are drained at a fixed point in the tick, with position and
    needs drawn from the tick's rng stream. Same seed plus same deployments at the same
    ticks reproduces the same world.
    """

    pending: list[dict] = field(default_factory=list)


@dataclass
class Inbox:
    """Messages delivered at the start of this tick. Bounded: an unread inbox must
    never become an unbounded accumulator."""

    messages: list[dict] = field(default_factory=list)


@dataclass
class Outbox:
    """Messages queued this tick, delivered at the start of the next one."""

    messages: list[dict] = field(default_factory=list)


@dataclass
class Clan:
    clan_id: int
    leader: Entity | None = None
    members: list[Entity] = field(default_factory=list)
    last_rally: list | None = None
    goal: str = ClanGoal.RALLY.value
    goal_set_tick: int = 0
    goal_source: str = "rules"
    goal_reason: str = ""
    last_advisor_tick: int = -1
    # Who has robbed us: "attacker_clan_id" -> [times_raided, last_raid_tick].
    # Keys are strings because this round-trips through json, and kept sorted for the
    # same reason the blackboard is — a reloaded world must iterate identically.
    # Naturally bounded by the number of clans, so it needs no eviction.
    grudges: dict[str, list] = field(default_factory=dict)
    # Clans we have a truce with, sorted. Recorded on both sides, so there is no
    # asymmetric "offered" state. Grudges are kept even after a truce — the memory of
    # the raid outlives the fighting.
    allies: list[int] = field(default_factory=list)

    def record_raid(self, attacker_clan_id: int, tick: int) -> None:
        key = str(attacker_clan_id)
        is_new = key not in self.grudges
        times = self.grudges.get(key, [0, -1])[0]
        self.grudges[key] = [times + 1, tick]
        if is_new:
            self.grudges = dict(sorted(self.grudges.items(), key=lambda kv: int(kv[0])))

    def grudge_against(self, clan_id: int | None) -> int:
        """How many times that clan has robbed us. 0 for strangers and the clanless."""
        if clan_id is None:
            return 0
        entry = self.grudges.get(str(clan_id))
        return entry[0] if entry else 0

    def last_raided_by(self, clan_id: int) -> int:
        entry = self.grudges.get(str(clan_id))
        return entry[1] if entry else -1

    def is_allied(self, clan_id: int | None) -> bool:
        return clan_id is not None and clan_id in self.allies

    def add_ally(self, clan_id: int) -> None:
        """Idempotent and sorted. The caller records it on both clans — a truce has no
        one-sided state, so there is nothing pending to persist or time out."""
        if clan_id not in self.allies:
            self.allies.append(clan_id)
            self.allies.sort()
    centre: list | None = None
    influence_radius: int = 0

    def add(self, entity: Entity) -> None:
        if entity not in self.members:
            self.members.append(entity)
            self.members.sort()
        if self.leader is None:
            self.leader = entity

    def remove(self, entity: Entity) -> None:
        if entity in self.members:
            self.members.remove(entity)
        if self.leader == entity:
            self.leader = self.members[0] if self.members else None


@dataclass
class RestQueue:
    """User rest-mode requests waiting for a fixed tick to be applied.

    A spectator toggling their agent's rest state is an input to the world, not a
    simulation event, so it queues and is drained at a fixed point in the tick.
    """

    pending: list[dict] = field(default_factory=list)


@dataclass
class ForceDecisionQueue:
    """Paid pay-to-force-decision requests waiting for a fixed tick.

    The api verifies payment and appends; the world applies the *recorded* request on a
    later tick. That ordering is the whole point: what history depends on is this queue
    entry, never the http request or the rpc call that produced it, so a replay does not
    need the network and a settlement that arrives late still lands at a definite tick.

    `spent` remembers proofs that have already been honoured so the same payment cannot
    buy two decisions. Bounded by `x402_spent_limit`.

    `applied` is the receipt: what a payment actually bought, kept so a spectator can see
    that somebody paid and check the transaction themselves. Bounded like everything else
    here — an append-only log inside a 24/7 world is an unbounded accumulator.
    """

    pending: list[dict] = field(default_factory=list)
    spent: list[str] = field(default_factory=list)
    applied: list[dict] = field(default_factory=list)


@dataclass
class IdentityQueue:
    """Wallet-link requests waiting for a fixed tick to be applied.

    Linking is an input to the world in exactly the way a deployment or a rest toggle is.
    The signature is verified at the api boundary — nothing inside the simulation touches
    a network or a curve — and only the verified outcome queues here.

    Bounded by `identity_queue_limit`.
    """

    pending: list[dict] = field(default_factory=list)


@dataclass
class EscrowBook:
    """Real-money deposits and payout intents, held by a single world entity.

    Deliberately inert unless `real_money_enabled`. The demo credit book (`MarketBook`)
    is untouched by this and stays demo — the two never share a balance.

    This records *intent*, and only ever from settled world state. It never sends value:
    a payout is an entry an operator's settlement job reads and marks paid, because a
    transfer inside the tick loop would put the network on the critical path of history.
    """

    deposits: dict[str, int] = field(default_factory=dict)
    payouts: list[dict] = field(default_factory=list)
    next_id: int = 1


@dataclass
class MarketBook:
    """Prediction markets on simulation events, held by a single world entity.

    A bet is an *input* to the simulation in exactly the way a deployment is: applying it
    the moment an http request lands would make history depend on wall clock. Positions
    queue in `pending` and are drained at a fixed point in the tick, so the same seed plus
    the same bets at the same ticks rebuilds the same book.

    This component is read-write for the markets system and read-only for everything else.
    Nothing here may write to an agent or a clan — spectators watch the world, they do not
    move it.

    `markets` holds both open and settled entries so a settled market keeps its audit
    trail. It is bounded by `market_history_limit`, because every unbounded accumulator in
    this project has eventually eaten the simulation.
    """

    markets: list[dict] = field(default_factory=list)
    # Positions waiting for the next tick. Same shape and same reason as SpawnQueue.
    pending: list[dict] = field(default_factory=list)
    # user -> credits. Demo money; there is no real currency anywhere in this system.
    balances: dict[str, int] = field(default_factory=dict)
    next_id: int = 1
    last_scheduled_tick: int = 0


@dataclass
class SpendBook:
    """Real money the world is allowed to spend on its own thinking, and the governor
    that stands between an agent's intent and the wallet.

    Everything else in this file moves numbers that only mean something inside the
    simulation. This one moves money that leaves it, autonomously, while nobody is
    watching — so it is built as a permission system first and a ledger second.

    Three independent brakes, because a single number is a limit and not a control:

    - `budget_units` is an *envelope* an operator approved. Agents spend freely inside
      it and nothing at all outside it. Raising it is a deliberate act, recorded in
      `approvals`, which is why a runaway costs exactly one envelope rather than a card.
    - `per_tick_units` stops a burst. Without it a single tick could drain the whole
      envelope before any notice is read, which makes the envelope decorative.
    - `halted` is the kill switch. Set once, spending stops, and nothing but an operator
      clears it. It is checked before the budget so a halt beats any amount of headroom.

    `notices` is what makes this different from caps alone: threshold crossings are
    recorded here for the monitor to read and send on. A cap tells you how much you can
    lose. A notice tells you that you are losing it, which is the part that was missing
    when an advisor died quietly and nobody knew for two days.

    Units are the token's smallest denomination, never a float — the same reason the
    payment verifier uses Decimal. `reserved_units` holds money committed to a call that
    has not settled, so two concurrent spends cannot both fit in the same headroom.
    """

    # Off unless an operator turns it on. A deploy must never begin spending by itself.
    enabled: bool = False
    halted: bool = False
    budget_units: int = 0
    spent_units: int = 0
    reserved_units: int = 0
    per_tick_units: int = 0
    spent_this_tick: int = 0
    # Which tick `spent_this_tick` counts, so the burst cap resets without a scheduler.
    tick_of_spend: int = -1
    # agent id -> units earned in world. A claim on the pool, not a wallet: the agent
    # never holds a key, which is what keeps this a ledger rather than custody.
    #
    # Keyed by *string*, like MarketBook.balances and for the same reason: this dict is
    # persisted as json, and json has no integer keys. Keyed by int it round-tripped to
    # {"1": 500}, every lookup by agent id missed, and every agent's earnings silently
    # read as zero after a restart — money that vanishes without an error.
    balances: dict[str, int] = field(default_factory=dict)
    # Envelope changes, oldest first. The audit trail for "who approved this spend".
    approvals: list[dict] = field(default_factory=list)
    # Threshold crossings waiting to be read. Bounded like every accumulator here.
    notices: list[dict] = field(default_factory=list)
    # Settled spends, newest last. Bounded.
    spends: list[dict] = field(default_factory=list)
