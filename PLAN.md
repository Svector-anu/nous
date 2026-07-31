# Plan — from working simulation to something a stranger can use

Status verified 2026-08-01. Two corrections to the working summary, both load-bearing:

- **Tests are 352 Python + 39 browser**, not "300+".
- **"Handles 500 agents cleanly" is now true of the whole system** — measured 2026-08-01, see
  step 0 below. It was previously a sim-only number being quoted as a system figure.

Everything else in the summary matches the code.

---

## Step 0 — one hour, do it before choosing anything

Three cheap facts that change what the rest of this plan should be. None involves writing a
feature.

1. ~~**Render 500 agents.**~~ **Done 2026-08-01. The viewer holds it.** 500 agents, 682 huts,
   70 clans, at a vsync-locked 60fps in all three camera positions:

   | | overview | settlement | street |
   |---|---|---|---|
   | draw calls | 150 | 152 | 152 |
   | triangles | 294k | 294k | 297k |
   | p50 frame | 16.6 ms | 16.7 ms | 16.5 ms |
   | worst frame | 31.0 ms | 32.6 ms | 32.7 ms |

   "500 agents" is now an honest whole-system claim. Occasional 31-33 ms frames mean a
   dropped frame here and there, not a stall.

   **But it corrects the LOD plan, and this is the useful part.** Draw calls went 38 → 150
   because clan batches went 14 → 70. Agents are batched *per clan*, two batches each, so
   **cost scales with clan count, not agent count**: 70 clans × 2 = 140 calls, plus ~10 for
   the world.

   LOD would not fix that. LOD reduces triangle detail at distance, and triangles are not the
   constraint — 294k is nothing. The fix, if the budget ever binds, is **per-instance colour**
   (`InstancedMesh.setColorAt`) collapsing every clan into one or two batches regardless of how
   many clans exist. That is a smaller change than LOD and addresses the actual limit.

   Note the harness asserts `calls < 90`, tuned for the 120-agent default. At 500 it would
   fail — correctly, since that is a different world.
2. **Watch a stranger deploy an agent.** Anyone who has not seen this project. Say nothing.
   Note where they hesitate. That is the onboarding spec, and it will be shorter and more
   specific than anything written from a chair.
3. **Leave the world running for 24 hours.** Population only falls (no births — confirmed, no
   birth system exists). Nobody has established whether a day-old world is a thriving
   civilisation or eleven survivors in a field. If it is the latter, births move up this list.

---

## Recommendation: A, then B

Onboarding first. Not because identity is less interesting, but because **on-chain identity
for agents nobody deploys is infrastructure with no users.** The current deploy form is two
inputs and a button, sitting below the market panel. It works, and it explains nothing.

Identity built on top of a flow people complete is a feature. Identity built on top of a flow
people abandon is a migration nobody asked for.

---

## A. Deploy flow that a stranger can finish

**The problem, stated precisely.** A first-time visitor sees a live 3D world, a sidebar of
statistics, and a form asking for a "name" and a "short personality note" with no indication
of what either does. The personality field is the sharpest edge: it is **stored, displayed,
and read by nothing** (`AGENTS.md` trap 5). Asking someone to write a personality that has no
effect is a promise the world does not keep.

**Scope**

- **Say what happens before asking for input.** One line: your character spawns next tick,
  lives by the same rules as everyone else, and may starve. That last part is the hook, not a
  disclaimer.
- **Be honest about personality.** Either label it as a note that others can read, or cut the
  field until something consumes it. Do not imply behaviour it does not drive.
- **Follow the arrival.** On spawn, fly the camera to the new agent and name it in the HUD.
  The moment the world acknowledges you is the whole product; right now it is a line of text
  in a sidebar list.
- **Give it somewhere to live.** The agent card already exists in the inspector. Link deploy →
  that card, so "your agent" is a place you can return to.
- **Then place the panel above the fold.** Two features in this repo shipped invisible: the 3D
  button sat under the deploy form for a whole session and was never once clicked, and the
  markets panel had the same problem the day it landed. Assume this one does too until a
  screenshot proves otherwise.

