# implementation plan

status as of the user-agents milestone. this file is the handover doc: a fresh session should be
able to read this plus the [root readme](../README.md) and continue without asking.

## where we are

| phase | state |
|:--|:--|
| **0 — skeleton** | ✅ complete, frozen |
| **1 — social emergence** | 🟡 nearly done: blackboard, messaging, trade, clans, goals + influence and user agents all shipped; **combat is all that remains** |
| **2 — visual upgrade** | ⬜ not started (intentionally) |
| **3 — hierarchy & scale** | ⬜ not started; spatial index pulled forward and done |
| **4 — spectator & economy** | ⬜ not started |

145 tests green. local `main`, lowercase commits, **no remote yet**.

## phase 0 — skeleton ✅

world grid, fixed-timestep tick loop, deterministic rng, sqlite save/load, websocket
viewer. agents gather, build, eat, rest and starve.

- **needs**: energy and hunger decay 1/tick. eating restores 45 hunger. resting restores
  6 energy, but is **barred while `hunger == 0`** — starvation kills by removing the only
  recovery mechanism, not by adding damage. death is the single `energy == 0` rule.
- **carry capacity** (5) is a real per-resource cap, refused at pickup, leaving the unit
  in the ground.
- **regrowth**: nodes are never destroyed. a foraged node goes dormant at 0 and recovers
  one unit every `regrow_every` ticks. this is the only sustainability mechanism.
- **building**: `max_huts_per_agent` (3) plus a global stop at
  `global_build_stop_fraction` (60%) of tiles. no decay, no upkeep.

carrying capacity settles around 98–112 agents from a start of 120, and holds across
60000 ticks. a total food blackout is lethal; a regional one is survivable and graded.

## phase 1 — social emergence 🟡

### shipped

- **blackboard** — global kv store on a single world entity. entries stamped with writer
  and tick, expired past `blackboard_ttl_ticks` (300). keys kept sorted so a live world
  and a reloaded one iterate identically.
- **messaging** — envelope is exactly `{from, to, type, content}`; `to` resolves as an
  agent id, `"clan"` or `"all"`; `type` is one of info/request/offer/alert. delivered at
  the top of the next tick and **persisting until consumed**, bounded by `inbox_capacity`.
- **transfer** — `trade.transfer()` moves food or wood between adjacent agents, bounded
  by donor stock and receiver capacity, conserving world total.
- **trade protocol** — `request` gets surplus from a clanmate in range or an `offer` from
  one out of range; `alert` makes donors dip into their reserve; `offer` sends the asker
  walking in `MEET`. sharing measurably improves survival (98 → 103 agents).
- **clans** — two idle unaffiliated agents in range found a clan, capped at
  `max_clan_size` (8). founder leads; lowest surviving id inherits; empty clans pruned.
- **user-deployed agents** — `POST /agents` with a name and a short personality note.
  queued on a `SpawnQueue` world entity and born in the `spawning` system, first in the
  tick, so a deployment is a reproducible input rather than a wall-clock event. bounded by
  `max_pending_spawns`, `max_user_agents` and length caps.
- **clan goals + soft influence** — one active goal per clan
  (`gather_food`/`gather_wood`/`expand`/`rally`), reviewed every `goal_review_ticks` and
  published to `clan:{id}:goal`. centre is the mean member position; influence is a soft
  radius growing with membership, published to `clan:{id}:centre`. members bias toward the
  goal strictly below survival, gated on `goal_bias_chance`. goals raised carrying
  capacity 103 → 112.

### outstanding

- **combat / raiding** — the `FLEE` state exists as an empty seam with no trigger. this
  is the last thing standing between phase 1 and done.
- **richer social use of messages** — agents act on request/offer/alert; nothing yet uses
  `info` beyond rally, and there is no bartering of wood.

## sequence from here

1. **finish phase 1** — combat / raiding
2. **stronger social** — alliances, rivalry, clan-vs-clan dynamics
3. ~~spatial index~~ — **done early**, see below
4. **hierarchical llm leaders** — middle tier only, per
   [TECHNICAL_ARCHITECTURE.md](TECHNICAL_ARCHITECTURE.md); the vast majority of agents
   stay pure fsm
5. **selective procedural 3d** — claude-of-duty materials, hero areas only
6. **economy layer** — prediction market stub, token chaos

### spatial index — done ahead of schedule

`_nearest_resource` was 56% of runtime and superlinear in agents × nodes. now chunked
(`src/world/spatial.py`). measured: 500 agents went 43.5 → 7.0 ms/tick. this was the only
thing genuinely blocking phase 3 counts, so it was pulled forward.

## non-goals still held

no llm brains · no combat · no hard territory exclusion · no births or reproduction ·
no on-chain economy · no react or three.js viewer · no full 3d everywhere

personality is stored on user-deployed agents but **nothing reads it** — it is the seam
the middle llm tier plugs into, not a behaviour today.

## working notes for whoever picks this up

- **measure before tuning.** the clan-goal thresholds were guessed first and left all 22
  clans on `rally` forever; dumping the actual per-clan spread fixed it in one pass.
- **benchmark on a quiet machine.** the dev server running in the background made the same
  code time between 8 s and 52 s and sent a whole perf investigation down a false trail.
- **watch for livelocks.** four have been found so far, all the same shape: an agent is
  reset to `IDLE` every tick and never completes the action that would resolve its need.
  `needs._already_addressing` is where the exemptions live.
- **the determinism test earns its keep.** it has caught two real ordering bugs that were
  invisible to `==` comparison.
