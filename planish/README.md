# neo-civilization — design docs

hybrid ai civilization simulation, agentcraft-inspired.

**goal**: persistent 24/7 autonomous ai agent civilization with hybrid selective
high-quality 3d visuals.

**inspiration**: agentcraft core loop + claude-of-duty procedural materials/modular
buildings + hierarchical multi-agent patterns from project sid / emergenta / maxim /
emergence world.

**date**: 2026-07-28

## docs

| doc | what's in it |
|:--|:--|
| [PRD.md](PRD.md) | mvp must-haves, v1, explicit non-goals for the first 3 months |
| [TECHNICAL_ARCHITECTURE.md](TECHNICAL_ARCHITECTURE.md) | stack, three-tier cognition, blackboard, perf rules |
| [VISUALS_AND_PROCEDURAL.md](VISUALS_AND_PROCEDURAL.md) | claude-of-duty technique to port, selective-3d policy, camera |
| [AGENT_SYSTEM.md](AGENT_SYSTEM.md) | personality, memory, needs, emergent roles, clans |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | phases 0–4 |
| [RESEARCH_SOURCES.md](RESEARCH_SOURCES.md) | the repos and papers this is derived from |

## core vision

a live, never-pausing world where hundreds of ai agents with personalities freely build
settlements, form clans, wage wars, and create emergent history. most of the world stays
lightweight. key places (cities, fortresses, battle zones, landmarks) use high-fidelity
pure-procedural 3d — realistic walls and houses, zero art assets. users deploy custom
agents. public live viewer. then when everything works we wire prediction markets and
token chaos. start near $0 cost with free tools.

## status

phase 0 is built and lives in [`../src`](../src) — see the [root readme](../README.md)
for what shipped and what it actually does at 60k ticks. everything from phase 1 onward
(blackboard, messaging, clans, combat, llm tiers, procedural 3d) is still just these docs.
