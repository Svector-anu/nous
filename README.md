# neo-civilization

a persistent, tick-based ai agent civilization sim. agents gather, build, eat, starve,
form clans, trade, and raid each other when the food runs out — streamed live to a browser.

**no llm calls happen unless you turn them on.** every agent is a state machine; only
clan leaders can be given a brain, and only behind `llm_enabled`. off by default, because
the world has to run 24/7 for $0.

when you do turn it on, credentials come from `.env` (gitignored; copy `.env.example`).
providers: `anthropic`, `xai`, `openai`, and `dgrid` — a gateway fronting many providers
behind one openai-compatible endpoint, where models are addressed `provider/model`.

> dgrid issues two kinds of key and only one can infer. a **management** key (`mk-`) may
> list models but returns 401 from chat/completions; a **model** key (`sk-`) is the one you
> want. that asymmetry makes a wrong key look like a broken request, so both the advisor
> and `scripts/verify_llm.py` check the prefix up front and say which is which.
>
> dgrid also **drops system-role messages**. measured, not guessed: the same instruction
> sent as `system` came back "I don't have context for this", and sent in the user turn came
> back as exactly the requested json. so `DGridAdvisor` folds the system prompt into the
> user turn. without that, the goal list and the output shape never reach the model and it
> answers plausibly from the state summary alone — which reads as a parsing bug and is not
> one. it accepts `response_format: json_schema` and ignores it too, so the system prompt
> states the output shape itself rather than relying on that parameter.

**the live path is verified.** one real call, one clan, through the running simulation:

```
[raw response]
{"goal": "gather_food", "reason": "Hunger sits near half with eight rival clans nearby,
 so keep stores topped up rather than chase the last hut."}

model chose : gather_food      rules chose : gather_food      verdict : same
latency     : 4101 ms          applied at tick 813, survives save/reload
```

reproduce with `.venv/bin/python -m scripts.verify_llm --provider dgrid --model
anthropic/claude-opus-5`. it is deliberately bounded to one clan and three calls.

design docs live in [`planish/`](planish/). nothing here invents behaviour those docs
don't call for. [`planish/IMPLEMENTATION_PLAN.md`](planish/IMPLEMENTATION_PLAN.md) is the
handover doc — status, what's next, and the traps already fallen into.

**where this is**: phase 0 complete and frozen. **phase 1 is done** — blackboard,
structured messaging, resource transfer, clans, clan goals with soft influence,
user-deployed agents and scarcity-driven raiding, plus raid memory (grudges) and truces.
the phase 3 spatial index got pulled forward because it was the only thing genuinely
blocking scale. **the visual layer is done**: procedural 3d is the main view, with a
self-directing camera. 347 tests on local `main`, no remote.

still deliberately absent: hard territory ownership, births, economy.

