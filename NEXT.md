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
| Tests | **343 passed in 178s** (`pytest -q`, actual output) |
| Browser checks | **39/39** (`node scripts/verify_world3d.mjs`) |
| Commits | 30, `main`, pushed to `origin` (private) |
| Working tree | clean |
| Lint / typecheck / CI | **none exist** |

**Built:** phase 0 survival skeleton; phase 1 social layer (blackboard, messaging, trade,
clans, goals, influence, user-deployed agents, raiding, grudges, truces); chunked spatial
index; multi-provider LLM advisor, off by default, **verified live once** through DGrid;
procedural 3D as the main view with a self-directing cinematic camera; prediction markets.

**Not built:** humanoid agents (capsule + sphere today), LOD (none anywhere), births,
economy, hard territory ownership, on-chain identity.

## Next task — humanoid agents

Locked as Option B. Replace the capsule-and-sphere agent with a readable low-poly humanoid.

**Do first, before designing anything:** most of the "continuous world / free movement"
milestone is already shipped. The whole world is meshed and resident (51 draw calls, 180k
triangles, vsync-locked), there is no focus-cluster limit, and the camera already travels
freely. Verify that yourself rather than trusting this file, then build only what is missing.

**Scope**
- Head, torso, arms, legs — readable at street level, not at GTA fidelity
- Clan colour on clothing; keep the skin-toned head
- Basic idle/walk if it stays cheap
- Stay instanced and batched per clan; the per-tick path must still allocate nothing

**Acceptance**
- `pytest -q` still 343+ passing
- `verify_world3d.mjs` still 39/39, draw calls still under 90
- Measure draw calls and frame time before and after, and put the numbers in the commit
- Cinematic camera, picking, minimap all still work

**Then LOD, and only then.** Today LOD would be premature — the GPU idles waiting for vsync
at 51 draw calls. Humanoids are ~6 primitives against today's 2, which is the first time agent
count actually costs something. **Let the measurement design the LOD, not the other way round.**

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
