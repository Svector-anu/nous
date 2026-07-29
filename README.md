# neo-civilization

a persistent, tick-based ai agent civilization sim. this repo is at **phase 1**: a
deterministic world of fsm-driven agents that gather, build, eat, starve, form clans and
talk to each other — streamed live to a browser.

no llm calls anywhere yet — that's phase 3, and only for leaders. phase 0 is pure state
machines, which is the whole point: a world that runs 24/7 for $0.

design docs live in [`planish/`](planish/). nothing here invents behaviour those docs
don't call for.

## run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.main
```

open <http://127.0.0.1:8000>. agents are coloured dots (colour = fsm state), resource
nodes and huts are tiles, dormant nodes render dimmed so you can watch a stripped region
grow back. one tick per second, full snapshot pushed over `/ws` every tick.

state lands in `data/world.db` every 50 ticks and on clean shutdown. kill the process,
start it again, it picks up exactly where it left off. delete the db for a new world.

agents are coloured by clan by default; the button in the sidebar flips them back to fsm
state colouring.

```bash
.venv/bin/python -m pytest tests/ -q      # 80 tests, ~46s
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
    systems/                 messaging -> needs -> fsm -> movement -> build
                             -> regrowth -> social -> blackboard, in that order
  persistence/sqlite_store.py
  api/server.py              fastapi + websocket
  viewer/index.html          canvas viewer, no build step, no npm, no react
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

## what phase 0 does

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
[`planish/IMPLEMENTATION_PLAN.md`](planish/IMPLEMENTATION_PLAN.md): shared blackboard,
structured messaging, clans, combat, user agent deployment, llm cognition tiers, and the
procedural 3d viewer. `ClanRef` and the `FLEE` state exist as empty seams so phase 1 can
fill them without a schema change.

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

## what phase 1 adds

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

per-tick cost has climbed across the phases: **1.0 ms** (phase 0) → **2.1 ms** (clans and
blackboard) → **5.9 ms** (trade), at ~100 agents over 40000 ticks. still 0.6% of the
budget at 1 hz.

profiling says the money is not where it looks. trade is 11% of runtime and social 11%;
**56% is `fsm.run`, and 9.6 of 25.5 seconds is `_nearest_resource`** — a phase 0 function
doing a linear scan of every resource node, per seeking agent, per tick. trade did not
get slower, it changed the *state mix*: sociable agents forage far more actively than the
idle, fully-stocked ones phase 0 settled into, so the expensive path runs much more often.

the fix is the spatial partitioning `planish/TECHNICAL_ARCHITECTURE.md` already calls for
and phase 0 deliberately deferred. it is the first thing that will actually block phase 3
agent counts — `_live_node_at` and `build.py`'s occupied-tile rebuild are the same shape
of problem.
