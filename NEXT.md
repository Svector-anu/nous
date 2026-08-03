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

## Current state — verified 2026-08-02

| | |
|---|---|
| Tests | **394 passed in 206s** (`pytest -q`, actual output) |
| Browser checks | **39/39** (`node scripts/verify_world3d.mjs`) |
| Commits | 41, branch `phase/2-user-agent-standing` ready to merge |
| Working tree | dirty (Phase 2: user-agent standing and progression) |
| Lint / typecheck / CI | **none exist** |
| GitHub issues | #1–#6 filed |
| Open PRs | #7 phase 2 ready, #8 phase 1b, #9 phase 3 (drafts) |

**Built:** phase 0 survival skeleton; phase 1 social layer (blackboard, messaging, trade,
clans, goals, influence, user-deployed agents, raiding, grudges, truces); chunked spatial
index; multi-provider LLM advisor, off by default, verified live and **measured against the
rules** (79% divergence, coherent reasoning — see `scripts/compare_advisor.py`);
procedural 3D as the main view with a self-directing cinematic camera; humanoid agents;
prediction markets; comic-style real-time speech bubbles and a social feed so spectators
can see the social layer; visibility / aliveness pass (event flashes for builds and raids,
right-card WORLD log, shorter director shots, `longestJourney` follows) so purposeful
movement and world activity read clearly; Phase 1 natural-language clan-leader messages
influenced by personality (no LLM, deterministic, bounded text length); Phase 2
user-agent standing and progression (Member → Trusted → Officer → leader succession)
based only on recorded actions: donations, builds, raids, survival, membership time.

**Not built:** LOD (none anywhere, and not yet justified — see below), births, economy,
hard territory ownership, on-chain identity.

## Roadmap

`PLAN.md` holds the sequence from here — step 0 measurements, then onboarding, then
on-chain identity. Read it before picking work.

## Deferred — LOD

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

**Measured 2026-08-01: the viewer holds 500 agents at a vsync-locked 60fps** (150 draw calls,
294k triangles, p50 16.6 ms). So LOD is still not justified — and the measurement shows it
would be the wrong tool anyway. Draw calls scale with **clan count**, not agent count: agents
are batched two per clan, so 70 clans cost 140 calls. Triangles are not the constraint.

If the budget ever binds, do **per-instance colour** (`setColorAt`) to collapse all clans into
one or two batches. Smaller change than LOD, and it addresses the actual limit.

`PLAN.md` holds the sequence from here.

## Blocked on a human

- **Lint / formatter / typechecker.** None installed. Deliberate minimalism, or an accident to
  fix? Changes GATES either way. *(unanswered)*
- **Paired survival test.** *Deliberately parked, not forgotten.* `compare_advisor.py` proved
  the model diverges coherently (79%, $0.60). Whether it survives *better* needs 10 seed
  pairs with the advisor uncapped, ~$10-30. Do it when a decision depends on the number —
  a launch, an investor question — not because the experiment exists.
- **Repo visibility.** Private. Public is a one-way door — `.env` is gitignored and history is
  key-free (scanned), but that is the owner's call.

## Do not reopen

- **On-chain identity confers nothing in-world** (settled 2026-08-01). It is ownership, not
  advantage. Registered agents starve like everyone else. Rejected the alternative because it
  is a simulation rule change *and* pay-to-win in a world whose appeal is impartiality.

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
