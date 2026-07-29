"""Deterministic RNG.

Randomness is derived from (world_seed, tick, stream) rather than carried in a
mutable generator. Nothing about the RNG needs to be persisted: reloading a save
at tick N and continuing produces the identical sequence an uninterrupted run
would have produced.
"""

from __future__ import annotations

from zlib import crc32

_MASK64 = 0xFFFFFFFFFFFFFFFF


def _splitmix64(state: int) -> int:
    state = (state + 0x9E3779B97F4A7C15) & _MASK64
    mixed = state
    mixed = ((mixed ^ (mixed >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    mixed = ((mixed ^ (mixed >> 27)) * 0x94D049BB133111EB) & _MASK64
    return mixed ^ (mixed >> 31)


def _stream_id(stream: str) -> int:
    return crc32(stream.encode("utf-8")) & 0xFFFFFFFF


class TickRng:
    """A random stream scoped to one (seed, tick, stream) triple."""

    __slots__ = ("_state",)

    def __init__(self, seed: int, tick: int, stream: str) -> None:
        state = _splitmix64(seed & _MASK64)
        state = _splitmix64(state ^ (tick & _MASK64))
        self._state = _splitmix64(state ^ _stream_id(stream))

    def next_u64(self) -> int:
        self._state = _splitmix64(self._state)
        return self._state

    def below(self, bound: int) -> int:
        """Uniform integer in [0, bound). Rejection-sampled to avoid modulo bias."""
        if bound <= 0:
            raise ValueError(f"bound must be positive, got {bound}")
        limit = _MASK64 - (_MASK64 % bound)
        while True:
            value = self.next_u64()
            if value < limit:
                return value % bound

    def between(self, low: int, high: int) -> int:
        """Uniform integer in [low, high]."""
        if high < low:
            raise ValueError(f"empty range [{low}, {high}]")
        return low + self.below(high - low + 1)

    def unit(self) -> float:
        """Uniform float in [0, 1)."""
        return (self.next_u64() >> 11) / float(1 << 53)

    def chance(self, probability: float) -> bool:
        return self.unit() < probability

    def pick(self, items):
        sequence = list(items)
        if not sequence:
            raise ValueError("cannot pick from an empty sequence")
        return sequence[self.below(len(sequence))]
