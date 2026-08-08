"""World genesis, the fixed-timestep tick loop, and the viewer snapshot."""

from __future__ import annotations

import hashlib
from typing import Protocol

from .components import (
    AdvisorState,
    Agent,
    Blackboard,
    Building,
    Clan,
    ClanRef,
    DecisionLog,
    EscrowBook,
    ForceDecisionQueue,
    IdentityQueue,
    Inbox,
    Inventory,
    MarketBook,
    MessageLog,
    Needs,
    Outbox,
    Position,
    ResourceKind,
    ResourceNode,
    RestQueue,
    SpawnQueue,
    SpendBook,
    Standing,
)
from .config import TICKS_PER_DAY, WorldConfig
from .ecs import SystemRegistry, World
from .rng import TickRng
from . import spend
from .systems import (
    blackboard,
    build,
    combat,
    fsm,
    identity,
    leadership,
    markets,
    messaging,
    movement,
    needs,
    regrowth,
    resting,
    social,
    spawning,
    standing,
    trade,
)

_NAME_PREFIXES = ("Ka", "Mor", "Tel", "Ash", "Rin", "Vos", "Dor", "Ely", "Bran", "Sev")
_NAME_SUFFIXES = ("ra", "nix", "wyn", "dor", "sha", "lek", "mir", "tas", "ven", "oth")


class WorldStore(Protocol):
    def save(self, world: World) -> None: ...


def build_registry() -> SystemRegistry:
    """Messaging first so the fsm sees last tick's mail; social and blackboard last so
    what they publish is read on the next tick. Every social channel has the same
    one-tick lag."""
    registry = SystemRegistry()
    registry.register("spawning", spawning.run)
    # Straight after spawning: a label attaches to an agent on the same tick it becomes
    # visible. Writes only Agent.owner_address, which no system reads.
    registry.register("identity", identity.run)
    registry.register("messaging", messaging.run)
    registry.register("needs", needs.run)
    registry.register("trade", trade.run)
    registry.register("resting", resting.run)
    registry.register("combat", combat.run)
    registry.register("fsm", fsm.run)
    registry.register("movement", movement.run)
    registry.register("build", build.run)
    registry.register("regrowth", regrowth.run)
    registry.register("leadership", leadership.run)
    registry.register("social", social.run)
    registry.register("standing", standing.run)
    registry.register("blackboard", blackboard.run)
    # Last: a market resolves against the tick's final state, so it must run after every
    # system that can change the world. It only ever reads that state.
    registry.register("markets", markets.run)
    return registry


def _agent_name(rng: TickRng, index: int) -> str:
    prefix = _NAME_PREFIXES[rng.below(len(_NAME_PREFIXES))]
    suffix = _NAME_SUFFIXES[rng.below(len(_NAME_SUFFIXES))]
    return f"{prefix}{suffix}-{index:03d}"


