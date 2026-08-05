# Soft public demo launch plan

## Proven current capabilities

- A seeded, tick-based simulation advances once per configured second. `TICKS_PER_DAY` is
  200 and `tick_seconds` is 1.0 (`src/world/config.py:11,22`); `Simulation.day` is
  `world.tick // TICKS_PER_DAY` (`src/world/tick.py:165-167`). One in-world day is therefore
  200 real seconds (3 minutes 20 seconds).
- The simulation has a stable whole-world fingerprint for determinism testing
  (`src/world/tick.py:141-151`). It increments the tick and runs registered systems in a
  fixed sequence (`src/world/tick.py:169-175`).
- LLM leadership is opt-in, disabled by default, and bounded (`src/world/config.py:75-95`).
- Prediction markets are enabled by default but read world state rather than writing it
  (`src/world/config.py:99-112`); their balances are explicitly “Demo money” with no real
  currency (`src/world/components.py:389-394`). Position requests are likewise described as
  demo credits only (`src/api/server.py:24-29`).
- A spectator identity for markets is merely a display name: the viewer stores it in browser
  `localStorage` (`src/viewer/index.html:2713-2719`). No authentication capability is proven.
- Visitors can deploy agents (`POST /agents`, `src/api/server.py:324-358`), toggle rest
  (`POST /agents/{agent_id}/rest`, `src/api/server.py:360-379`), and delete deployed agents
  (`DELETE /agents/{agent_id}`, `src/api/server.py:381-399`).
- The server exposes `/health`, returning 200 only while the simulation loop is running
  (`src/api/server.py:181-200`), and `/markets` exposes open/settled demo markets, balances,
  and leaderboard (`src/api/server.py:218-248`).
- World state is saved every 50 ticks (`src/world/config.py:22-23`; `src/world/tick.py:169-175`).
  README documents clean-shutdown persistence (`README.md:67-68`).
- A CI workflow defines Python and real-Chrome browser gates for pushes to `main` and pull
  requests (`.github/workflows/gates.yml:12-16,22-107`). Not verified — needs a decision or
  source: whether GitHub Actions billing is enabled and this workflow has ever run.
- The map-first viewer is a single plain HTML file; its world and bets cards are present in
  `src/viewer/index.html:1617-1638`. The repository rules prohibit React, a runtime build,
  runtime npm, and a CDN (`AGENTS.md:94-96`).

## Gaps for a soft public demo

- There is no authenticated account or persistent server-side user identity proven by source.
  The market name is client-side `localStorage`, not an account (`src/viewer/index.html:2715-2719`).
- Deployed agents have no owner field (`src/world/components.py:71-88`). The rest and delete
  endpoints only verify `user_deployed` (`src/api/server.py:360-369,381-395`), so any visitor
  can control or delete another visitor’s deployed agent.
- The public market card is headed “BETS” and the identity input says “your name”
  (`src/viewer/index.html:1625-1636`). It does not itself establish the demo-only nature of
  the credits despite the backend’s explicit demo-only semantics.
- Existing documentation has stale test-count claims: README says 354 tests
  (`README.md:45-50,145`); NEXT says 416 (`NEXT.md:22`); AGENTS says 418 (`AGENTS.md:110-112`).
  The supplied current baseline is 424 pytest tests and 59 browser checks. Not verified —
  needs a decision or source: whether those supplied counts still reflect this checkout.
- Not verified — needs a decision or source: the intended always-on hosting provider, log
  destination/retention, restart policy, backup location and retention, recovery objective,
  and who owns operational alerts.

## Ordered implementation steps

### Step 1 — Claims audit

Audit public-facing copy and documentation against proven behaviour. Retain the existing
in-simulation markets and credits, but label them as demo/in-sim credits wherever spectators
would otherwise infer cash, settlement, or on-chain status. Correct only claims that source
does not prove.

Acceptance criteria:

- Public copy makes no real-money, on-chain, settled-market, or account claim unsupported by
  source.
- Demo/in-sim market and credit wording is present where markets and balances are presented.
- Stale test-count claims are either corrected from a freshly run gate or removed where a
  durable exact count is inappropriate.
- Both required gates pass, and every new or changed test is shown to fail when the protected
  behaviour is removed.

Dependencies: none.

### Step 2 — Time-model consistency

