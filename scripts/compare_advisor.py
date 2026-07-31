"""Does the model actually disagree with the rules, and is it defensible when it does?

    .venv/bin/python -m scripts.compare_advisor --dry-run          # free, no calls
    .venv/bin/python -m scripts.compare_advisor --samples 40       # live, needs .env

This deliberately does *not* measure survival. Seed-to-seed population variance was measured
at sd 8.7 over six seeds (spread 92-117 at tick 8000), while the advisor is capped at 200
calls against ~3333 goal reviews — 6% of decisions. A 6% intervention cannot move a metric
whose noise floor is +/-25, so an A/B on population would burn credits to produce a number
nobody could interpret.

The answerable question is narrower and much cheaper: given the *identical* clan state, how
often does the model pick something the rules would not, and what does it say about why.
If it agrees nearly always, the tier is adding cost and no judgement, and that is worth
knowing for about a dollar.

Every state is drawn from a real simulation rather than invented, so the distribution of
situations is the one the world actually produces.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter

from src.env import load_env
from src.llm.advisor import PROVIDERS, build_advisor
from src.world.components import Clan
from src.world.config import WorldConfig
from src.world.systems.leadership import _brief, _nearby_clan_count
from src.world.tick import Simulation, create_world

# Sampling starts early on purpose. A settled world only ever produces two of the five
# goals; the scarcity and building situations happen in the first few hundred ticks and
# never come back, so waiting for a "mature" world throws away most of the decision space.
SETTLE_TICKS = 60
SAMPLE_EVERY = 25


def _rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def collect_states(seed: int, wanted: int) -> list:
    """Walk a real world and snapshot clan states as they arise.

    Sampling every SAMPLE_EVERY ticks across many clans gives a spread of situations —
    early scarcity, mid-game expansion, post-raid recovery — rather than many copies of
    one steady state, which would make an agreement rate meaningless.
    """
    simulation = Simulation(create_world(WorldConfig(seed=seed, llm_enabled=False)))
    simulation.run(SETTLE_TICKS)
    world = simulation.world

    # Stratified by what the rules answer. A settled world sits on `rally` almost always —
    # a first pass drew 26 of 30 states with the same rule answer, and an agreement rate
    # measured on that is close to meaningless. Capping each stratum forces the sample to
    # include the scarce, interesting situations: famine, timber shortage, expansion.
    per_goal = max(2, wanted // 3)
    briefs = []
    taken = Counter()
    seen = set()
    while len(briefs) < wanted:
        simulation.run(SAMPLE_EVERY)
        for entity in world.query(Clan):
            clan = world.get(entity, Clan)
            if not clan.members:
                continue
            brief = _brief(world, clan, _nearby_clan_count(world, clan))
            if taken[brief.rules_goal] >= per_goal:
                continue
            # One sample per distinct situation: identical states would inflate whichever
            # way that state happens to fall.
            key = (
                brief.members,
                round(brief.mean_hunger, 0),
                round(brief.food_ratio, 1),
                round(brief.huts_per_member, 1),
                brief.wood_held,
                brief.current_goal,
                brief.rules_goal,
            )
            if key in seen:
                continue
            seen.add(key)
            taken[brief.rules_goal] += 1
            briefs.append(brief)
            if len(briefs) >= wanted:
                break
        if world.tick > SETTLE_TICKS + 40000:
            break
    return briefs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20260728)
    parser.add_argument("--provider", default="dgrid", choices=[p for p in PROVIDERS if p != "none"])
    parser.add_argument("--model", default="anthropic/claude-opus-5")
    parser.add_argument("--dry-run", action="store_true", help="collect states, make no calls")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    load_env()

    _rule(f"1. collecting {args.samples} distinct clan states from a real world")
    briefs = collect_states(args.seed, args.samples)
    print(f"gathered {len(briefs)} states, seed {args.seed}")
    spread = Counter(b.rules_goal for b in briefs)
    print(f"rules would choose: {dict(spread)}")
    if len(spread) == 1:
        print("\nWARNING: every sampled state has the same rule answer. An agreement rate")
        print("measured here says little — the states are not varied enough to be a test.")

    if args.dry_run:
        print("\ndry run: no calls made, nothing spent.")
        return 0

    config = WorldConfig(
        llm_enabled=True,
        llm_provider=args.provider,
        llm_model=args.model,
        llm_max_calls_per_session=len(briefs) + 5,
        llm_max_inflight=1,
    )
    advisor = build_advisor(config)
    print(f"\nadvisor: {type(advisor).__name__} model={args.model}")

    _rule("2. asking the model, one state at a time")
    results = []
    started = time.monotonic()
    for index, brief in enumerate(briefs, 1):
        if not advisor.submit(brief):
            print(f"  [{index}] refused by budget or a disabled advisor; stopping")
            break
        decision = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            got = advisor.collect()
            if got:
                decision = got[0]
                break
            time.sleep(0.25)
        if decision is None:
            print(f"  [{index}] no answer inside 90s")
            continue
        agrees = decision.goal == brief.rules_goal
        results.append((brief, decision, agrees))
        mark = "same" if agrees else "DIFFERS"
        print(f"  [{index:>2}] rules={brief.rules_goal:<12} model={decision.goal:<12} {mark}")
    advisor.close()
    elapsed = time.monotonic() - started

    _rule("3. result")
    if not results:
        print("no decisions came back; nothing to report.")
        return 2

    agreed = sum(1 for _, _, a in results if a)
    differed = len(results) - agreed
    print(f"states asked   : {len(results)}")
    print(f"model agreed   : {agreed} ({agreed / len(results) * 100:.0f}%)")
    print(f"model differed : {differed} ({differed / len(results) * 100:.0f}%)")
    print(f"wall clock     : {elapsed:.0f}s for {len(results)} calls")

    if differed:
        _rule("4. every disagreement, in full")
        for brief, decision, agrees in results:
            if agrees:
                continue
            print(f"\nclan {brief.clan_id} at tick {brief.tick}")
            print(f"  members {brief.members}, hunger {brief.mean_hunger:.0f}, "
                  f"food {brief.food_ratio:.2f}, huts/member {brief.huts_per_member:.1f}, "
                  f"wood {brief.wood_held}, nearby {brief.nearby_clans}, "
                  f"raids suffered {brief.recent_raids_suffered}")
            print(f"  rules : {brief.rules_goal}")
            print(f"  model : {decision.goal}")
            print(f"  why   : {decision.reason}")

    _rule("verdict")
    rate = differed / len(results)
    if rate < 0.1:
        print(f"the model reproduced the rules {agreed}/{len(results)} times.")
        print("at this rate the tier is paying for judgement it is not exercising —")
        print("a paired survival test would be measuring almost nothing.")
    else:
        print(f"the model departed from the rules in {differed} of {len(results)} states.")
        print("read the reasoning above and judge whether those calls are defensible.")
        print("if they are, a *paired* survival test (same seed, advisor on vs off,")
        print("repeated across seeds) is now worth its cost — pairing cancels the")
        print("seed noise that makes an unpaired comparison useless.")
    print("\nthis measures judgement, not outcomes. it cannot tell you the world")
    print("survives better with an advisor; only that the advisor has an opinion.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