def create_world(config: WorldConfig) -> World:
    """Populate a fresh world. Identical for identical seeds."""
    world = World(config)
    rng = TickRng(config.seed, 0, "genesis")

    world.add(world.create_entity(), Blackboard())
    world.add(world.create_entity(), SpawnQueue())
    world.add(world.create_entity(), DecisionLog())
    world.add(world.create_entity(), MessageLog())
    world.add(world.create_entity(), AdvisorState())
    world.add(world.create_entity(), RestQueue())
    world.add(world.create_entity(), ForceDecisionQueue())
    world.add(world.create_entity(), IdentityQueue())
    world.add(world.create_entity(), EscrowBook())
    world.add(world.create_entity(), MarketBook())
    # Disabled, unfunded and unhalted. A new world can no more spend money than an
    # old one — the envelope starts at zero and only an operator raises it.
    world.add(world.create_entity(), SpendBook())

    for _ in range(config.resource_count):
        entity = world.create_entity()
        world.add(entity, Position(rng.below(config.grid_width), rng.below(config.grid_height)))
        kind = ResourceKind.FOOD if rng.chance(0.5) else ResourceKind.WOOD
        capacity = rng.between(config.node_min_amount, config.node_max_amount)
        world.add(
            entity,
            ResourceNode(
                kind=kind,
                amount=capacity,
                max_amount=capacity,
                regrow_every=max(1, config.node_full_regrow_ticks // capacity),
            ),
        )

    for index in range(config.agent_count):
        entity = world.create_entity()
        world.add(entity, Position(rng.below(config.grid_width), rng.below(config.grid_height)))
        world.add(
            entity,
            Needs(
                energy=rng.between(config.need_max // 2, config.need_max),
                hunger=rng.between(config.need_max // 2, config.need_max),
                social=config.need_max,
                safety=config.need_max,
            ),
        )
        world.add(entity, Inventory())
        world.add(entity, ClanRef())
        world.add(entity, Inbox())
        world.add(entity, Outbox())
        world.add(entity, Agent(name=_agent_name(rng, index), spawn_tick=0))
        world.add(entity, Standing())

    return world


# Singleton components a world needs one of. Kept as a list rather than inlined so adding
# one is a single edit and cannot be half-done.
_SINGLETONS = (
    Blackboard,
    SpawnQueue,
    DecisionLog,
    MessageLog,
    AdvisorState,
    RestQueue,
    ForceDecisionQueue,
    IdentityQueue,
    EscrowBook,
    MarketBook,
    SpendBook,
)


def ensure_singletons(world: World) -> list[str]:
    """Give a resumed world any singleton component it was saved without.

    A world saved before a component existed has no entity carrying it, and nothing in the
    load path creates one — so the feature is silently unreachable on exactly the worlds
    that have been running longest. SpendBook found this: the live world would have
    reported spending permanently disabled with no way to enable it, and no error to say
    why.

    Additive and idempotent. Only ever creates what is missing, never touches what is
    there, so a resumed world keeps its history and a fresh one is unchanged.
    """
    added = []
    for component_type in _SINGLETONS:
        if world.first(component_type) is None:
            world.add(world.create_entity(), component_type())
            added.append(component_type.__name__)
    return added


def state_hash(world: World) -> str:
    """Stable fingerprint of the whole world, used by the determinism tests."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(f"tick={world.tick}".encode())

    for component_type in world.component_types():
        digest.update(component_type.__name__.encode())
        for entity, component in world.store(component_type):
            digest.update(f"{entity}:{component!r};".encode())

    return digest.hexdigest()


def _paid_receipts(world: World) -> list[dict]:
    """Recent paid decisions, newest last. Empty for the overwhelming majority of ticks."""
    entity = world.first(ForceDecisionQueue)
    if entity is None:
        return []
    return [dict(entry) for entry in world.get(entity, ForceDecisionQueue).applied]


class Simulation:
    def __init__(
        self,
        world: World,
        registry: SystemRegistry | None = None,
        store: WorldStore | None = None,
    ) -> None:
        self.world = world
        self.registry = registry if registry is not None else build_registry()
        self.store = store

    @property
    def day(self) -> int:
        return self.world.tick // TICKS_PER_DAY

    def step(self) -> None:
        self.world.tick += 1
        self.registry.run_all(self.world, self.world.config.seed, self.world.tick)

        save_every = self.world.config.save_every_ticks
        if self.store is not None and save_every > 0 and self.world.tick % save_every == 0:
            self.store.save(self.world)

    def run(self, ticks: int) -> None:
        for _ in range(ticks):
            self.step()

    def snapshot(self) -> dict:
        world = self.world
        agents = []
        for entity in world.query(Agent, Position, Needs, Inventory):
            agent = world.get(entity, Agent)
            position = world.get(entity, Position)
            agent_needs = world.get(entity, Needs)
            inventory = world.get(entity, Inventory)
            reference = world.try_get(entity, ClanRef)
            agents.append(
                {
                    "id": entity,
                    "name": agent.name,
                    "x": position.x,
                    "y": position.y,
                    "state": agent.state.value,
                    "energy": agent_needs.energy,
                    "hunger": agent_needs.hunger,
                    "food": inventory.food,
                    "wood": inventory.wood,
                    "clan": reference.clan_id if reference is not None else None,
                    "user": agent.user_deployed,
                    "personality": agent.personality,
                    "wants": agent.wants.value,
                    "huts": agent.huts_owned,
                    "raids_won": agent.raids_won,
                    "raids_lost": agent.raids_lost,
                    "received": agent.received,
                    "target": (
                        [agent.target_x, agent.target_y]
                        if agent.target_x is not None and agent.target_y is not None
                        else None
                    ),
                    "standing": (
                        world.get(entity, Standing).value
                        if world.has(entity, Standing)
                        else 0
                    ),
                    "rank": (
                        world.get(entity, Standing).rank
                        if world.has(entity, Standing)
                        else "member"
                    ),
                    "rest_mode": agent.rest_mode,
                    "owner": agent.owner_address,
                }
            )

        clans = []
        for entity in world.query(Clan):
            clan = world.get(entity, Clan)
            clans.append(
                {
                    "id": clan.clan_id,
                    "leader": clan.leader,
                    "size": len(clan.members),
                    "goal": clan.goal,
                    "goal_source": clan.goal_source,
                    "goal_reason": clan.goal_reason,
                    "centre": clan.centre,
                    "influence": clan.influence_radius,
                    "allies": list(clan.allies),
                    # {"attacker_clan_id": [times, last_tick]} — the social history the
                    # viewer had no way to show before.
                    "grudges": {k: list(v) for k, v in clan.grudges.items()},
                }
            )

        resources = []
        dormant = 0
        for entity in world.query(ResourceNode, Position):
            node = world.get(entity, ResourceNode)
            position = world.get(entity, Position)
            if node.is_dormant:
                dormant += 1
            resources.append(
                {
                    "id": entity,
                    "x": position.x,
                    "y": position.y,
                    "kind": node.kind.value,
                    "amount": node.amount,
                    "max": node.max_amount,
                }
            )

        buildings = []
        for entity in world.query(Building, Position):
            building = world.get(entity, Building)
            position = world.get(entity, Position)
            buildings.append(
                {
                    "id": entity,
                    "x": position.x,
                    "y": position.y,
                    "kind": building.kind,
                    "owner": building.owner,
                }
            )

        message_log_entity = world.first(MessageLog)
        messages = []
        if message_log_entity is not None:
            messages = list(world.get(message_log_entity, MessageLog).entries)

        return {
            "tick": world.tick,
            "day": self.day,
            "grid": {"width": world.config.grid_width, "height": world.config.grid_height},
            "agents": agents,
            "resources": resources,
            "buildings": buildings,
            "clans": clans,
            "messages": messages,
            "stats": {
                "agents": len(agents),
                "resources": len(resources) - dormant,
                "dormant": dormant,
                "buildings": len(buildings),
                "clans": len(clans),
                "clanned": sum(1 for a in agents if a["clan"] is not None),
                "user_agents": sum(1 for a in agents if a["user"]),
            },
            "advisor": leadership.advisor_status(world),
            # Real money the world may spend on itself. Surfaced so a monitor can
            # alert on a halt or an exhausted envelope without reading world state.
            "spend": spend.status(world),
            # Receipts for paid decisions. A payment that leaves no trace in the world
            # is indistinguishable from one that did nothing, so the world says who
            # bought what and carries the transaction it was bought with.
            "paid": _paid_receipts(world),
        }
