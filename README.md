# neo-civilization

a persistent, tick-based ai agent civilization sim. this repo is at **phase 0**: a
deterministic world of fsm-driven agents that gather, build, eat and starve, streamed
live to a browser.

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

```bash
.venv/bin/python -m pytest tests/ -q      # 46 tests, ~20s
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
    systems/                 needs -> fsm -> movement -> build -> regrowth, in that order
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

### no births

population can only fall. there's no reproduction, so a world that loses agents never
gets the headcount back — "recovery" above means the survivors stop dying, not that the
count climbs.

### cost

~1.0 ms/tick at 120 agents and 360 huts, measured over 60000 ticks. utterly irrelevant at
1 hz. `build.py` rebuilds its occupied-tile set every tick and `query()` sorts the whole
entity set, both of which will matter at phase 3 agent counts and neither of which is
worth touching yet.
