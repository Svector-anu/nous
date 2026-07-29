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
    resource_count: int = 220

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

    max_huts_per_agent: int = 3
    global_build_stop_fraction: float = 0.6

    blackboard_ttl_ticks: int = 300
    blackboard_max_locations: int = 8
    inbox_capacity: int = 16
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

    # Hierarchical cognition. Off by default: the world must run at $0 without it, and
    # every one of these limits exists so enabling it cannot produce an unbounded bill.
    llm_enabled: bool = False
    llm_model: str = "claude-opus-5"
    llm_effort: str = "low"
    llm_max_inflight: int = 2
    llm_max_calls_per_session: int = 200
    # First-run safety valve: when set, only this clan is ever consulted. Keeps the
    # initial live test to a single clan and a handful of calls.
    llm_only_clan_id: int | None = None
    # How many times a request that was in flight across a restart may be re-sent before
    # being abandoned. Bounded so a crash loop cannot retry forever.
    llm_max_recovery_attempts: int = 2
    llm_min_ticks_between_calls: int = 300
    llm_timeout_seconds: float = 30.0
    llm_log_limit: int = 200

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
    clan_desperate_threshold: int = 20
    huts_per_member_target: float = 3.0

    def build_ceiling(self) -> int:
        """Hard cap on total buildings. Truncated so the last permitted placement
        still lands at or under the fraction, never one past it."""
        return int(self.grid_width * self.grid_height * self.global_build_stop_fraction)
