"""One live model call for one clan, compared against the rules it must beat.

    .venv/bin/python -m scripts.verify_llm --scripted   # offline harness check, no key

    # live — pass the key inline so it never lands in a shell history file or a repo
    DGRID_API_KEY=... .venv/bin/python -m scripts.verify_llm \
        --provider dgrid --model anthropic/claude-opus-5

    ANTHROPIC_API_KEY=... .venv/bin/python -m scripts.verify_llm \
        --provider anthropic --model claude-opus-5

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
import time
from pathlib import Path

from src.env import load_env
from src.llm.advisor import PROVIDERS, AdvisorBudget, ScriptedAdvisor, build_advisor
from src.persistence.sqlite_store import SqliteWorldStore
from src.world.components import Clan, DecisionLog
from src.world.config import WorldConfig
from src.world.systems.leadership import log as decision_log
from src.world.tick import Simulation, create_world

SETTLE_TICKS = 400
WAIT_TICKS = 400
# A live request takes tens of seconds; ticks alone cannot wait for the network.
REPLY_GRACE_SECONDS = 90.0


def _rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scripted", action="store_true", help="offline harness check")
    parser.add_argument("--max-calls", type=int, default=3)
    parser.add_argument("--model", default="anthropic/claude-sonnet-4")
    parser.add_argument(
        "--provider",
        default="anthropic",
        choices=[p for p in PROVIDERS if p != "none"],
        help="dgrid routes to any provider through one gateway; models are provider/model",
    )
    parser.add_argument("--api-key-env", default="", help="override the env var holding the key")
    parser.add_argument("--seed", type=int, default=20260728)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_env()

    # Fail here, not 400 ticks in. A wrong-kind key is the single most likely reason a
    # live run does nothing, and the rules fallback hides it perfectly.
    if not args.scripted:
        import os

        key_env = args.api_key_env or {
            "dgrid": "DGRID_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "xai": "XAI_API_KEY",
            "openai": "OPENAI_API_KEY",
        }.get(args.provider, "")
        key = os.environ.get(key_env, "") if key_env else ""
        if key_env and not key:
            print(f"{key_env} is not set. put it in .env (see .env.example) or pass it inline.")
            return 1
        if key:
            print(f"credential : {key_env} present, starts {key[:3]!r}, {len(key)} chars")
        if args.provider == "dgrid" and key.startswith("mk-"):
            print(
                "\nthat is a MANAGEMENT key. it can list models but cannot call inference,\n"
                "which is why /v1/models works and /v1/chat/completions returns 401.\n"
                "create a model key (sk-...) in the dgrid console under Model API Keys."
            )
            return 1

    config = WorldConfig(
        seed=args.seed, llm_enabled=True, llm_model=args.model, llm_provider=args.provider
    )
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
        llm_provider=args.provider,
        llm_api_key_env=args.api_key_env,
        llm_only_clan_id=target.clan_id,
        llm_min_ticks_between_calls=50,
        llm_max_calls_per_session=args.max_calls,
        llm_max_inflight=1,
    )
    world.config = config

    if args.scripted:
        world.advisor = ScriptedAdvisor({target.clan_id: "expand"}, lag_calls=1, reason="offline harness")
        print("advisor: ScriptedAdvisor (offline)")
    else:
        # Built through the same factory the server uses, so this verifies the real path
        # rather than a bespoke one that might diverge from it.
        world.advisor = build_advisor(config)
        print(
            f"advisor: {type(world.advisor).__name__} provider={args.provider} "
            f"model={args.model} max_calls={args.max_calls}"
        )

    _rule("2. waiting for a decision")
    before = len(decision_log(world).entries)
    llm_entries: list[dict] = []

    def _llm_entries() -> list[dict]:
        return [e for e in decision_log(world).entries if e["source"] == "llm"]

    for _ in range(WAIT_TICKS):
        simulation.step()
        llm_entries = _llm_entries()
        if llm_entries:
            break

    # Ticks are the sim's clock, not the network's. 400 steps run in about a second here
    # while a real request takes tens of seconds, so the loop above always finished first
    # and reported failure while the answer was still in flight — pending=1 was the tell.
    # Keep stepping in wall-clock time for as long as the advisor still owes us something.
    if not llm_entries and world.advisor.pending():
        deadline = time.monotonic() + REPLY_GRACE_SECONDS
        print(f"request still in flight; waiting up to {REPLY_GRACE_SECONDS:.0f}s for it")
        while time.monotonic() < deadline and world.advisor.pending():
            simulation.step()
            llm_entries = _llm_entries()
            if llm_entries:
                break
            time.sleep(0.25)
        # One last tick: a reply collected on the final pass is applied on the next one.
        if not llm_entries:
            simulation.step()
            llm_entries = _llm_entries()

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
    print(f"message     : {entry.get('message', '')}")
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