**Acceptance**

- A stranger deploys an agent without being told how, and can find it again a minute later.
- `pytest -q` still green; `verify_world3d.mjs` still 39/39.
- No simulation rule changes. Deploy stays an input queued to a tick.

**Cost**: 1–2 days. **Risk**: low, viewer-only.

---

## B. On-chain identity for user-deployed agents

Do after A. The design constraint is already fixed by this codebase and is worth stating
before any chain work starts, because it is the thing that will otherwise be got wrong:

> **The simulation core never makes a network call.** An identity is an *input* — recorded on
> the agent, queued like a deployment, applied at a fixed tick. If minting has to succeed for
> the world to advance, the world now depends on a chain being up, replay breaks, and
> determinism goes with it.

So: mint off to the side, attach the resulting identifier to the agent as durable world state,
and let the simulation carry on regardless of whether the chain answered. Exactly the pattern
the LLM advisor already uses — asked on one tick, answered on a later one, rules carry the
clan in between.

**Scope**

- An identity field on user-deployed agents, persisted with the world, `null` until confirmed.
- Minting as an out-of-band step that cannot block or crash the tick loop. Every call
  contained at the boundary — a hostile advisor test caught two real crashes on exactly this
  seam.
- Surface it on the agent card. An identity nobody can see is a database row.
- Wallet association without making a wallet mandatory to watch. Spectating must stay free.

**Settled 2026-08-01: an identity is a label, not an advantage.** A registered agent lives by
exactly the same rules as everyone else — same hunger, same starvation, same chances. The
chain knows who owns it; the simulation does not read it and must never start.

The alternative was letting registration confer in-world advantage, and it was rejected on
two grounds. It is a simulation rule change, which in this codebase means a measured control
run (raiding cost 47 agents before the real trigger was found). And it is pay-to-win in a
world whose appeal is that nobody's hand is on the scale — easy to add later, very hard to
take back once people have paid for it.

**Cost**: 3–5 days once the chain target is fixed. **Blocked on**: which chain.

---

## Then, in order

**4. Births.** Population monotonically falls. A 24/7 world that only shrinks has one ending,
and step 0 will tell you how close it already is. This is a real simulation change: it needs a
measured control run, like combat and grudges got.

**5. Markets to external infrastructure.** The data model was built for it — `subject` and
`evidence` are machine-readable precisely so settlement can be handed off without re-parsing
English questions. Mostly integration, not redesign.

**6. Deeper politics.** Truce breaking, evolving rivalries. Cheapest of these in engineering
terms; the grudge and truce machinery already exists. Highest risk of thrashing the delicate
survival balance, so it needs control runs.

**Later**: hard territory, real-money markets (regulatory, not technical), further visual
polish (explicitly accepted as good enough), public launch.

**Not LOD.** Measured above: the constraint at scale is draw calls from per-clan batching, not
triangle detail. If 500+ agents ever need to be cheaper, do per-instance colour first.

---

## Standing constraints — do not trade these away for velocity

- **$0 idle.** The world runs 24/7 without spending. Every change is measured against this.
- **Determinism.** Same seed, same history. User input queues; nothing depends on wall clock.
- **No simulation rule changed for a visual or product reason.**
- **`AGENTS.md` traps 1–12 apply**, particularly: register new components in
  `COMPONENT_TYPES` or they vanish on reload, and a green test can be measuring nothing.

---

## Parked, deliberately

**Proving AI leaders produce better outcomes.** We have proof of coherent divergence — 79% of
decisions differ from the rules, with consistent reasoning, for $0.60. We do **not** have proof
those decisions are better, and the paired survival test that could show it costs $10–30.

Do it when a decision depends on the answer: a launch, an investor question, or a choice about
whether to widen the advisor's reach. Not because the experiment is available.

Until then the honest line is: *materially different, coherent decisions — outcome impact
unmeasured.* Do not let that get rounded up to "smarter."
