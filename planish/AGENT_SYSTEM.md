# agent system

what an agent actually is, as built. this describes the code in `src/world/`, not an
aspiration — anything not yet implemented is marked.

## components

an agent is an entity carrying:

| component | fields |
|:--|:--|
| `Position` | `x`, `y` |
| `Needs` | `energy`, `hunger`, `social`, `safety` — only energy and hunger are driven |
| `Inventory` | `food`, `wood`, each capped at `carry_capacity` |
| `Agent` | `name`, `state`, target fields, `wants`, `huts_owned`, `last_request_tick`, `received`, `personality`, `user_deployed` |
| `ClanRef` | `clan_id` or none |
| `Inbox` / `Outbox` | delivered and queued messages |

`social` and `safety` are stored but unused — seams for phase 1 rivalry and phase 2
danger, kept so adding them needs no schema change.

## the state machine

pure fsm. **no llm calls anywhere.** cheap enough that 500 agents cost ~7 ms/tick.

```
IDLE ──┬─ energy low & not starving ──────────→ REST
       ├─ hunger low & room to carry ─────────→ SEEK_NEED (food)
       ├─ wood ≥ hut cost & may build ────────→ BUILD
       ├─ clan goal bias (rng-gated) ─────────→ SEEK_NEED (goal resource)
       ├─ room to carry ──────────────────────→ SEEK_NEED
       ├─ clan rally & rested ────────────────→ FOLLOW
       └─ nothing to do ──────────────────────→ REST

SEEK_NEED ─ arrived at node ─→ GATHER ─ full / node dormant ─→ IDLE or BUILD
FOLLOW    ─ arrived at rally ─────────────────────────────────→ IDLE
MEET      ─ arrived at donor ─────────────────────────────────→ IDLE
FLEE      ─ no trigger yet (combat seam) ─────────────────────→ IDLE
```

**decision order is the design.** everything above the clan-goal branch is survival or a
standing commitment and is never overridden. the goal only leans on the discretionary
part, and only on a `goal_bias_chance` roll. a starving member of a `gather_wood` clan
still goes for food — there is a test for exactly that.

## needs and death

- hunger and energy each decay 1/tick
- eating costs 1 food, restores 45 hunger, resolved *before* energy each tick
- resting restores 6 energy, **unavailable while `hunger == 0`**
- `energy == 0` is the only death rule; starvation kills by denying recovery

a need crossing its threshold preempts whatever the agent was doing, with two exemptions
in `needs._already_addressing`: a rest is never cut short, and an agent already fetching
what it needs is left alone. **both exemptions exist because their absence caused
livelocks** where an agent was reset to `IDLE` every tick and never ate.

## memory and personality

**memory is not built.** the prd calls for episodic memory plus llm-compressed summaries.
neither exists, and neither should until the hierarchical llm tier arrives — bottom-tier
agents are meant to stay free.

**personality exists, but only as a note.** user-deployed agents carry a `personality`
string on `Agent`, stored, persisted and shown in the viewer and on `GET /agents`. nothing
reads it: it does not steer the fsm and there is no llm to feed it to. it is deliberately
the seam that middle-tier cognition plugs into later. genesis agents leave it empty.

## user-deployed agents

`POST /agents` appends to a `SpawnQueue` held by a world entity; the `spawning` system,
first in the tick, is the only place an agent is created. that ordering is what keeps a
deployment a reproducible *input* rather than a wall-clock event — position comes from the
tick's rng stream, and a save carrying a pending request replays it identically.

deployed agents are ordinary in every other respect: same needs, same fsm, they join
clans, trade, starve and die. they are drawn larger with a white ring.

## roles

**not hard-coded, by design.** there is no builder/fighter/trader class. what an agent
does falls out of its needs, its inventory and its clan's goal. a fed agent in an
`expand` clan builds; a hungry one in the same clan forages. this is deliberate and
should survive contact with combat.

## clans

- form between two idle unaffiliated agents within `clan_form_radius`, at
  `clan_form_chance` per tick, capped at `max_clan_size`
- a clan is its own entity; membership is mirrored on `ClanRef`
- founder leads; on death the lowest surviving id inherits; empty clans are pruned
- **centre** = mean position of living members, recomputed per tick with no smoothing
  buffer, so save/load stays exact
- **influence** = soft radius growing with membership, published to the blackboard.
  readable, never enforceable — no ownership, no exclusion

### goals

one at a time, reviewed every `goal_review_ticks`, chosen by conditions that were
**measured rather than guessed**:

| goal | when |
|:--|:--|
| `gather_food` | clan food ratio below threshold, or mean hunger below threshold |
| `gather_wood` | short of huts and short of timber |
| `expand` | short of huts but holding wood — builds inside the clan's own influence |
| `rally` | fed and built out |

`expand` and `gather_wood` retire on their own: once every member holds
`max_huts_per_agent` huts the signal can never fire again, so they are early-life goals
and mature clans alternate between feeding themselves and staying together.

## communication

two channels, both with a **one-tick propagation lag** — nothing is readable in the tick
it was written.

- **blackboard** — `food_locations`, `wood_locations`, `clan:{id}:goal`,
  `clan:{id}:centre`, `clan:{id}:rally`. entries expire past `blackboard_ttl_ticks`.
- **messages** — `{from, to, type, content}`, delivered next tick, **persisting until
  consumed**. `info` is always consumed so rally chatter cannot evict a real request.

agents read the blackboard only when nothing is in vision, so phase 0 foraging is
untouched whenever food is nearby. hints are validated against live nodes at read time.

## trade

`request` → a clanmate in range hands over surplus above `surplus_reserve`, one out of
range replies with an `offer`. `alert` → donors dip into that reserve. `offer` → the asker
walks over in `MEET`. a donor with nothing to give **keeps** the request rather than
discarding it.

sharing measurably improves survival: 407 food units changed hands over 40000 ticks and
carrying capacity rose from 98 to 103, then to 112 once clan goals coordinated foraging.

## disasters — not built yet

the prd calls for map-reshaping disasters writing to the blackboard and triggering local
fsm reactions. the `alert` message type and the `FLEE` state are the seams. neither has a
trigger.