## run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.main
```

open <http://127.0.0.1:8000>. a procedural 3d world fills the page and the camera starts
directing itself — no input needed to watch. the minimap in the corner shows the whole world:
agents as coloured dots, huts and resource nodes as tiles, clan territory as soft discs. one
tick per second, full snapshot pushed over `/ws` every tick.

state lands in `data/world.db` every 50 ticks and on clean shutdown. kill the process,
start it again, it picks up exactly where it left off. delete the db for a new world.

**click an agent in the 3d view to inspect it.** picking is a raycast against the instanced
agent batches, so it works on the person you can actually see. the side panel shows its name
and personality, fsm state, what it wants, energy and hunger as bars, inventory, huts owned,
raid record, its clan and whether it leads, the clan's current goal and who set it (`rules`
or `llm`), and the clan's grudges and truces — plus buttons to fly to that agent or to its
clan's centre. selecting also dims every agent outside that clan on the minimap, so a click
answers "where is this clan" as well as "what is this agent doing". `esc` clears it.

selection is keyed by agent id, so it survives ticks as the agent moves, and says so
plainly when the agent starves or loses a raid.

**3d is the main view.** the whole world is meshed in three.js and fills the page. the old
canvas map is now a 200 px minimap in the corner carrying territory, resources, and a ring
showing where the camera is looking. click the minimap to travel, click an agent in 3d to
inspect it, click a clan in the legend to fly to it, or take the whole-world overview.
orbit / shift-drag pan / wheel zoom throughout.

**the camera runs itself.** it opens cinematic: a director watches each tick, works out what
is worth looking at — a raid beats a building site beats an empty field — and composes a shot
around it. orbits, dolly-ins, tracking shots on somebody fleeing, slow cranes over a
settlement, and low vistas from among the huts. the hud names what you are watching
("push · Branwyn-016 won a raid"). you do not have to drive it, and if you grab the camera it
hands over instantly and picks up again once you stop. `camera: cinematic` in the sidebar
toggles it, and toggling it off keeps it off; `street level` drops you in among them.

the lens is floored above head height, so no shot — and no amount of manual zooming — ends
up inside somebody's skull.

this used to be a 25-tile focus overlay, because when each hut was seven separate meshes 93
huts cost 651 draw calls. instancing removed the reason for the constraint: the **entire**
world — 360 huts, 220 nodes, 114 agents — now measures **50 draw calls and 179k triangles at
a vsync-locked 60fps**, the same frame time the 25-tile overlay had. so nothing streams and
nothing is chunk-loaded; it is all resident and the camera just moves.

it reads the same websocket snapshot the minimap does, so it is live for free and needed no
new endpoint — the only backend addition was `owner` on buildings, and no simulation rule
changed for any of the visual work.

materials follow the claude-of-duty rules rather than its full gpu forge: **zero art
assets, everything generated at init, nothing allocated per frame**. the pipeline is
height-first — fbm value noise builds a height field, albedo and roughness are read off
that height, and the normal map is a sobel derivative of it. five surfaces so far (knotted
plank wood, spalled plaster, coursed rubble stone, weathered shingle roof, and grass that
dries to earth where it thins), which is enough for a hut to read as built rather than as
boxes. the noise is **periodic**, so every texture tiles without a seam — the reference
calls this out and it matters here, because the grass repeats 60x60 across the ground and a
discontinuity at the tile edge draws a grid over the whole scene. terrain noise is
deliberately not periodic, since it is never tiled. three.js is **vendored into the repo**,
not fetched from a cdn, because the same reference holds itself to working offline.

a hut is modular — a stone plinth bedded into the ground, four walls with real thickness, a
doorway gap with a lintel, and a pitched roof of two leaning slabs — so it can grow into a
real kit without being rebuilt. every box in every hut is the same unit cube sized by its
instance matrix, and agents are batched per clan, so the whole world costs ~50 draw calls
and the per-tick path allocates nothing: huts rebuild only when something is built, and
agents just get new matrices.

`scripts/verify_world3d.mjs` is the adversarial check — 39 assertions in real chrome covering
leaks across both synthetic and live ticks, mount/unmount symmetry, webgl context
exhaustion, camera framing per clan, input clamps through the real event handlers, picking
accuracy, and minimap/camera agreement. it needs `npm i playwright` in a scratch dir;
playwright is deliberately not a project dependency.

agents are coloured by clan by default; the button in the sidebar flips them back to fsm
state colouring. deploy your own agent from the sidebar form, or over http:

```bash
curl -X POST localhost:8000/agents -H 'Content-Type: application/json' \
     -d '{"name":"Kestrel","personality":"hoards wood, distrusts strangers"}'
```

it arrives on the next tick, drawn larger with a white ring, and lives by exactly the same
rules as everyone else. `GET /agents` returns the cards.

```bash
.venv/bin/python -m pytest tests/ -q      # 347 tests, ~130s
```

**prediction markets.** spectators bet on what the world will do. markets open on a
schedule and on events (a truce forms, a clan takes a beating), resolve automatically from
world state with no human oracle, and pay out parimutuel — winners split the pool, no house.
five kinds: will a clan survive, will population fall below a bar, will a truce hold, will a
clan be raided, will an agent live. demo credits only; 1000 to start, 100 max a position.

the load-bearing property is that **a market cannot change the world it bets on**. the
markets system only reads agent and clan state. `test_markets_cannot_change_the_world_they_bet_on`
runs two worlds from one seed, with markets and without, and asserts every agent position,
need and state and every clan goal, membership, truce and grudge is identical.

a bet is an *input*, queued like a deployment and applied at a fixed point in the tick, so
history never depends on wall clock. payouts are integer parimutuel with the division
remainder going to the lowest user id — the same ascending-id tie-break determinism rests on
everywhere else.

```bash
curl localhost:8000/markets
curl -X POST localhost:8000/markets/3/positions -H 'Content-Type: application/json' \
     -d '{"user":"ana","side":"yes","stake":25}'
