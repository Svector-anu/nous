"""Loads `.env` from the repo root, once, before anything reads an api key.

The world is meant to run unattended for days. Retyping a credential on every start is
not a workflow, and putting it in a shell profile spreads it across machines — a file the
repo already refuses to track is the smaller thing to get wrong.

Nothing here reads the values. Advisors resolve their own key by env var name, so this
only has to make sure the environment is populated before that happens.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("neociv")

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

_loaded = False


def load_env(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Populate os.environ from a .env file. Returns the names that were applied.

    A variable already present in the environment wins by default, so an inline
    `DGRID_API_KEY=... python -m src.main` still beats whatever is in the file — otherwise
    a stale file would silently override the key you just passed on purpose.
    """
    global _loaded
    target = path if path is not None else ENV_PATH
    if not target.exists():
        return {}

    applied: dict[str, str] = {}
    for raw in target.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        # Tolerate quoted values; a quoted key that keeps its quotes fails auth in a way
        # that looks exactly like a wrong key.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not name:
            continue
        if not override and name in os.environ:
            continue
        os.environ[name] = value
        applied[name] = value

    if applied and not _loaded:
        # Names only. Never the values.
        logger.info("loaded %s from %s", ", ".join(sorted(applied)), target.name)
    _loaded = True
    return applied
