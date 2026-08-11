"""Errors the world raises that other layers need to catch by type.

Kept dependency-free and separate from both the simulation and the store. The store
imports the world's components, so the world cannot import the store back to catch what
it raises — this is the piece both sides are allowed to know about.
"""

from __future__ import annotations


class StaleWorldError(RuntimeError):
    """A process tried to save a world older than the one already on disk.

    Railway overlaps containers across a deploy: for a few seconds two processes have the
    same volume mounted and both are ticking the same world. A save rewrites the whole
    thing, so whichever writes last wins — and when that is the older container, the world
    rewinds. It has happened twice on the live world, and once it took a user's agent with
    it, which is the only way real data has ever been lost here.

    A lock would be worse than the disease: the outgoing container would hold it while the
    incoming one boots, so every deploy would fail to start. Refusing the *stale* write
    instead lets both run and makes the damaging case impossible, whichever order they
    happen to write in.
    """
