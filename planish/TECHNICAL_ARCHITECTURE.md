Recommended Stack

Simulation core: Python (custom ECS or Mesa) + FastAPI.
Persistence: DuckDB (great for aggregation) or SQLite + Redis for live state.
Viewer: Three.js / React Three Fiber (browser public viewer).
Optional heavy simulation: Godot 4 (MultiMesh + Jolt) with state streaming to web, or pure Three.js + ECS.

Core Patterns (from research)

Hybrid Hierarchical Cognition (mandatory for cost)
Bottom (majority): FSM + utility / Markov. Zero LLM.
Middle (clan leaders): lightweight LLM, infrequent.
Top (empire / crises): frontier LLM, rare.
Information asymmetry creates realistic politics.

Communication
Primary: Shared Blackboard (world state slices + event log).
Secondary: Structured JSON message envelopes (type, from, to, content, context).
Close-range only for social; aggregation for higher levels.

Performance
Spatial partitioning / chunks.
MultiMesh / instancing for agents and modular buildings.
Selective full 3D only when camera is near hero areas.
Deterministic seeded RNG everywhere.

