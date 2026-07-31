# NEXT.md

## Paste this to start a session

```
Read AGENTS.md, then NEXT.md, then planish/IMPLEMENTATION_PLAN.md.
Then run: .venv/bin/python -m pytest -q

Before writing any code, report back:
  - what this project is and the one constraint that shapes it
  - the test count you actually saw, and whether it matched NEXT.md
  - which task you are about to do and its acceptance criteria
  - anything in the docs that contradicts the code

Do not start until I confirm your read is right.
```

## Current state — verified 2026-07-31

| | |
|---|---|
| Tests | **350 passed in 113s** (`pytest -q`, actual output) |
| Browser checks | **39/39** (`node scripts/verify_world3d.mjs`) |
| Commits | 33, `main`, pushed to `origin` (private) |
| Working tree | clean |
| Lint / typecheck / CI | **none exist** |

**Built:** phase 0 survival skeleton; phase 1 social layer (blackboard, messaging, trade,
clans, goals, influence, user-deployed agents, raiding, grudges, truces); chunked spatial
index; multi-provider LLM advisor, off by default, **verified live once** through DGrid;
procedural 3D as the main view with a self-directing cinematic camera; humanoid agents;
prediction markets.

**Not built:** LOD (none anywhere, and not yet justified — see below), births, economy,
hard territory ownership, on-chain identity.

## Next task — LOD

Humanoids landed (`buildHumanoid` in `world3d.js`). Measured, whole world, 120 agents:

| | before | after |
|---|---|---|
| draw calls | 40 | **38** |
| triangles | 147k | **134k** |
| p50 frame | 16.6 ms | 16.7 ms (vsync) |

Blocky boxes are cheaper than the smooth capsule they replaced, so the humanoid cost nothing.
Limbs swing for real, in the vertex shader, still at two batches per clan — so per-limb
animation did **not** cost the draw calls that a batch-per-limb approach would have.

That means **LOD is still not justified by measurement** — the GPU idles waiting for vsync.

Do not build LOD until something makes it necessary. The honest triggers are:
- agent counts well past 120 (the sim supports 500; the viewer has never been asked to)
- ~~per-limb walk animation~~ — done in the vertex shader instead, at no draw-call cost
- a lower-end GPU than this machine

**Measure first, then decide.** `scripts/verify_world3d.mjs` reports calls and triangles;
raise `agent_count` in `WorldConfig` and see where it actually hurts. If it does not hurt,
say so and pick different work rather than building LOD because it was on a list.

## Blocked on a human

- **Lint / formatter / typechecker.** None installed. Deliberate minimalism, or an accident to
  fix? Changes GATES either way. *(unanswered)*
- **LLM A/B.** The advisor has only ever been asked once, and it agreed with the rules. Proving
  it is *better* needs one seed run both ways. Costs credits. *(unauthorised)*
- **Repo visibility.** Private. Public is a one-way door — `.env` is gitignored and history is
  key-free (scanned), but that is the owner's call.

## Do not reopen

- **Full-world 3D vs a focus square** — settled by measurement at four radii; identical frame
  times. Do not reintroduce chunking or streaming.
- **`no three.js` / `no full 3d`** — reversed 2026-07-30. `IMPLEMENTATION_PLAN.md` carries the
  struck-through record. Do not resurrect them.
- **No React / no build step / no npm at runtime** — still binding.
- **LLM for ordinary agents** — permanently out. Arithmetic, not preference.
- **Parimutuel payouts, 1000 starting credits, 100 max stake** — decided.
- **Markets read-only over world state** — the property the feature rests on.
- **Where the 3D entry points live in the sidebar** — moved above the fold twice after
  features shipped that nobody could find. Keep new panels high.
