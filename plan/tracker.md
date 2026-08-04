# roadmap tracker

issue: #6

this is the meta tracker for the current Nous slice.

## phases

- #1 phase 0: visibility/aliveness pass — completed in main
- #2 phase 1a: readable, personality-aware leader messages without llm — completed in main
- #3 phase 2: user-agent standing and progression — pr #7 (completed)
- #4 phase 1b: llm-generated leader messages and reactions — pr #8 (completed)
- #5 phase 3: soft llm limits, offline rest, and x402 seam — pr #9

## rules for every phase

- measure first, report real numbers.
- sim core deterministic and $0 when llm_enabled=False.
- no network calls from the sim core.
- no unbounded accumulators.
- no mocks or invented thresholds without measurement.
- every new behaviour proven with hard tests that fail when the behaviour is missing.
- keep AGENTS.md and PLAN.md honest; if the doc and code disagree, the doc is the bug.

close this tracker when #3, #4, and #5 are closed.
