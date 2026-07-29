# Neo-Civilization

A persistent, tick-based AI agent civilization simulation. This repo is at **Phase 0**:
a deterministic world of FSM-driven agents that gather, build and rest, streamed live to
a browser viewer.

Design docs live in [`planish/`](planish/). Nothing here invents behaviour those docs
don't call for.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.main
```

Open <http://127.0.0.1:8000>. Agents are coloured dots (colour = FSM state), resource
nodes and huts are tiles. The world advances one tick per second and pushes a full
snapshot over `/ws` on every tick.

State is written to `data/world.db` every 50 ticks and on clean shutdown. Stop the
process and start it again — it resumes from the last save. Delete `data/world.db` to
start a new world.

```bash
.venv/bin/python -m pytest tests/ -q
```

## Layout

```
src/
  main.py                    entry point
  world/
    ecs.py                   entities, component stores, system registry
    tick.py                  genesis, tick loop, viewer snapshot, state hash
    components.py            Position, Needs, Inventory, ClanRef, Agent, ResourceNode, Building
    config.py                all tunable world parameters
    rng.py                   deterministic RNG
    systems/                 needs -> fsm -> movement -> build -> regrowth, in that order
  persistence/sqlite_store.py
  api/server.py              FastAPI + WebSocket
  viewer/index.html          canvas viewer, no build step
data/                        world.db
tests/
```

## Determinism

There is no mutable global RNG. Every random draw comes from a `TickRng` derived from
`(seed, tick, system_name)`, so a system's randomness is a pure function of when it runs.
Nothing about the RNG is persisted, and a reloaded save produces byte-identical history
to a run that was never interrupted — `tests/test_determinism.py` asserts exactly that
against a full save/reload cycle.

Queries return entity ids in ascending order for the same reason: system behaviour must
not depend on dict insertion history.

## Phase 0 scope

Implemented: world grid, tick loop, needs/FSM/movement/build/regrowth systems,
deterministic RNG, SQLite save/load, WebSocket viewer.

Resource nodes are never destroyed. A fully foraged node goes dormant at `amount == 0`
and recovers one unit every `regrow_every` ticks (a full node takes ~300 ticks), which is
the only sustainability mechanism in Phase 0 — no farming, no new node spawning. Dormant
nodes render dimmed in the viewer, so you can watch a stripped region come back.

Construction is bounded two ways. Each agent owns at most `max_huts_per_agent` (3) huts,
tracked as a counter on `Agent` and enforced in both the FSM and the build system;
ownership is recorded on `Building.owner`. On top of that, building stops entirely once
buildings reach `global_build_stop_fraction` (60%) of all tiles. At default settings the
per-agent cap is what binds — 120 agents × 3 = 360 huts, well under the 2457-tile global
ceiling — so the global stop only matters on small or crowded maps. There is no hut decay,
no upkeep, and no territory claim in this phase.

Not implemented, by design — these are later phases in
[`planish/IMPLEMENTATION_PLAN.md`](planish/IMPLEMENTATION_PLAN.md): the shared blackboard,
structured messaging, clans, combat, user agent deployment, LLM cognition tiers, and the
procedural 3D viewer. `ClanRef` and the `FLEE` state exist as empty seams so Phase 1 can
fill them without a schema change.

Nothing here is invented beyond what `planish/` and the agreed Phase 0 decisions call for.
Where the docs are silent on something the simulation needed — resource sustainability,
construction limits — the behaviour was decided explicitly rather than guessed at.

### Steady state

Measured over 50000 ticks (~14 hours of wall clock) with default settings. Huts settle at
the per-agent ceiling by tick 2000 and the world runs indefinitely from there — population
flat, agents still moving, resource economy self-sustaining.

| tick | agents | huts | active nodes | agents moving |
|-----:|-------:|-----:|-------------:|--------------:|
| 500 | 120 | 338 | 180/220 | — |
| 2000 | 120 | 360 | 175/220 | 118/120 |
| 8000 | 120 | 360 | 196/220 | 120/120 |
| 20000 | 120 | 360 | 183/220 | 120/120 |
| 50000 | 120 | 360 | 183/220 | 119/120 |

### Starvation and carrying capacity

An agent whose hunger bottoms out at 0 is barred from `REST` — it cannot enter the state,
and is evicted if already in it. Energy keeps decaying at the normal −1 per tick with no
extra penalty, so starvation kills by removing the only recovery mechanism rather than by
adding damage. Eating restores access to rest on the same tick. Death remains the single
`energy == 0` rule.

With inventories bounded (below), the default world sustains all 120 agents indefinitely —
food regrows at roughly 3 units/tick against ~2.7 consumed, and each agent keeps a small
buffer rather than a hoard. Population is flat and the starving count is zero from tick
2000 through 60000. The margin is thin by design, which is what makes shocks bite.

A *regional* shortage is survivable and graded. Blighting a fraction of food nodes for
600 ticks, from a settled world of 40:

| blighted | agents after | outcome |
|---------:|-------------:|:--------|
| 50% | 40 | no deaths |
| 60% | 37 | light losses |
| 70% | 28 | survivable |
| 80% | 14 | severe |
| 90% | 8 | near-collapse |

`test_population_survives_a_temporary_food_shortage` pins the 70% case from both sides —
it fails if a drought kills nobody, and if the survivors never stop dying.

### Carrying capacity is a real per-resource cap

`carry_capacity` (5) bounds food and wood independently. `_gather` checks it *before*
taking a unit and refuses the pickup outright, leaving the unit in the ground rather than
overfilling the agent; `_decide_from_idle` will not send an agent after a resource it has
no room for, so a full agent never walks to a node just to be turned away. An agent with
nothing left worth carrying banks energy in `REST` instead.

The bound holds across a 60000-tick run: max food 5, max wood 5, zero violations. This
matters beyond tidiness — an agent now carries at most 5 meals (~225 ticks of buffer)
instead of the several hundred it used to hoard, which is what keeps starvation reachable
at any point in the run rather than only in the opening minutes.

A late shock demonstrates it. Blighting 70% of food nodes for 600 ticks starting at tick
**40000**, on a world that had been stable at 120 agents for the whole run:

| tick | agents | starving |
|-----:|-------:|---------:|
| 40000 | 120 | 0 |
| 40400 | 99 | 14 |
| 40600 (drought lifts) | 53 | 8 |
| 41500 | 48 | 0 |
| 60000 | 48 | 0 |

67 deaths, then survivors stabilise at 48 and hold for a further 19000 ticks. A *total*
food blackout is no longer survivable at all — `test_total_famine_is_lethal` pins that.

One cosmetic leftover: an agent that has built its three huts keeps whatever wood it was
carrying, since it has no use for it and no reason to gather more. Harmless, and there is
no drop action in Phase 0.

### Not implemented: births

Population can only fall. There is no reproduction, so a world that loses agents never
regains the headcount — "recovery" above means the survivors stop dying, not that the
count climbs back.

Per-tick cost rises with world population — 0.70 ms early, 3.27 ms at 50000 ticks —
because `build.py` rebuilds its occupied-tile set every tick and `query()` sorts a larger
entity set. Irrelevant at 1 Hz; revisit at Phase 3 agent counts.
