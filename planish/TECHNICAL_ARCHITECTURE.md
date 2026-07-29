# technical architecture

## stack — as chosen and built

| layer | decision |
|:--|:--|
| simulation core | **custom minimal ecs** in python — chosen over mesa for control over tick determinism and the fsm hot path |
| persistence | **sqlite**, single file at `data/world.db`. duckdb deferred to analytics, redis only when live state needs it |
| api | **fastapi** + websocket, full snapshot pushed per tick |
| viewer | **plain html + vanilla js canvas**. no react, no three.js, no build step — deliberately, until phase 2 |

godot remains the untaken alternative for heavy simulation. nothing depends on it.

## the tick

fixed timestep, one tick per second, twelve systems in a fixed order:

```
spawning → messaging → needs → trade → combat → fsm → movement → build
         → regrowth → leadership → social → blackboard
```

- **messaging first** so the fsm sees last tick's mail
- **trade after needs** so hunger is fresh and transferred food is in the inventory before
  the agent decides
- **leadership before social** so an arrived model decision is applied before the rules
  would otherwise choose a goal — and any clan it did not answer for still gets one
- **social and blackboard last** so what they publish is read next tick

every social channel therefore has the same one-tick lag. information travels; it does not
teleport.

## determinism

there is no mutable global rng. every draw comes from `TickRng(seed, tick, system_name)`,
so a system's randomness is a pure function of *when it runs*. nothing about the rng is
persisted because there is no stream state to persist — reloading a save at tick n and
continuing produces byte-identical history to an uninterrupted run.

supporting rules, each of which exists because breaking it caused a real bug:

- `World.query()` returns entity ids **ascending**, so behaviour never depends on dict
  insertion history
- the blackboard keeps its keys **sorted**, and the sqlite encoder deliberately does
  **not** use `json.dumps(sort_keys=True)` — a save must round-trip to the same *iteration
  order*, not merely the same content
- the spatial index visits candidates in ascending entity order and compares with a strict
  `<`, so ties still go to the lowest id exactly as the old linear scan did

`tests/test_determinism.py` asserts all of this against a full save/reload cycle by
hashing the entire world.

## performance

**spatial partitioning is implemented** (`src/world/spatial.py`) — chunked buckets for
resource lookup. node positions never change (nodes go dormant, never destroyed), so the
index is pure position data, rebuilt per tick and queried many times.

| world | before | after |
|:--|--:|--:|
| 120 agents | 6.13 ms/tick | 1.55 ms/tick |
| 300 agents | 16.53 ms/tick | 4.52 ms/tick |
| 500 agents | 43.51 ms/tick | 6.97 ms/tick |

the win grows with scale because the old path was superlinear in agents × nodes. phase 3
counts are comfortably in budget.

related ecs decisions: `World.first()` returns a singleton without sorting a whole store
(the blackboard lookup ran ~105k times per 1000 ticks through `query()`), and
`World.query()` intersects dict key views in c rather than testing membership per entity.

remaining known debt, neither load-bearing: `build.py` rebuilds its occupied-tile set each
tick (once per tick, not per agent), and `social.find_clan` scans clans linearly.

## hybrid hierarchical cognition

- **bottom (the vast majority)** — fsm + utility. **zero llm, permanently.** this is what
  makes the world runnable at all.
- **middle (clan leaders)** — built and **provider-neutral**. `src/llm/` + the
  `leadership` system, off behind `llm_enabled`, backend chosen by `llm_provider`
  (`anthropic` | `xai` | `openai` | `none`; the openai path also serves openrouter and
  local servers). asked on one tick, answers on a later one; `social._choose_goal` stays
  the floor for any clan it does not answer for.

  the guarantees live in `ThreadedAdvisor`, not in any provider — a backend supplies only
  a client, a request, and an auth-failure test, and inherits non-blocking submission,
  exception containment, self-disable on bad credentials, and budget enforcement.
- **top (empire / crises)** — frontier llm, rare. not built.

**advisor spend is world state, not runtime state.** `AdvisorState` (`calls_made` plus
outstanding requests) is persisted with everything else. money already spent is a fact
about the world, not about the process that spent it — keeping the counter in memory meant
a restart reset the budget to zero, and an in-flight request was silently dropped while the
clan's cooldown persisted. an interrupted request is re-sent, bounded by
`llm_max_recovery_attempts` and by the remaining budget.

information asymmetry between tiers is a feature, not a limitation.

**any proposal that puts an llm call in a bottom-tier agent's tick loop is wrong by
construction.** at 500 agents and one tick per second that is 500 calls/second forever.

## communication

- **primary: shared blackboard** — world state slices and a tick-stamped event log,
  expired by ttl
- **secondary: structured json envelopes** — `{from, to, type, content}`, close-range or
  clan-scoped, persisting until read

higher tiers, when they exist, consume aggregates rather than raw messages.