```

## layout

```
src/
  main.py                    entry point
  world/
    ecs.py                   entities, component stores, system registry
    tick.py                  genesis, tick loop, viewer snapshot, state hash
    components.py            Position, Needs, Inventory, ClanRef, Agent, ResourceNode, Building
    config.py                every tunable in one frozen dataclass
    rng.py                   deterministic rng
    spatial.py               chunked resource lookup
    systems/                 spawning -> messaging -> needs -> trade -> combat -> fsm
                             -> movement -> build -> regrowth -> leadership
                             -> social -> blackboard
  llm/advisor.py             clan leader cognition (opt-in, off by default)
  persistence/sqlite_store.py
  api/server.py              fastapi + websocket
  viewer/
    index.html               page, minimap, sidebar; no build step, no npm, no react
    world3d.js               procedural materials, hut kit, instancing, picking
    director.js              cinematic autopilot: events -> subjects -> shots
    vendor/                  three.js, vendored for offline
data/                        world.db
tests/
```

## determinism

there is no mutable global rng. every random draw comes from a `TickRng` derived from
`(seed, tick, system_name)`, so a system's randomness is a pure function of *when* it
runs. nothing about the rng is persisted, because there's no stream state to persist.

reload a save and you get byte-identical history to a run that was never interrupted.
`tests/test_determinism.py` asserts exactly that against a full save/reload cycle, by
hashing the entire world.

queries return entity ids in ascending order for the same reason — system behaviour must
never depend on dict insertion history.

clone this repo and you reproduce the same civilization, tick for tick, from seed
`20260728`.

## the survival layer

world grid, tick loop, needs/fsm/movement/build/regrowth systems, deterministic rng,
sqlite save/load, websocket viewer.

**resources regrow.** nodes are never destroyed. a fully foraged node goes dormant at
`amount == 0` and recovers one unit every `regrow_every` ticks (~300 ticks for a full
node). that's the only sustainability mechanism here — no farming, no new node spawning.

**building is bounded twice.** each agent owns at most `max_huts_per_agent` (3) huts,
counted on `Agent` and enforced in both the fsm and the build system, with ownership
recorded on `Building.owner`. on top of that, building stops once buildings hit
`global_build_stop_fraction` (60%) of all tiles. at default settings the per-agent cap is
what binds — 120 agents × 3 = 360 huts, well under the 2457-tile ceiling — so the global
stop only matters on small or crowded maps. no decay, no upkeep, no territory yet.

**not implemented, on purpose** — these are later phases in
[`planish/IMPLEMENTATION_PLAN.md`](planish/IMPLEMENTATION_PLAN.md): llm cognition tiers,
the procedural 3d viewer, hard territory, births, and the economy layer.

where the docs were silent on something the sim genuinely needed — resource
sustainability, construction limits, what starvation does — the behaviour was decided
explicitly rather than guessed at.

### steady state

50000 ticks (~14 hours of wall clock) at default settings. huts settle at the per-agent
ceiling by tick 2000 and the world just keeps going.

| tick | agents | huts | active nodes |
|-----:|-------:|-----:|-------------:|
| 500 | 120 | 338 | 180/220 |
| 2000 | 120 | 360 | 175/220 |
| 8000 | 120 | 360 | 196/220 |
| 20000 | 120 | 360 | 183/220 |
| 50000 | 120 | 360 | 183/220 |

### starvation

an agent whose hunger bottoms out at 0 is barred from `REST` — it can't enter the state
and gets evicted if it's already in it. energy keeps decaying at the normal −1 per tick
with no extra penalty, so starvation kills by removing the only recovery mechanism rather
than by adding damage. eat, and rest is available again on the same tick. death is still
the single `energy == 0` rule.

food regrows at roughly 3 units/tick against ~2.7 consumed. that margin is thin on
purpose — it's what makes shocks bite. population sits flat at 120 with zero starving
from tick 2000 through 60000.

a *regional* shortage is survivable and graded. blight a fraction of food nodes for 600
ticks, starting from a settled world of 40:

| blighted | agents after | outcome |
|---------:|-------------:|:--------|
| 50% | 40 | nobody dies |
| 60% | 37 | light losses |
| 70% | 28 | survivable |
| 80% | 14 | severe |
| 90% | 8 | near-collapse |

`test_population_survives_a_temporary_food_shortage` pins the 70% case from both sides —
it fails if a drought kills nobody, and if the survivors never stop dying.

### carry capacity is a real cap

`carry_capacity` (5) bounds food and wood independently. `_gather` checks it *before*
taking a unit and refuses the pickup outright, leaving the unit in the ground rather than
overfilling the agent. `_decide_from_idle` won't send an agent after something it has no
room for, so a full agent never walks across the map just to get turned away. an agent
with nothing worth carrying banks energy in `REST` instead.

holds across 60000 ticks: max food 5, max wood 5, zero violations.

this matters more than tidiness. an agent now carries at most 5 meals (~225 ticks of
buffer) instead of the several hundred it used to hoard, and that's what keeps starvation
reachable at *any* point in the run instead of only in the opening minutes. proof — same
70% blight, but fired at tick **40000** on a world that had been stable at 120 the whole
time:

| tick | agents | starving |
|-----:|-------:|---------:|
| 40000 | 120 | 0 |
| 40400 | 99 | 14 |
| 40600 (drought lifts) | 53 | 8 |
| 41500 | 48 | 0 |
| 60000 | 48 | 0 |

67 deaths, then survivors stabilise at 48 and hold for another 19000 ticks. a *total*
food blackout isn't survivable at all any more — `test_total_famine_is_lethal` pins that.

one cosmetic leftover: an agent that's built its three huts keeps whatever wood it was
holding, because it has no use for it and no reason to gather more. there's no drop
action in phase 0.

### grudges

a clan remembers who has robbed it. `Clan.grudges` maps attacker clan id to
`[times_raided, last_raid_tick]` — string keys because it round-trips through json, kept
sorted so a reloaded world iterates identically.

**recorded on theft, not on violence.** a scuffle that took no food leaves no debt; the
entry is written only when `transfer()` actually moves something. the victim's clan
remembers, the aggressor holds nothing, and a clanless victim has no one to do the
remembering.

**one behavioural change, in one place.** `combat._victim()` used to take the nearest
robbable neighbour. it now takes the one whose clan owes the deepest debt, with distance
then entity id breaking ties — a total order, so no new rng stream. a grudge biases *who*
gets robbed, never *how far* a raider will travel: raiders still never pursue, which is
what keeps the chase livelock impossible by construction.

naturally bounded by the number of clans, so no eviction. **grudges never fade** — that is
a decision, not an oversight; nothing yet forgives.

measured over a drought on the default world: peacetime holds zero grudges, and afterwards
12 of 13 surviving clans remember something, 40 raids in total, with **four mutual feuds**
where each clan had robbed the other. clan 1 and clan 10 robbed each other four and one
times respectively.

grudges are behaviourally near-neutral on survival — identical drought outcomes on five of
six seeds, 17 vs 19 on the sixth. they change *who* fights whom, not how many live.

### truces

two clans that have hurt each other can stop. `Clan.allies` is a sorted list of clan ids,
recorded on **both** sides — a truce has no one-sided state, so there is nothing pending to
persist or time out.

it forms on a pure condition, no rng, when all five hold: each has robbed the other at
least once, neither is starving, no raid between them for `truce_peace_ticks` (200), their
centres are within the sum of their influence radii plus slack, and they are not already
allied.

the effect is one line in `combat._victim()`: an ally is skipped. **a truce outranks a
grudge** — a clan you still resent but have made peace with is passed over for a stranger.
grudges are *kept*, not cleared: the memory of the raid outlives the fighting, it just
stops picking the target.

measured on the default world after a drought that produced four mutual feuds: **two settle
into truces**, the other two never do because those clans drifted apart and the contact
condition never holds. that is the rule working rather than failing.

nothing forgives and nothing breaks a truce yet — once made it is permanent, and a clan
that is never near an enemy stays at war forever.

## the social layer

society. three channels, all with the same one-tick propagation lag — nothing an agent
learns is available in the tick it was said.

**blackboard.** a global key-value store on a single world entity. every entry carries
the writer and the tick it landed, and the `blackboard` system drops anything older than
`blackboard_ttl_ticks` (300). gatherers publish `food_locations` / `wood_locations`;
leaders publish `clan_<id>_rally`. keys are kept sorted so a live world and a reloaded
one iterate identically.

**messaging.** `Outbox` drains into recipients' `Inbox` at the top of the next tick.
envelope is exactly `{"from", "to", "type", "content"}` with `to` being an agent id,
`"clan"`, or `"all"`, and `type` one of info/request/offer/alert. inboxes are capped at
`inbox_capacity` and cleared each tick.

**clans.** two idle unaffiliated agents within `clan_form_radius` found a clan at
`clan_form_chance` per tick, capped at `max_clan_size` (8). a clan is its own entity;
membership is mirrored on `ClanRef`. the founder leads, and leadership falls to the
lowest surviving id when a leader dies. empty clans are cleaned up.

**how agents use it.** `_seek` consults the board only when nothing is in vision —
replacing a blind random wander with an informed one, so phase 0 foraging is untouched
whenever food is actually nearby. hints are validated against live nodes at read time, so
a sighting of a since-foraged tile is skipped rather than walked to. idle, well-provisioned,
well-rested agents enter `FOLLOW` and drift to their clan's meeting point.

measured at tick 40000 on the default world: 21 clans, largest at the cap of 8, ~77
messages delivered per tick, and clan members sitting a mean **1.4 tiles** from their
clan's centre where scattering would put them ~24 apart.

### trade

agents read their mail and act on it. inboxes **persist until consumed** — a message
arriving while its recipient is mid-gather is still there when the agent is free — and
only `inbox_capacity` evicts, oldest first. `info` is always consumed so rally chatter can
never push a real request out of a full inbox; leaders now also only announce a rally that
actually moved.

`transfer(world, donor, receiver, kind, amount)` is the underlying verb. it is bounded on
both sides — never more than the donor holds, never past the receiver's `carry_capacity` —
and conserves total world resource. adjacency is `transfer_radius` (2).

the protocol is two hops:

| message | meaning | what happens |
|:--|:--|:--|
| `request` | "i could use food" | a clanmate in range hands over its surplus above `surplus_reserve`; one out of range replies with an `offer` |
| `alert` | "i am starving" | donors dip into the reserve they keep for themselves — the whole difference between a request and an alert |
| `offer` | "come to me, i have spare" | the asker walks over in `MEET`; once adjacent its next request is fulfilled |

requests are rate-limited by `request_cooldown_ticks` (40), and a donor with nothing to
give *keeps* the request rather than discarding it, so it can help once it has food again.

measured over 40000 ticks on the default world: 407 food units changed hands, inboxes
peaked at 2 messages, and population rose from ~98 to **103** — sharing measurably helps
agents survive. by tick 5000 alone: 99 requests, 643 offers, 19 alerts, 84 transfers.

### clan goals and soft influence

a clan's **centre** is the mean position of its living members, recomputed each tick — no
smoothing buffer, because members already sit ~1.4 tiles from it and holding no history
keeps save/load exact. **influence** is a soft radius growing with membership
(`influence_base_radius + influence_per_member × size`, ~10 tiles in practice),
published to the board as `clan:{id}:centre` and drawn as a tinted disc in the viewer.
there is no ownership and no exclusion — influence is readable, not enforceable.

each clan holds one **goal**, reviewed every `goal_review_ticks` and published under
`clan:{id}:goal`:

| goal | chosen when | what members do |
|:--|:--|:--|
| `gather_food` | clan food ratio below `clan_low_food_ratio`, or mean hunger below `clan_hungry_threshold` | bias toward foraging food even when not hungry |
| `gather_wood` | short of huts and short of timber | bias toward foraging wood |
| `expand` | short of huts but holding enough wood | build, and walk inside the clan's own influence first |
| `rally` | fed and built out | drift to the meeting point when idle |

the thresholds are measured, not guessed. clan food ratio runs 0.38–1.00 (median 0.93)
and mean hunger 22–49 (median 40) at steady state; the first thresholds i picked (0.4 and
2 huts/member) fired for one clan in twenty and left all twenty sitting on `rally`.

**bias, never override.** the goal is consulted only below survival and standing
commitments in `_decide_from_idle`, and even then it is gated on `goal_bias_chance`. a
starving member of a `gather_wood` clan still goes after food — there is a test for
exactly that.

goals genuinely diverge. at tick 300 all four are in play across twenty clans (14
`gather_food`, 3 `gather_wood`, 2 `rally`, 1 `expand`); at steady state the split keeps
moving between `rally` and `gather_food`. **expansion retires on its own** — once every
member holds `max_huts_per_agent` huts the hut signal can never fire again, so `expand`
and `gather_wood` are early-life goals and mature clans alternate between feeding
themselves and staying together.

population went *up* with goals, from 103 to **112** at tick 40000: a clan that notices it
is short of food and biases toward foraging feeds itself better than uncoordinated agents.

### user-deployed agents

`POST /agents` with a name and a short personality note. the agent arrives on the next
tick and is an ordinary agent in every respect — same needs, same fsm, joins clans,
trades, starves, dies. the personality is a **note it carries, not a prompt**; there is no
llm in the loop yet, and this is the seam that tier will plug into.

a deployment is an *input* to the simulation, not an event inside it, so the api never
creates an agent. it appends to a `SpawnQueue` on a world entity, and the `spawning`
system — first in the tick — is the single place an agent is born, drawing position from
that tick's rng stream. history stays reproducible: same seed plus the same deployments at
the same ticks rebuilds the same world, and a save carrying a pending request replays it
identically on reload. both are tested.

bounded on three axes, because every unbounded accumulator in this project has eventually
eaten the simulation: `max_pending_spawns` (429), `max_user_agents` (409), and length caps
on name and personality (422).

deployed agents draw larger with a white ring and are listed with their notes in the
sidebar. `GET /agents` returns the cards plus anything still queued.

### raiding

the only violence in the world, and it is a famine response. a clan escalates to the
`raid` goal after a review window on `gather_food` leaves it still desperate, where
desperate means **mean clan hunger below 20** — a bar set under the 22 floor measured in a
healthy world, so peace is the default and raids emerge from droughts.

encounters are 1v1 and opportunistic. a raider robs a non-clanmate it already finds itself
next to; **it never chases**. that is what makes the obvious livelock — a starving raider
forever pursuing a target it cannot catch — impossible by construction rather than by
tuning. outcome is energy-weighted via `TickRng`, so a desperate raider is exactly the one
most likely to lose the attempt.

energy is the only currency. a fight costs both sides energy, costs the loser more, and
kills through `energy == 0` — the same single death rule starvation uses. no health
component, no weapons, no revenge, no territory capture. the loser flees to a fixed point
away from the winner, so flights always terminate, and shouts an `alert`, which the trade
system already answers with food.

measured against a no-combat control:

| world | without combat | with combat |
|:--|--:|--:|
| healthy, tick 40000 | 112 agents | **112 agents, zero raids** |
| 70% blight for 600 ticks | 119 → 53 → 48 | 119 → 43 → 36, 8 clans raiding, 45 raids |

combat is free in peacetime and makes famine deadlier — which is the point. an agent's
carry dies with it, as it always has for starvation, so raiding also destroys food rather
than only moving it.

**the first cut cost 47 agents.** keying escalation to empty *stores* meant every clan
raided within 400 ticks of genesis, when nobody holds food yet — startup conditions, not
famine. moving the trigger to hunger fixed it completely.

### hierarchical cognition

the middle tier of the three the architecture calls for. **the bottom tier stays free** —
at one tick per second and hundreds of agents, per-agent inference is not a cost to
optimise, it is arithmetic that does not work.

**the simulation core never calls the network.** an advisor is *asked* on one tick and
*answers* on a later one, exactly like a user deployment or a message: the decision is an
input to the world, not an event inside it. what the world depends on is the recorded
decision, not the api call.

**the rules are the floor, not a degraded mode.** `leadership` runs immediately before
`social`; it applies whatever decisions have arrived and asks for new ones, then `social`
picks a goal by rules for every clan the advisor did not answer for. a clan whose advisor
is slow, unavailable, over budget, or simply wrong still has a goal on that same tick —
the model's answer refines it when it lands rather than gating it.

nothing an advisor does can break the world. every call into it is contained and every
failure — no sdk, no credentials, timeout, malformed answer, third-party exception —
resolves to "no decision". there is a test that an advisor raising on every call leaves
the simulation running normally.

| control | default | what it bounds |
|:--|:--|:--|
| `llm_enabled` | `False` | nothing runs unless explicitly turned on |
| `llm_provider` | `anthropic` | which backend, or `none` |
| `llm_min_ticks_between_calls` | 300 | how often one clan may be consulted |
| `llm_max_inflight` | 2 | concurrent requests |
| `llm_max_calls_per_session` | 200 | total spend for the life of the process |
| `llm_timeout_seconds` | 30 | a stalled call falls back to rules |
| `llm_log_limit` | 200 | the decision log is bounded like everything else |
| `llm_max_recovery_attempts` | 2 | retries for a request interrupted by a restart |

on top of those, an authentication failure disables the advisor **permanently** rather
than spending the whole session budget rediscovering it 200 times.

**advisor spend is durable world state.** `AdvisorState` holds `calls_made` and the list
of outstanding requests, and is saved and reloaded with everything else. two bugs made
that necessary, both found by adversarial review rather than by the tests:

- the spend counter lived on the in-memory advisor, so **a restart handed the world a
  fresh session budget** — a crash loop could spend without limit.
- an in-flight request vanished on reload while the clan's `last_advisor_tick` persisted,
  so the request was **never answered and never retried**; that clan sat on rule-based
  goals with no indication anything had been lost.

a request interrupted by a restart is now re-sent, capped by `llm_max_recovery_attempts`,
and the retry is counted like any other call because it costs the same. with the budget
exhausted there is nothing to retry with, so the request is abandoned and the rules carry
the clan — the one thing that must never happen is spending past the cap.

a reloaded world with **nothing** outstanding is byte-identical to one that ran straight
through. with a request outstanding it cannot be — the retry is a real extra call and is
correctly counted — so the test asserts the two worlds converge on the same clan goals and
that the reloaded one never overspends.

credentials are deliberately not gated on `ANTHROPIC_API_KEY` — the sdk also resolves an
auth token or an `ant auth login` profile, so an env check would refuse a working setup.

every decision is logged with its source, reason and latency, persisted with the world,
and served at `GET /decisions`:

```json
{"tick": 130, "clan": 2, "goal": "gather_wood", "source": "rules", "reason": "", "latency_ms": 0}
```

### choosing a provider

the simulation talks to one interface — `submit` / `collect` / `pending` /
`inflight_clans` / `close` — and never to a provider. adding a backend touches nothing
outside `src/llm/`.

```python
WorldConfig(llm_enabled=True, llm_provider="xai", llm_model="grok-4")
```

| provider | goes to | key from | notes |
|:--|:--|:--|:--|
| `anthropic` | claude, via the official sdk | sdk resolution (env, auth token, or `ant` profile) | the default |
| `xai` | `https://api.x.ai/v1` | `XAI_API_KEY` | grok; openai-compatible |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` | point `llm_base_url` elsewhere for openrouter, ollama, vllm, lm studio |
| `none` | nothing | — | rules only |

