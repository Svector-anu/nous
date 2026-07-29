"""Minimal entity-component-system core.

Entities are integer ids. Components live in per-type stores keyed by entity id.
Every query returns entity ids in ascending order so that system behaviour does
not depend on dict insertion history — which is what makes replays reproducible.
"""

from __future__ import annotations

from typing import Callable, Iterable, Iterator, TypeVar

from .config import WorldConfig
from .rng import TickRng

Entity = int
TComponent = TypeVar("TComponent")


_MISSING = object()


class ComponentMissingError(KeyError):
    """Raised when a component is read from an entity that does not have it."""


class World:
    def __init__(self, config: WorldConfig) -> None:
        self.config = config
        self.tick: int = 0
        self._next_entity: Entity = 1
        self._alive: set[Entity] = set()
        self._stores: dict[type, dict[Entity, object]] = {}

    @property
    def entity_count(self) -> int:
        return len(self._alive)

    @property
    def next_entity_id(self) -> Entity:
        return self._next_entity

    def create_entity(self) -> Entity:
        entity = self._next_entity
        self._next_entity += 1
        self._alive.add(entity)
        return entity

    def destroy_entity(self, entity: Entity) -> None:
        self._alive.discard(entity)
        for store in self._stores.values():
            store.pop(entity, None)

    def is_alive(self, entity: Entity) -> bool:
        return entity in self._alive

    def add(self, entity: Entity, component: TComponent) -> TComponent:
        if entity not in self._alive:
            raise ValueError(f"entity {entity} is not alive")
        self._stores.setdefault(type(component), {})[entity] = component
        return component

    def get(self, entity: Entity, component_type: type[TComponent]) -> TComponent:
        store = self._stores.get(component_type)
        if store is not None:
            component = store.get(entity, _MISSING)
            if component is not _MISSING:
                return component  # type: ignore[return-value]
        raise ComponentMissingError(f"entity {entity} has no {component_type.__name__}")

    def try_get(
        self, entity: Entity, component_type: type[TComponent]
    ) -> TComponent | None:
        store = self._stores.get(component_type)
        if store is None:
            return None
        return store.get(entity)  # type: ignore[return-value]

    def has(self, entity: Entity, component_type: type) -> bool:
        store = self._stores.get(component_type)
        return store is not None and entity in store

    def remove(self, entity: Entity, component_type: type) -> None:
        store = self._stores.get(component_type)
        if store is not None:
            store.pop(entity, None)

    def query(self, *component_types: type) -> list[Entity]:
        """Entity ids holding all the given components, ascending.

        Intersects dict key views rather than testing membership per entity in Python:
        the key-view path runs in C and this is the single hottest call in the sim.
        """
        if not component_types:
            return sorted(self._alive)

        stores: list[dict[Entity, object]] = []
        for component_type in component_types:
            store = self._stores.get(component_type)
            if store is None:
                return []
            stores.append(store)

        smallest = min(stores, key=len)
        matched = set(smallest)
        for store in stores:
            if store is not smallest:
                matched &= store.keys()
        return sorted(matched)

    def first(self, component_type: type) -> Entity | None:
        """Lowest-id entity holding this component, without sorting the whole store.

        Singleton lookups (the blackboard) run per agent per tick; going through
        query() made them the hottest path in the simulation.
        """
        store = self._stores.get(component_type)
        if not store:
            return None
        return min(store)

    def component_types(self) -> list[type]:
        return sorted(self._stores, key=lambda t: t.__name__)

    def store(self, component_type: type[TComponent]) -> Iterator[tuple[Entity, TComponent]]:
        store = self._stores.get(component_type)
        if store is None:
            return iter(())
        return iter(sorted(store.items()))  # type: ignore[arg-type]

    def restore_entities(self, entities: Iterable[Entity], next_entity: Entity) -> None:
        """Reinstate ids exactly as saved. Ids are sparse once entities die, so the
        loader cannot replay create_entity() to rebuild them."""
        self._alive = set(entities)
        self._next_entity = max([next_entity, *self._alive, 1])


System = Callable[[World, TickRng], None]


class SystemRegistry:
    """Systems run in registration order, once per tick."""

    def __init__(self) -> None:
        self._systems: list[tuple[str, System]] = []

    def register(self, name: str, system: System) -> None:
        if any(existing == name for existing, _ in self._systems):
            raise ValueError(f"system {name!r} is already registered")
        self._systems.append((name, system))

    def names(self) -> list[str]:
        return [name for name, _ in self._systems]

    def run_all(self, world: World, seed: int, tick: int) -> None:
        for name, system in self._systems:
            system(world, TickRng(seed, tick, name))
