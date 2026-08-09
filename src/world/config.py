"""Tunable Phase 0 world parameters.

Values not fixed by planish/ are defaults chosen to keep a 1 tick/second world
legible in the browser; they are all overridable.
"""

from __future__ import annotations

from dataclasses import dataclass

TICKS_PER_DAY = 200


@dataclass(frozen=True)
class WorldConfig:
    seed: int = 20260728
    grid_width: int = 64
    grid_height: int = 64
    agent_count: int = 120
    resource_count: int = 210

    tick_seconds: float = 1.0
    save_every_ticks: int = 50

    need_max: int = 100
    energy_decay_per_tick: int = 1
    hunger_decay_per_tick: int = 1
    need_threshold: int = 20
    rest_energy_per_tick: int = 6
    eat_hunger_restored: int = 45

    gather_ticks: int = 3
    carry_capacity: int = 5
    wood_per_hut: int = 5
    vision_radius: int = 12

    spatial_chunk_size: int = 8

    node_full_regrow_ticks: int = 300
    node_min_amount: int = 4
    node_max_amount: int = 12

    max_huts_per_agent: int = 5
    global_build_stop_fraction: float = 0.6

    blackboard_ttl_ticks: int = 300
    blackboard_max_locations: int = 8
    inbox_capacity: int = 16
    message_log_limit: int = 128
    clan_form_radius: int = 3
    clan_form_chance: float = 0.25
    max_clan_size: int = 8
    rally_arrival_radius: int = 2
    social_energy_floor: int = 60

    transfer_radius: int = 2
    surplus_reserve: int = 2
    request_cooldown_ticks: int = 40

    max_pending_spawns: int = 32
    max_user_agents: int = 50
    max_name_length: int = 24
    max_personality_length: int = 140

    combat_energy_cost: int = 5
    combat_loser_penalty: int = 10
    raid_steal_amount: int = 2
    flee_distance: int = 6

    # A truce forms between two clans that have hurt each other, once neither is
    # desperate and the fighting has actually stopped.
    truce_peace_ticks: int = 200
    truce_contact_slack: int = 6

    # Hierarchical cognition. Off by default: the world must run at $0 without it, and
    # every one of these limits exists so enabling it cannot produce an unbounded bill.
    llm_enabled: bool = False
    # "anthropic" | "xai" | "openai" | "none". The openai path also serves openrouter,
    # ollama, vllm and lm studio — they differ only by llm_base_url.
    llm_provider: str = "dgrid"
    llm_model: str = "anthropic/claude-sonnet-4"
    llm_base_url: str = ""
    llm_api_key_env: str = ""
    llm_effort: str = "low"
    llm_max_inflight: int = 2
    llm_max_calls_per_session: int = 200
    # First-run safety valve: when set, only this clan is ever consulted. Keeps the
    # initial live test to a single clan and a handful of calls.
    llm_only_clan_id: int | None = None
    # How many times a request that was in flight across a restart may be re-sent before
    # being abandoned. Bounded so a crash loop cannot retry forever.
    llm_max_recovery_attempts: int = 2
    # Per-clan cooldown, and the setting that decides what a world costs to run. Every
    # clan is asked independently, so the spend scales with the clan count: at 300 ticks
    # a mature world of ~23 clans asks about 13,000 times a real day, which was invisible
    # when this default was chosen against a world that had three. 3000 keeps a live world
    # answering roughly once a minute — still visible to anyone watching — for a twentieth
    # of the calls. Override per deployment with LLM_MIN_TICKS_BETWEEN_CALLS.
    llm_min_ticks_between_calls: int = 3000
    llm_timeout_seconds: float = 30.0
    llm_log_limit: int = 200
    # Minds a visitor attached to their own agent. Off by default: an endpoint on
    # somebody else's machine must never be reached until an operator says so.
    attached_minds_enabled: bool = False
    # Per agent, so one busy mind cannot crowd out another. 20 ticks is a decision every
    # twenty seconds, which is frequent enough to look alive and slow enough that a
    # visitor's own bill stays small.
    attached_mind_cooldown_ticks: int = 20
    # Seam for future x402 / pay-to-force-decision. Disabled by default.
    llm_force_decision_enabled: bool = False

    # --- prediction markets ---------------------------------------------------
    # Spectators bet on world events. Reads world state, never writes to it.
    markets_enabled: bool = True
    # A scheduled market opens on this cadence, so there is always something to bet on
    # even in a quiet world. Event-driven markets open on top of these.
    market_open_every_ticks: int = 400
    # How long a market takes to settle, and how long before that betting closes.
    market_horizon_ticks: int = 1200
    market_lock_before_ticks: int = 400
    market_max_open: int = 8
    # Settled markets keep their audit trail, but not forever.
    market_history_limit: int = 60
    # Demo money. There is no real currency in this system.
    market_starting_balance: int = 1000
    market_max_stake: int = 100

    # --- chain identity and payments ------------------------------------------
    # Robinhood Chain. These are network coordinates, not credentials: the rpc url can
    # carry a provider key, so it lives in the environment (src/chain/settings.py) and
    # never in this dataclass — WorldConfig is persisted verbatim into world_meta.
    #
    # Nothing here changes a simulation rule. An address is a label on an agent, worth
    # exactly zero in-world; see AGENTS.md and the "do not reopen" note in NEXT.md.
    chain_id: int = 4663
    chain_testnet_id: int = 46630
    chain_name: str = "Robinhood Chain"
    chain_explorer_url: str = "https://robinhoodchain.blockscout.com"
    # Wallet linking. Off by default: an unconfigured deploy must not advertise a
    # connect button that cannot verify anything.
    chain_identity_enabled: bool = False
    # One link request per user agent is plenty, and the bound stops the queue from
    # becoming the unbounded accumulator every other queue here is capped to avoid.
    identity_queue_limit: int = 32
    # A verified payment proof is remembered so it cannot be replayed. Bounded for the
    # same reason.
    x402_spent_limit: int = 256

    # x402 pay-to-act. "header" trusts an upstream proxy's verdict (the existing seam,
    # useful in tests and behind a gateway); "chain" verifies a transaction receipt
    # against the rpc. Live payments need X402_ENABLED=true *and* a reachable rpc.
    x402_enabled: bool = False
    x402_verifier: str = "header"
    x402_price: str = "0.10"
    x402_currency: str = "USDG"

    # Funding the world's own thinking. A visitor pays what they like and the amount is
    # credited to the spend envelope, so the people watching pay for the minds instead of
    # the operator. Off by default, because taking money for compute is a decision.
    #
    # The floor exists so a dust transfer cannot mint an approval: every credit is an
    # entry in the audit trail, and one worth 0.000001 is noise in a record that is
    # supposed to answer "who paid for this".
    world_funding_enabled: bool = False
    world_funding_minimum: str = "0.10"
    # What share of a paid decision the clan's leader keeps as its own earnings. The rest
    # stays in the envelope for any clan to draw on.
    #
    # This is the only way an agent earns, and deliberately so: a balance is a *claim* on
    # the pool, so minting one for winning a raid or building a hut would create claims the
    # world has no money behind. Earnings can only come from money that actually arrived.
    agent_earning_share: float = 0.5

    # Real-money markets. Off until an operator configures custody; the demo credit
    # markets above are unaffected either way and stay labelled demo in the viewer.
    real_money_enabled: bool = False

    goal_review_ticks: int = 120
    goal_bias_chance: float = 0.6
    influence_base_radius: int = 4
    influence_per_member: int = 1
    # Thresholds picked from measured steady-state spread, not guessed: clan food ratio
    # runs 0.38-1.00 (median 0.93) and mean hunger 22-49 (median 40), so these actually
    # split the population instead of never firing.
    clan_low_food_ratio: float = 0.85
    clan_hungry_threshold: int = 35
    # Raiding needs real desperation, not merely empty stores. Measured mean clan hunger
    # never drops below 22 in a healthy world, so a bar under that means raids happen
    # during genuine famine and essentially never otherwise.
    clan_desperate_threshold: int = 25
    huts_per_member_target: float = 3.0

    # Standing and rank progression for user-deployed agents. Standing only increases from
    # recorded actions; it is never granted for free.
    standing_trusted_threshold: int = 60
    standing_officer_threshold: int = 150
    standing_per_hut_built: int = 10
    standing_per_food_given: int = 3
    standing_per_wood_given: int = 3
    standing_per_raid_won: int = 40
    standing_per_survival_ticks: int = 200  # standing +1 every 200 ticks alive
    standing_per_membership_ticks: int = 100  # standing +1 every 100 ticks in a clan

    def build_ceiling(self) -> int:
        """Hard cap on total buildings. Truncated so the last permitted placement
        still lands at or under the fraction, never one past it."""
        return int(self.grid_width * self.grid_height * self.global_build_stop_fraction)