an endpoint on a non-default `llm_base_url` is not required to have a key, so local
servers work with no auth. an unrecognised provider name falls back to the rules rather
than guessing.

**the guarantees live in the base class, not in any provider.** a backend supplies three
things — how to build its client, how to ask one question, how to recognise a permanent
credential failure — and inherits the rest: never blocking the tick loop, containing every
exception, self-disabling on bad credentials, honouring the budget, and reporting what it
has in flight. a new provider cannot forget them, and `tests/test_providers.py` proves it
by driving a deliberately minimal fake backend through the whole contract.

claude speaks its own sdk. everything else speaks raw http, deliberately: the point of the
openai-compatible path is to work against *any* endpoint implementing
`POST /chat/completions`, including local servers whose compatibility is approximate, and a
raw request has no opinion about which one it is talking to. structured output is requested
via `response_format` but never relied on — the answer is also parsed defensively, and one
that cannot be resolved to a known goal is discarded, leaving the rules standing.

### verifying the live path

```bash
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python -m scripts.verify_llm                # one clan, 3 calls max
.venv/bin/python -m scripts.verify_llm --scripted     # offline harness check
```

settles a world, pins the advisor to a **single clan** (`llm_only_clan_id`), and prints the
exact prompt sent, the raw response, the goal chosen, what `_choose_goal` would have chosen
in the identical state, and whether the decision survived save/reload. every decision
carries its own shadow comparison, so the log answers "did the model differ from the rules"
for free.

