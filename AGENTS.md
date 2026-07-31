# AGENTS.md

Read this before touching anything. `NEXT.md` has the current work.

## What this is

A persistent tick-based world where ~110 agents forage, build, form clans, trade, raid and
remember. It streams live to a browser as procedural 3D. Its users are spectators, who watch,
inspect individual agents, deploy their own, and bet on outcomes.

**The one constraint everything descends from: it must run 24/7 for $0.** ~110 agents ×
1 tick/sec would be ~10M inference calls a day, which is not a cost to optimise — it is
arithmetic that does not work. That is why almost every agent is a state machine, why only
*clan leaders* may ever consult a model, why `llm_enabled` defaults to `False`, why three.js
is vendored rather than fetched, and why there are zero art assets. If a change makes the
idle world cost money, it is wrong regardless of how good it looks.

## Where truth lives

| Question | Authoritative |
|---|---|
| What a system actually does | the code in `src/world/systems/` |
| Every tunable | `src/world/config.py` |
| Tick order and why | `build_registry()` in `src/world/tick.py` |
| What is built / outstanding | `planish/IMPLEMENTATION_PLAN.md` |
| Visual decisions and their measurements | `planish/VISUALS_AND_PROCEDURAL.md` |
| Why a past decision was made | `git log` — commit bodies explain *why*, not what |
| Current state and next task | `NEXT.md` |

**When a doc and the code disagree, the code wins and the doc is a bug — fix the doc.** This
has already happened once: `IMPLEMENTATION_PLAN.md` listed "no three.js viewer" as a non-goal
long after three.js became the main view.

## TRAPS

Architecture you can discover by reading. These you cannot.

1. **`json.dumps` in `sqlite_store.py` deliberately omits `sort_keys`.** A save must
   round-trip to the *same iteration order*. Adding `sort_keys` breaks determinism, and `==`
   on dicts will not show you — it ignores order. This bug shipped twice.

2. **A component missing from `COMPONENT_TYPES` in `sqlite_store.py` is silently dropped on
   reload.** No error, no warning, just `None` where state was. `MarketBook` hit this.
   `test_markets.py::test_every_component_in_a_live_world_is_persisted` guards it now.

3. **`needs._already_addressing` exemptions exist because of four separate livelocks.** Each
   is an agent reset to `IDLE` every tick, never completing the action that resolves its need.
   Removing an exemption resurrects one.

4. **Raiders never pursue.** Deliberate: it makes the starving-raider-chases-forever livelock
   impossible *at any parameter setting* rather than merely unlikely. Adding chase logic
   reintroduces a whole failure class.

5. **Personality is stored, persisted and displayed, but nothing reads it.** Looks unfinished;
   is a deliberate seam for the cognition tier. Do not wire it to the FSM.

6. **`test_population_survives_a_temporary_food_shortage` uses 0.6, not 0.8.** Looks like a
   weakened test. It was lowered after measuring six seeds: the natural spread is 0.68–0.91
   and the old bar was tuned to one seed. Do not "restore" it.

7. **Two noise paths in `world3d.js` must never be crossed.** `fbm` is periodic (textures must
   tile); `openFbm` is not (terrain must not repeat). Swapping either is a visible bug.

8. **DGrid silently discards `system` messages.** The model answers plausibly from the user
   turn alone — confident, on-topic, completely uninstructed. `DGridAdvisor` folds system into
   the user turn. Other OpenAI-shaped providers keep the system role because they honour it.
   DGrid also accepts `response_format: json_schema` and ignores it.

9. **DGrid has two key types.** `mk-` (management) can list models but cannot infer; `sk-`
   (model) can. A wrong key looks like a broken request.

10. **A green test can be measuring nothing.** Three times here: a vacuous `unmount` check hid
    a leaked 2048² shadow map; a zoom-clamp test wrote past the clamp it was testing; a pixel
    probe sampled a fixed stride that missed the subject. When a test passes, ask what would
    make it fail.

11. **The camera glides, it does not jump.** Assertions about camera position must call
    `settle()` first — but a suite that *only* settles cannot tell easing from a hard cut, so
    the glide is asserted separately.

12. **`markets` runs last in the registry and only ever reads.** A market must not be able to
    change the world it bets on.

## NEVER

- **No LLM brains for ordinary agents.** ~10M calls/day. Breaks the $0 constraint outright.
- **No network call from the simulation core.** An advisor is asked on one tick and answers on
  a later one. What the world depends on is the *recorded decision*, never the API call —
  otherwise history depends on the network and replay is impossible.
- **No unbounded accumulator.** Every one added here has eventually eaten the simulation.
  Spend counters, logs, market history, spawn queues are all capped.
- **No in-memory-only cost counter.** A budget that resets on restart is the same as no budget;
  a crash loop would spend without limit. Spend is durable world state.
- **No React, no build step, no npm at runtime.** The viewer is plain files served as-is.
  Playwright is dev-only and must never become a project dependency.
- **No CDN.** three.js is vendored so the viewer works offline.
- **No wall-clock dependency in the sim.** User input queues and is applied at a fixed tick.
- **No simulation rule changed for a visual reason.** Nothing in the sim reads `terrainHeight`.

## GATES

There is **no linter, formatter, typechecker or CI** in this repo — none installed, no
`.github/`. Tests are the only automated gate, so nothing arrives "too late to iterate
against", but also nothing catches style or types.

```bash
.venv/bin/python -m pytest -q          # 352 tests, ~195s. the gate.
```

GPU behaviour is invisible to pytest, so:

```bash
.venv/bin/python -m src.main           # in one terminal
node scripts/verify_world3d.mjs        # 39 checks in real chrome, ~90s
```

Needs `npm i playwright` in a scratch directory — **not** in the repo.

```bash
.venv/bin/python -m scripts.verify_llm --scripted   # offline, no key, ~30s
```

Live model path (costs credits, needs `.env`):

```bash
.venv/bin/python -m scripts.verify_llm --provider dgrid --model anthropic/claude-opus-5
.venv/bin/python -m scripts.compare_advisor --dry-run      # free: collects states only
.venv/bin/python -m scripts.compare_advisor --samples 24   # ~$0.60, ~100s
```

**Do not measure the advisor by population.** Seed-to-seed variance is sd 8.7, spread 92-117
at tick 8000, while the advisor is capped at 200 calls against ~3333 goal reviews — 6% of
decisions. A 6% intervention cannot move a metric with a ±25 noise floor, so an unpaired A/B
produces a number nobody can interpret. Measure judgement instead, or pair on identical seeds.

## How to work here

- **Measure before tuning.** Guessed clan-goal thresholds left every clan on `rally` forever;
  dumping the actual spread fixed it in one pass. The full-world-3D decision came from timing
  four radii, not from opinion.
- **Benchmark on a quiet machine.** A background dev server made identical code time 8s–52s
  and sent a whole investigation down a false trail.
- **Check the trigger, not the symptom.** Raiding keyed to empty *stores* made every clan raid
  within 400 ticks of genesis and cost 47 agents — that was startup conditions, not famine.
- **Design the failure out rather than tuning it away.** See trap 4.
- **Prove a new test fails.** Break the thing deliberately, watch it go red, restore. Every
  test added here was verified this way.
- **Contain anything reaching outside the sim at the boundary.** A hostile advisor must leave
  the world running; that test has caught two real crashes.
- **Report drift rather than quietly retuning a threshold.** If a bar has to move, measure
  first and say why in the commit.
