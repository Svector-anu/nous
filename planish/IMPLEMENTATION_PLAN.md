# implementation plan

status as of the hierarchical-leaders milestone. this file is the handover doc: a fresh session should be
able to read this plus the [root readme](../README.md) and continue without asking.

## where we are

| phase | state |
|:--|:--|
| **0 — skeleton** | ✅ complete, frozen |
| **1 — social emergence** | ✅ complete: blackboard, messaging, trade, clans, goals + influence, user agents and raiding |
| **2 — visual upgrade** | ⬜ not started (intentionally) |
| **3 — hierarchy & scale** | 🟡 middle llm tier built (opt-in, off by default); spatial index done |
| **4 — spectator & economy** | ⬜ not started |

246 tests green. local `main`, lowercase commits, **no remote yet**.

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

## phase 1 — social emergence ✅

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

- **raiding** — scarcity-driven fifth clan goal. a clan escalates only when mean hunger
  drops below `clan_desperate_threshold` (20, set under the 22 floor measured in a healthy
  world) *and* it has already spent a window on `gather_food`. 1v1, opportunistic, never
  pursuing. energy is the only currency and `energy == 0` the only death. free in
  peacetime — 112 agents with and without combat at tick 40000 — and it makes droughts
  markedly deadlier.

### outstanding

- **richer social use of messages** — nothing yet uses `info` beyond rally, and there is
  no bartering of wood.
- **alliances** — rivalry exists (clans remember raids and settle grudges first), but
  nothing forgives, allies, or negotiates. that is the next real social layer, and the
  first place an llm leader would have something to decide that rules cannot express.

## sequence from here

phase 1 is closed. next up:

1. **verify the live llm path** — the middle tier is built but has never made a real
   call (no credentials on the build machine). one live call, then measure whether
   model-set goals actually beat `_choose_goal`. if they don't, that is worth knowing early.
2. **stronger social** — alliances, rivalry, memory of who raided you
3. **selective procedural 3d** — claude-of-duty materials, hero areas only
4. **economy layer** — prediction market stub, token chaos

### hierarchical llm leaders — built, opt-in, unproven live

`src/llm/` plus the `leadership` system, **provider-neutral**: `llm_provider` selects
anthropic, xai/grok, or any openai-compatible endpoint (openrouter, ollama, vllm, lm
studio) without the simulation knowing. only clan leaders are consulted; the
bottom tier stays pure fsm permanently, because per-agent inference at one tick per second
is arithmetic that does not work.

the load-bearing decision: **the sim core never calls the network.** an advisor is asked on
one tick and answers on a later one, so a decision is an *input* to the world exactly as a
user deployment is. the rules are the floor, not a fallback path — `leadership` runs before
`social`, and any clan the advisor did not answer for gets a rule-based goal that same tick.

bounded by `llm_enabled`, `llm_min_ticks_between_calls`, `llm_max_inflight`,
`llm_max_calls_per_session`, `llm_timeout_seconds` and `llm_log_limit`; an auth failure
disables the advisor permanently rather than burning the session budget.

**advisor spend is durable.** `AdvisorState` persists `calls_made` and outstanding
requests. adversarial review found that a restart previously reset the budget to zero and
silently dropped in-flight requests; both are fixed and locked down by
`tests/test_advisor_persistence.py`.

**never verified against the real api** — no credentials existed on the build machine.
`scripts/verify_llm.py` runs the whole single-clan comparison once a key is present.

### spatial index — done ahead of schedule

`_nearest_resource` was 56% of runtime and superlinear in agents × nodes. now chunked
(`src/world/spatial.py`). measured: 500 agents went 43.5 → 7.0 ms/tick. this was the only
thing genuinely blocking phase 3 counts, so it was pulled forward.

## non-goals still held

no llm brains for ordinary agents (permanently) · no top/empire tier · no hard territory exclusion · no births or reproduction ·
no on-chain economy · no react or three.js viewer · no full 3d everywhere

personality is stored on user-deployed agents but **nothing reads it** — the middle tier
decides clan goals, not individual behaviour, so the seam is still unused.

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
- **check the trigger, not the symptom.** raiding keyed to empty *stores* made every clan
  raid within 400 ticks of genesis and cost 47 agents; nobody holds food at world start,
  which is startup conditions, not famine. moving the trigger to hunger fixed it outright.
- **design the livelock out, don't tune it away.** raiders never pursue, so the obvious
  starving-raider-chases-forever failure cannot happen at any parameter setting.
- **anything reaching outside the sim must be contained at the boundary.** the advisor is
  the only such thing. a hostile advisor — raising from every method and not implementing
  the whole protocol — must leave the world running. that test has now caught two real
  crashes, one per new code path added to `leadership`.
- **anything that costs money is world state.** a counter kept in memory resets on restart,
  which is the same thing as having no limit at all.
