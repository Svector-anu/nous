# phase 3: soft llm limits, offline rest, and x402 seam

issue: #5

this branch will add soft rate limits, optional offline rest for user agents, and a seam for future x402 pay-to-force-decision.

see the issue for full acceptance and constraints.

## what shipped

- `advisor_status` in the world snapshot (`src/world/systems/leadership.py`) plus a
  non-blocking "Leader thinking…" indicator in the viewer.
- `Agent.rest_mode` and `RestQueue`/`src/world/systems/resting.py` for queued rest toggles.
- `POST /agents/{id}/rest` and a rest/resume button in the deploy and inspector panels.
- rest mode in `needs.py` and `fsm.py`: a resting agent breaks for hunger, rests when tired,
  and does not initiate raids (`combat.py`).
- `ForceDecisionQueue` and `POST /clans/{id}/force-decision` returning 402 x402 headers.

## ux clarity pass

- dedicated **My Agents** panel (first in the right card stack) listing deployed agents with
  plain status (alive/resting, clan, simple need state, rank), plus **Find**, **Rest/Resume**, and
  **Copy status** buttons.
- **World summary** sentence at the top of the world card: "Day 12. 94 people live here in 11
  clans. Food feels scarce — a raid in the east."
- page share metadata (`<title>` and `<meta name="description">`) and a `/favicon.ico` route.
- confusion pass: dock labels renamed to "My Agents", "Deploy", "Inspect", "World", "Clans",
  "Bets"; secondary controls (keyboard help, camera tours) hidden behind Settings; state labels
  like "SEEK_NEED" replaced with "looking for food or wood".

## gates

- tests: `416 passed` (includes `tests/test_rest_mode.py` and `tests/test_advisor_status.py`).
- browser: `39/39` via `node scripts/verify_world3d.mjs`.