**still unverified against the wire.** the machine this was built on has no credentials of
any kind, so no real call has ever been made. everything around the call is proven offline —
the harness runs end to end on a scripted advisor, and the request shape follows the current
api (structured outputs via `output_config.format`, cached system prompt, `effort: low`).
what has not been proven is that the request shape is accepted.

### the cost of sociality

phase 1 lowers the carrying capacity from 120 agents to ~98. socialising burns ticks and
energy that would otherwise go into foraging, so the land supports fewer of them. the
population stabilises — 100 at tick 10000, 100 at 20000, 98 at 40000 — it does not
spiral. `social_energy_floor` (60) is the dial: an agent only socialises well clear of
the threshold where it would rather rest.

this was much worse before. the first design had leaders publish *the best known food
node* as the rally point, which sent all eight members onto one tile to strip it and
starve together — a 120 → 45 collapse, isolated by running clans-without-rally (118) and
no-social-at-all (120) side by side. the rally is now a meeting point at the leader's own
position. resource sharing still happens, but through `food_locations`, which spreads
agents across many nodes instead of funnelling them onto one.

### no births

population can only fall. there's no reproduction, so a world that loses agents never
gets the headcount back — "recovery" above means the survivors stop dying, not that the
count climbs.

### cost

resource lookup goes through a chunked spatial index (`src/world/spatial.py`) — the
partitioning `planish/TECHNICAL_ARCHITECTURE.md` calls for. node positions never change
(nodes go dormant, they are never destroyed), so the buckets are pure position data,
rebuilt once per tick and queried many times, with dormancy filtered at query time.

