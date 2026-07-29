"""Needs decay, eating, resting, and death.

Runs first each tick so the FSM always decides against fresh need values.

A starving agent — one whose hunger has bottomed out at 0 — cannot rest. Energy still
decays at the normal rate, so starvation kills by denying the only recovery mechanism
rather than by adding damage. Eating restores access to REST on the same tick.
"""

from __future__ import annotations

from ..components import Agent, AgentState, Inventory, Needs, ResourceKind
from ..config import WorldConfig
from ..ecs import World
from ..rng import TickRng

_UNINTERRUPTIBLE = (AgentState.REST, AgentState.FLEE, AgentState.IDLE)


def is_starving(needs: Needs) -> bool:
    return needs.hunger <= 0


def _already_addressing(agent: Agent, needs: Needs, config: WorldConfig) -> bool:
    """Is the agent's current activity the right response to its crossed need?

    Low energy is normally answered by resting, but a starving agent cannot rest — for
    it, food is the answer to both needs. Without that exemption a starving, exhausted
    agent is bounced back to IDLE every tick and never reaches the food it needs.
    """
    if needs.energy < config.need_threshold and not is_starving(needs):
        return agent.state is AgentState.REST
    if needs.hunger < config.need_threshold:
        return (
            agent.state in (AgentState.SEEK_NEED, AgentState.GATHER)
            and agent.wants is ResourceKind.FOOD
        )
    return True


def run(world: World, rng: TickRng) -> None:
    config = world.config

    for entity in world.query(Agent, Needs, Inventory):
        agent = world.get(entity, Agent)
        needs = world.get(entity, Needs)
        inventory = world.get(entity, Inventory)

        needs.hunger = max(0, needs.hunger - config.hunger_decay_per_tick)

        # Eating is resolved before energy, so an agent that feeds itself this tick is
        # treated as fed for the rest of the tick and keeps its rest.
        if needs.hunger < config.need_threshold and inventory.food > 0:
            inventory.food -= 1
            needs.hunger = min(config.need_max, needs.hunger + config.eat_hunger_restored)

        needs.energy = max(0, needs.energy - config.energy_decay_per_tick)

        if agent.state is AgentState.REST:
            if is_starving(needs):
                agent.state = AgentState.IDLE
                agent.clear_target()
            else:
                needs.energy = min(
                    config.need_max, needs.energy + config.rest_energy_per_tick
                )

        if needs.energy <= 0:
            world.destroy_entity(entity)
            continue

        # A need crossing its threshold preempts whatever the agent was doing, so a
        # long errand can't run an agent to death. Two exemptions matter: a rest is
        # never cut short (interrupting it thrashes an agent at the threshold and
        # kills it), and an agent already fetching what it needs is left alone —
        # without that, an agent whose hunger is pinned at 0 is reset to IDLE every
        # tick and never gets far enough to eat.
        if agent.state in _UNINTERRUPTIBLE:
            continue

        crossed = (
            needs.energy < config.need_threshold or needs.hunger < config.need_threshold
        )
        if crossed and not _already_addressing(agent, needs, config):
            agent.state = AgentState.IDLE
            agent.clear_target()
