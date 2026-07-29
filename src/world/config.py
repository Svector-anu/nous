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

    goal_review_ticks: int = 120
    goal_bias_chance: float = 0.6
    influence_base_radius: int = 4
    influence_per_member: int = 1
    # Thresholds picked from measured steady-state spread, not guessed: clan food ratio
    # runs 0.38-1.00 (median 0.93) and mean hunger 22-49 (median 40), so these actually
    # split the population instead of never firing.
    clan_low_food_ratio: float = 0.85
    clan_hungry_threshold: int = 35
    huts_per_member_target: float = 3.0

    def build_ceiling(self) -> int:
        """Hard cap on total buildings. Truncated so the last permitted placement
        still lands at or under the fraction, never one past it."""
        return int(self.grid_width * self.grid_height * self.global_build_stop_fraction)