Make all user-facing and documentary references to “Day N”, tick timing, and market timing
consistent with the source-derived model: 200 ticks/day × 1 second/tick = 3m20s/day. Do not
introduce a wall-clock dependency into the simulation.

Acceptance criteria:

- All audited claims use the source model or explicitly say they are illustrative.
- “Day N” continues to be derived from simulation tick state, not browser/server wall clock.
- Both gates pass; tests fail if the displayed day calculation diverges from the configured
  tick model.

Dependencies: Step 1’s claims inventory.

### Step 3 — Production spine

Specify and implement the smallest deployment/operations boundary for always-on serving:
health checking, structured or recoverable logs, restart behaviour, and durable backups for
the SQLite world state. Preserve the existing server routes and simulation contract.

Acceptance criteria:

- A documented deploy/run/restart/restore procedure exists and uses `/health` to distinguish
  a live loop from a frozen snapshot.
- Backup and restore of the durable world state are exercised without corrupting simulation
  continuity.
- Logs provide enough information to diagnose a failed loop or failed persistence.
- Both gates pass, including a test that fails if the new operational guard is removed.

Dependencies: Not verified — needs a decision or source: hosting, persistence volume, backup
target, and operational ownership.

### Step 4 — Email authentication

Add guest viewing plus a persistent authenticated user identity through magic link or OTP.
The identity must be available server-side for ownership enforcement but must not alter world
rules, tick order, or spectator access.

Acceptance criteria:

- Guests can open and watch the map without signing in.
- A verified email identity persists across a normal browser return and is available to API
  authorization code.
- Authentication failures, expired/invalid credentials, and guest access are covered by
  meaningful tests.
- Both gates pass.

Dependencies: Not verified — needs a decision or source: authentication provider, magic link
versus OTP, email sender/domain, session lifetime, privacy policy, and data-retention policy.

### Step 5 — Agent ownership security fix

Bind each newly deployed agent to the authenticated creator, persist that binding, and require
the same authenticated identity for rest and deletion. Existing agents require an explicit
migration/administration policy; do not silently assign ownership.

Acceptance criteria:

- An authenticated user can rest and delete only their own deployed agents.
- A different user and a guest receive a non-success authorization response and leave the
  target agent unchanged.
- Ownership survives save/reload and is registered in persistence machinery.
- Tests prove both authorization and non-mutation; mutation-testing the authorization check
  makes at least one test fail.
- Both gates pass.

Dependencies: Step 4 and a decision for ownership treatment of pre-auth existing agents.

### Step 6 — Soft-demo stranger path

Validate the stranger flow: open the map, understand the world and demo-market status,
deploy an agent, find it, and use its allowed controls. Apply only small map-first copy and
usability fixes surfaced by this walkthrough.

Acceptance criteria:

- A first-time visitor can complete the stated flow without redesigning the map-first UI.
- The flow works at the supported mobile and desktop viewports already covered by browser
  checks.
- Both gates pass; each new browser assertion demonstrably fails when its intended UI cue or
  interaction is removed.

Dependencies: Steps 1, 2, 4, and 5.

## Explicitly deferred scope

- On-chain markets, real-money payments, settlement, wallet gameplay advantages, ERC-8004,
  full x402 settlement, and any financial representation beyond existing demo credits.
- Robinhood email/password login.
- LLM brains for ordinary agents and any paid API call in tests.
- A map/UI redesign, React, a build step, npm at runtime, external CDN dependencies, or
  unrelated refactors.
- Any simulation-rule change made for visual or launch-copy reasons.

## Risks and guardrails

- **Map-first UI:** authentication, labels, and controls can crowd or obscure the world. Keep
  auth chrome minimal and test it at mobile widths; do not displace map navigation or agent
  inspection.
- **World determinism:** changing system ordering, consuming seeded RNG on a new path, or
  applying user input outside its fixed tick changes replay/state hashes. `state_hash` exists
  specifically to detect whole-world drift (`src/world/tick.py:141-151`), and `Simulation.step`
  runs at a fixed tick (`src/world/tick.py:169-175`).
- **Persistence:** a new ownership component must be included in the persistence component
  registry or it can be silently lost on reload (AGENTS.md:41-44).
- **Cost:** `llm_enabled` remains false by default and no ordinary agent may gain an LLM loop
  (`src/world/config.py:75-77`; AGENTS.md:86-89).
- **Testing:** green assertions must be capable of failing when the feature is removed
  (AGENTS.md:72-75).