the win grows with scale, which is the whole point:

| world | linear scan | spatial index | |
|:--|--:|--:|--:|
| 120 agents, 220 nodes | 6.13 ms/tick | 1.55 ms/tick | 4.0× |
| 300 agents, 500 nodes | 16.53 ms/tick | 4.52 ms/tick | 3.7× |
| 500 agents, 850 nodes | 43.51 ms/tick | 6.97 ms/tick | 6.2× |

the old path was superlinear — every seeking agent scanned every node, every tick. phase 3
wants 200–500 agents; at 500 that was 43 ms/tick and is now 7 ms, comfortably inside a 1 hz
budget. the test suite dropped from 205 s to 68 s as a side effect.

two smaller fixes came out of profiling the result. `board(world)` was doing a full sorted
`query()` for a single-entity lookup, ~105 000 times per 1000 ticks, so `World.first()`
now returns the lowest matching id without sorting. and `World.query()` intersects dict
key views in C rather than testing membership per entity in python.

**this refactor changes no behaviour.** `state_hash` at ticks 500, 2000 and 5000 is
byte-identical before and after, and `tests/test_spatial.py` checks the index against a
brute-force scan across the whole grid, before and after depletion, at five chunk sizes —
including that ties still go to the lowest entity id, which is what the pre-index sorted
scan did and what determinism depends on.
