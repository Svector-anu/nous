"""World genesis, the fixed-timestep tick loop, and the viewer snapshot."""

from __future__ import annotations

import hashlib
from typing import Protocol

from .components import (
    Agent,
    Blackboard,
    Building,
    Clan,
    ClanRef,
    Inbox,
    Inventory,
    Needs,
    Outbox,
    Position,
    ResourceKind,
    ResourceNode,
)
from .config import TICKS_PER_DAY, WorldConfig
from .ecs import SystemRegistry, World
from .rng import TickRng
from .systems import blackboard, build, fsm, messaging, movement, needs, regrowth, social

_NAME_PREFIXES = ("Ka", "Mor", "Tel", "Ash", "Rin", "Vos", "Dor", "Ely", "Bran", "Sev")
_NAME_SUFFIXES = ("ra", "nix", "wyn", "dor", "sha", "lek", "mir", "tas", "ven", "oth")


class WorldStore(Protocol):
    def save(self, world: World) -> None: ...


def build_registry() -> SystemRegistry:
    """Messaging first so the fsm sees last tick's mail; social and blackboard last so
    what they publish is read on the next tick. Every social channel has the same
    one-tick lag."""
    registry = SystemRegistry()
    registry.register("messaging", messaging.run)
    registry.register("needs", needs.run)
    registry.register("fsm", fsm.run)
    registry.register("movement", movement.run)
    registry.register("build", build.run)
    registry.register("regrowth", regrowth.run)
    registry.register("social", social.run)
    registry.register("blackboard", blackboard.run)
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
        world.add(entity, Agent(name=_agent_name(rng, index)))

    return world


def state_hash(world: World) -> str:
    """Stable fingerprint of the whole world, used by the determinism tests."""
    digest = hashlib.blake2b(digest_size=16)
    digest.update(f"tick={world.tick}".encode())

    for component_type in world.component_types():
        digest.update(component_type.__name__.encode())
        for entity, component in world.store(component_type):
            digest.update(f"{entity}:{component!r};".encode())

    return digest.hexdigest()


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
                {"id": entity, "x": position.x, "y": position.y, "kind": building.kind}
            )

        return {
            "tick": world.tick,
            "day": self.day,
            "grid": {"width": world.config.grid_width, "height": world.config.grid_height},
            "agents": agents,
            "resources": resources,
            "buildings": buildings,
            "clans": clans,
            "stats": {
                "agents": len(agents),
                "resources": len(resources) - dormant,
                "dormant": dormant,
                "buildings": len(buildings),
                "clans": len(clans),
                "clanned": sum(1 for a in agents if a["clan"] is not None),
            },
        }
