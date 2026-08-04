"""SQLite save/load for the whole world.

The schema is deliberately generic: one row per component, serialised as JSON.
Phase 0 changes component shapes constantly, and a generic store means those
changes do not require a migration. A save captures (seed, tick, next entity id,
every component), which is everything needed to resume an identical run.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import get_type_hints

from ..world import components as component_module
from ..world.config import WorldConfig
from ..world.ecs import World

COMPONENT_TYPES: tuple[type, ...] = (
    component_module.Position,
    component_module.Needs,
    component_module.Inventory,
    component_module.ClanRef,
    component_module.Agent,
    component_module.ResourceNode,
    component_module.Building,
    component_module.Blackboard,
    component_module.Inbox,
    component_module.Outbox,
    component_module.Clan,
    component_module.Standing,
    component_module.SpawnQueue,
    component_module.DecisionLog,
    component_module.MessageLog,
    component_module.AdvisorState,
    component_module.RestQueue,
    component_module.ForceDecisionQueue,
    component_module.MarketBook,
)

_BY_NAME = {component_type.__name__: component_type for component_type in COMPONENT_TYPES}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS world_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS components (
    entity INTEGER NOT NULL,
    kind   TEXT    NOT NULL,
    data   TEXT    NOT NULL,
    PRIMARY KEY (entity, kind)
);
"""


def _enum_fields(component_type: type) -> dict[str, type[Enum]]:
    hints = get_type_hints(component_type)
    return {
        field.name: hints[field.name]
        for field in fields(component_type)
        if isinstance(hints.get(field.name), type) and issubclass(hints[field.name], Enum)
    }


_ENUM_FIELDS = {
    component_type.__name__: _enum_fields(component_type)
    for component_type in COMPONENT_TYPES
    if is_dataclass(component_type)
}


def _encode(component: object) -> str:
    payload = {
        field.name: getattr(component, field.name)
        for field in fields(component)  # type: ignore[arg-type]
    }
    for key, value in payload.items():
        if isinstance(value, Enum):
            payload[key] = value.value
    # Deliberately not sort_keys: a save must round-trip to the *same* iteration order,
    # not merely the same content. Sorting here would reorder nested dicts (message
    # envelopes, blackboard entries) so a reloaded world iterated them differently from
    # a live one. The output is stable regardless, because the simulation is.
    return json.dumps(payload)


def _decode(kind: str, data: str) -> object:
    component_type = _BY_NAME[kind]
    payload = json.loads(data)
    for field_name, enum_type in _ENUM_FIELDS[kind].items():
        payload[field_name] = enum_type(payload[field_name])
    return component_type(**payload)


class SqliteWorldStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def has_save(self) -> bool:
        row = self._connection.execute(
            "SELECT value FROM world_meta WHERE key = 'tick'"
        ).fetchone()
        return row is not None

    def save(self, world: World) -> None:
        rows = [
            (entity, component_type.__name__, _encode(component))
            for component_type in COMPONENT_TYPES
            for entity, component in world.store(component_type)
        ]
        entity_ids = [(entity,) for entity in world.query()]
        meta = {
            "seed": world.config.seed,
            "tick": world.tick,
            "next_entity": world.next_entity_id,
            "config": {
                field.name: getattr(world.config, field.name)
                for field in fields(world.config)
            },
        }

        with self._connection:
            self._connection.execute("DELETE FROM components")
            self._connection.execute("DELETE FROM entities")
            self._connection.execute("DELETE FROM world_meta")
            self._connection.executemany("INSERT INTO entities (id) VALUES (?)", entity_ids)
            self._connection.executemany(
                "INSERT INTO components (entity, kind, data) VALUES (?, ?, ?)", rows
            )
            self._connection.executemany(
                "INSERT INTO world_meta (key, value) VALUES (?, ?)",
                [(key, json.dumps(value)) for key, value in meta.items()],
            )

    def load(self) -> World:
        meta = {
            key: json.loads(value)
            for key, value in self._connection.execute(
                "SELECT key, value FROM world_meta"
            )
        }
        if "tick" not in meta:
            raise ValueError(f"no saved world in {self.path}")

        world = World(WorldConfig(**meta["config"]))
        world.tick = int(meta["tick"])

        entity_ids = [
            row[0] for row in self._connection.execute("SELECT id FROM entities ORDER BY id")
        ]
        world.restore_entities(entity_ids, int(meta["next_entity"]))

        for entity_id, kind, data in self._connection.execute(
            "SELECT entity, kind, data FROM components ORDER BY entity, kind"
        ):
            world.add(entity_id, _decode(kind, data))

        # Migration: MessageLog was added after some saves; older worlds resume without it.
        if world.first(component_module.MessageLog) is None:
            world.add(world.create_entity(), component_module.MessageLog())
        if world.first(component_module.RestQueue) is None:
            world.add(world.create_entity(), component_module.RestQueue())
        if world.first(component_module.ForceDecisionQueue) is None:
            world.add(world.create_entity(), component_module.ForceDecisionQueue())

        return world
