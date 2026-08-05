"""Chain integration: identity, x402 payment verification, and settlement intent.

Everything here runs at the *api boundary*, never inside a tick. The simulation depends
only on what this package records into a queue — same contract the llm advisor follows,
and for the same reason: a world whose history depends on a network call cannot be
replayed from a seed.
"""
