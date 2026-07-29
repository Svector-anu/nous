"""One live Claude call for one clan, compared against the rules it must beat.

    .venv/bin/python -m scripts.verify_llm            # live, needs credentials
    .venv/bin/python -m scripts.verify_llm --scripted # offline harness check

Settles a world, picks a single clan, consults the model for that clan only, and reports:
the exact prompt sent, the raw response, the goal chosen, what `_choose_goal` would have
chosen in the identical state, and whether the decision survived a save/reload.

Deliberately bounded: one clan, and `--max-calls` (default 3) total requests.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

from src.llm.advisor import AdvisorBudget, ClaudeAdvisor, ScriptedAdvisor
from src.persistence.sqlite_store import SqliteWorldStore
from src.world.components import Clan, DecisionLog
from src.world.config import WorldConfig
from src.world.systems.leadership import log as decision_log
from src.world.tick import Simulation, create_world

SETTLE_TICKS = 400
WAIT_TICKS = 400


def _rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scripted", action="store_true", help="offline harness check")
    parser.add_argument("--max-calls", type=int, default=3)
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--seed", type=int, default=20260728)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = WorldConfig(seed=args.seed, llm_enabled=True, llm_model=args.model)
    simulation = Simulation(create_world(config))
    world = simulation.world

    _rule("1. settling the world")
    simulation.run(SETTLE_TICKS)
    clans = sorted(
        (world.get(e, Clan) for e in world.query(Clan)),
        key=lambda c: (-len(c.members), c.clan_id),
    )
    if not clans:
        print("no clans formed; nothing to consult")
        return 1
    target = clans[0]
    print(f"tick {world.tick}, {len(clans)} clans; consulting clan {target.clan_id} "
          f"({len(target.members)} members, goal {target.goal!r})")

    # Rebuild the config with the clan pinned, so only this one is ever consulted.
    config = WorldConfig(
        seed=args.seed,
        llm_enabled=True,
        llm_model=args.model,
        llm_only_clan_id=target.clan_id,
        llm_min_ticks_between_calls=50,
    )
    world.config = config

    if args.scripted:
        world.advisor = ScriptedAdvisor({target.clan_id: "expand"}, lag_calls=1, reason="offline harness")
        print("advisor: ScriptedAdvisor (offline)")
    else:
        world.advisor = ClaudeAdvisor(
            model=args.model,
            budget=AdvisorBudget(max_inflight=1, max_calls_per_session=args.max_calls),
        )
        print(f"advisor: ClaudeAdvisor model={args.model} max_calls={args.max_calls}")

    _rule("2. waiting for a decision")
    before = len(decision_log(world).entries)
    llm_entries: list[dict] = []
    for _ in range(WAIT_TICKS):
        simulation.step()
        llm_entries = [e for e in decision_log(world).entries if e["source"] == "llm"]
        if llm_entries:
            break

    if not llm_entries:
        print(f"no model decision arrived in {WAIT_TICKS} ticks.")
        advisor = world.advisor
        reason = getattr(advisor, "_unavailable_reason", None)
        print(f"advisor state: pending={advisor.pending()} unavailable={reason!r}")
        print("\nthe rules kept the world running throughout — that is the fallback working,")
        print("but it means the live path is still unverified.")
        return 2

    entry = llm_entries[-1]

    _rule("3. the exchange")
    print(f"[prompt sent]\n{entry['prompt']}\n")
    print(f"[raw response]\n{entry['raw_response']}\n")
    print(f"model chose : {entry['goal']}")
    print(f"rules chose : {entry['rules_goal']}")
    print(f"verdict     : {entry['verdict']}")
    print(f"reason      : {entry['reason']}")
    print(f"latency     : {entry['latency_ms']} ms")

    _rule("4. applied to the world")
    clan = next(world.get(e, Clan) for e in world.query(Clan) if world.get(e, Clan).clan_id == target.clan_id)
    applied = clan.goal == entry["goal"] and clan.goal_source == "llm"
    print(f"clan {clan.clan_id} goal={clan.goal!r} source={clan.goal_source!r} -> "
          f"{'APPLIED' if applied else 'NOT APPLIED'}")
    print(f"decision arrived at tick {entry['tick']}, after the tick it was requested on")

    _rule("5. survives save/reload")
    path = Path(tempfile.mkdtemp()) / "verify.db"
    store = SqliteWorldStore(path)
    store.save(world)
    store.close()
    reloaded = SqliteWorldStore(path).load()
    reloaded_entries = [e for e in decision_log(reloaded).entries if e["source"] == "llm"]
    reloaded_clan = next(
        (reloaded.get(e, Clan) for e in reloaded.query(Clan)
         if reloaded.get(e, Clan).clan_id == target.clan_id),
        None,
    )
    print(f"decision log survived : {reloaded_entries == llm_entries}")
    print(f"clan goal survived    : "
          f"{reloaded_clan is not None and reloaded_clan.goal == clan.goal and reloaded_clan.goal_source == 'llm'}")

    _rule("verdict")
    print(f"the model chose {entry['goal']!r}; the rules would have chosen "
          f"{entry['rules_goal']!r} in the same state ({entry['verdict']}).")
    print("whether that is *better* needs the world run both ways from here —")
    print("a single decision cannot answer it.")

    if world.advisor is not None:
        world.advisor.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
