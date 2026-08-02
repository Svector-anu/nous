// Cinematic autopilot for the world view.
//
// The camera used to sit wherever it was left until someone dragged it. This watches the
// per-tick snapshot, works out what is worth looking at, and composes a shot around it —
// so the view moves with the world instead of waiting to be driven.
//
// It owns no simulation state and reads nothing the viewer did not already receive. Every
// judgement here is presentation: which of several true things is the most interesting one
// to point a camera at.
//
// Shot choice is driven by a seeded rng rather than Math.random, so a given sequence of
// snapshots always produces the same sequence of shots and the behaviour can be tested.

const TILE = 2;

// How long a shot holds before the director looks for something new. An event worth more
// than the current subject can cut early; see `_shouldCut`.
const SHOT_SECONDS = { establish: 8, orbit: 7, push: 6, follow: 7, survey: 9, vista: 10 };

// Weights for why a subject is worth watching. Tuned so a fight beats a building site and
// a building site beats an empty field, which is the whole editorial policy.
const SCORE = {
  raid: 100,        // a raid resolved this tick
  death: 70,        // somebody died
  fleeing: 45,      // an agent running for its life
  building: 30,     // a hut went up
  arrival: 34,      // a user-deployed agent turned up
  goalChange: 22,   // a clan changed its mind
  crowd: 3,         // per agent standing in the densest cluster
  user: 18,         // a user-deployed agent, always a bit interesting
};

// splitmix32 — small, fast, and good enough to pick shots with.
function makeRng(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x9e3779b9) >>> 0;
    let z = state;
    z = ((z ^ (z >>> 16)) * 0x21f0aaad) >>> 0;
    z = ((z ^ (z >>> 15)) * 0x735a2d97) >>> 0;
    return ((z ^ (z >>> 15)) >>> 0) / 4294967296;
  };
}

// What changed between two snapshots. This is where "interesting" comes from: a single
// snapshot cannot tell you that a raid just happened, only that raid counts are non-zero.
export function findEvents(previous, snapshot) {
  const events = [];
  if (!snapshot) return events;

  const before = new Map();
  if (previous) for (const agent of previous.agents) before.set(agent.id, agent);

  for (const agent of snapshot.agents) {
    const was = before.get(agent.id);
    if (was) {
      if (agent.raids_won > was.raids_won || agent.raids_lost > was.raids_lost) {
        events.push({ kind: "raid", score: SCORE.raid, x: agent.x, y: agent.y, agent: agent.id,
          label: `${agent.name} ${agent.raids_won > was.raids_won ? "won a raid" : "lost a raid"}` });
      }
    } else if (agent.user) {
      events.push({ kind: "arrival", score: SCORE.arrival, x: agent.x, y: agent.y, agent: agent.id,
        label: `${agent.name} arrives` });
    }
    if (agent.state === "FLEE") {
      events.push({ kind: "fleeing", score: SCORE.fleeing, x: agent.x, y: agent.y, agent: agent.id,
        label: `${agent.name} is fleeing` });
    }
    if (agent.user) {
      events.push({ kind: "user", score: SCORE.user, x: agent.x, y: agent.y, agent: agent.id,
        label: `${agent.name} (yours)` });
    }
  }

  if (previous) {
    const now = new Set(snapshot.agents.map((a) => a.id));
    for (const agent of previous.agents) {
      if (now.has(agent.id)) continue;
      events.push({ kind: "death", score: SCORE.death, x: agent.x, y: agent.y,
        label: `${agent.name} died` });
    }

    const had = new Set(previous.buildings.map((b) => b.id));
    for (const building of snapshot.buildings) {
      if (had.has(building.id)) continue;
      events.push({ kind: "building", score: SCORE.building, x: building.x, y: building.y,
        label: "a hut goes up" });
    }

    const goals = new Map(previous.clans.map((c) => [c.id, c.goal]));
    for (const clan of snapshot.clans) {
      if (!clan.centre) continue;
      if (goals.has(clan.id) && goals.get(clan.id) !== clan.goal) {
        events.push({ kind: "goalChange", score: SCORE.goalChange, x: clan.centre[0], y: clan.centre[1],
          label: `clan ${clan.id} decides to ${clan.goal}` });
      }
    }
  }

  return events;
}

// The busiest place, as a fallback when nothing in particular is happening. Scored by head
// count rather than huts: an empty village is less watchable than a crowded field.
export function busiestPlace(snapshot, radius = 6) {
  if (!snapshot || snapshot.agents.length === 0) return null;
  const buckets = new Map();
  for (const agent of snapshot.agents) {
    const key = `${Math.floor(agent.x / radius)},${Math.floor(agent.y / radius)}`;
    let bucket = buckets.get(key);
    if (!bucket) buckets.set(key, (bucket = []));
    bucket.push(agent);
  }
  let best = null;
  for (const bucket of buckets.values()) {
    if (best === null || bucket.length > best.length) best = bucket;
  }
  if (!best) return null;
  const x = best.reduce((sum, a) => sum + a.x, 0) / best.length;
  const y = best.reduce((sum, a) => sum + a.y, 0) / best.length;
  return { x, y, count: best.length };
}

const MOVING_STATES = new Set(["SEEK_NEED", "FOLLOW", "MEET", "FLEE"]);

// The agent with the longest current journey, as a way to show purposeful movement.
// A journey is only worth a shot if the target is far enough to read as travel.
export function longestJourney(snapshot) {
  if (!snapshot || snapshot.agents.length === 0) return null;
  let best = null;
  let bestDistance = 4; // minimum Chebyshev distance to bother following
  for (const agent of snapshot.agents) {
    if (!agent.target) continue;
    if (!MOVING_STATES.has(agent.state)) continue;
    const dx = Math.abs(agent.x - agent.target[0]);
    const dy = Math.abs(agent.y - agent.target[1]);
    const distance = Math.max(dx, dy);
    if (distance > bestDistance) {
      bestDistance = distance;
      best = agent;
    }
  }
  if (!best) return null;
  return {
    x: best.x,
    y: best.y,
    agent: best.id,
    distance: bestDistance,
    label: `${best.name} is ${best.state.toLowerCase().replace("_", " ")}`,
  };
}

export class Director {
  constructor(view, { seed = 1337, rng } = {}) {
    this.view = view;
    this.rng = rng ?? makeRng(seed);
    this.enabled = false;
    this.shot = null;
    this.elapsed = 0;
    this.previous = null;
    this.snapshot = null;
    this.pendingEvents = [];
  }

  enable() {
    this.enabled = true;
    // Nothing held over from last time: pick a shot on the next frame.
    this.shot = null;
    this.elapsed = 0;
  }

  disable() {
    this.enabled = false;
    this.shot = null;
  }

  // Called every tick with the fresh snapshot. Events are collected here and consumed by
  // the frame loop, because a tick is a second and shots are composed per frame.
  observe(snapshot) {
    this.pendingEvents = findEvents(this.previous, snapshot);
    this.previous = this.snapshot;
    this.snapshot = snapshot;
  }

  // Called every frame with the elapsed seconds.
  advance(dt) {
    if (!this.enabled || !this.snapshot || !this.view.active) return;
    this.elapsed += dt;

    if (this.shot === null || this.elapsed >= this.shot.duration || this._shouldCut()) {
      this._chooseShot();
    }
    this._applyShot(dt);
  }

  // Cut early only for something clearly more important than what is on screen, or the
  // camera would twitch between equally dull subjects every tick.
  _shouldCut() {
    if (this.pendingEvents.length === 0) return false;
    const best = Math.max(...this.pendingEvents.map((e) => e.score));
    // A shot needs a moment to read before it can be interrupted.
    return this.elapsed > 2.5 && best > (this.shot?.score ?? 0) * 1.4;
  }

  _chooseShot() {
    const subject = this._chooseSubject();
    this.pendingEvents = [];
    this.elapsed = 0;

    // vista is in both lists and twice in the wide one: a low shot across a settlement is
    // the most watchable thing this can do, and every other wide shot looks down from 20-30
    // units up, which reads as a map rather than a place.
    const kinds = subject.agent !== undefined
      ? ["follow", "orbit", "vista", "push"]
      : ["vista", "vista", "orbit", "push", "establish", "survey"];
    const kind = subject.forceKind ?? kinds[Math.floor(this.rng() * kinds.length)];

    this.shot = {
      kind,
      subject,
      score: subject.score,
      label: subject.label,
      duration: SHOT_SECONDS[kind] ?? 10,
      // A shot's own randomness is fixed at the cut, so it does not jitter frame to frame.
      theta0: this.rng() * Math.PI * 2,
      spin: this.rng() < 0.5 ? -1 : 1,
      span: subject.span ?? TILE * (kind === "follow" ? 4 : 8),
    };
  }

  _chooseSubject() {
    // Standing events — a user agent simply existing, an agent still fleeing — recur every
    // tick, so without this the highest-scoring one would hold the camera for the rest of
    // the session. Penalising whatever was just on screen keeps the film moving without
    // hiding a genuinely bigger event, which will still outscore the penalty.
    const previous = this.shot?.subject;
    const penalty = (event) => {
      if (!previous) return event.score;
      const sameAgent = event.agent !== undefined && event.agent === previous.agent;
      const samePlace = event.agent === undefined && previous.agent === undefined &&
        Math.abs(event.x - previous.x) < 2 && Math.abs(event.y - previous.y) < 2;
      return sameAgent || samePlace ? event.score * 0.35 : event.score;
    };

    // The crowd and the world are candidates like any other, not a fallback for when the
    // event list is empty. Treating them as a fallback meant one standing event — a single
    // user-deployed agent — held the camera for the entire session, because the list was
    // never empty and so the alternatives were never considered.
    const candidates = [...this.pendingEvents];

    const busy = busiestPlace(this.snapshot);
    if (busy) {
      candidates.push({
        kind: "crowd", score: busy.count * SCORE.crowd, x: busy.x, y: busy.y,
        label: `${busy.count} gathered`,
      });
    }

    const journey = longestJourney(this.snapshot);
    if (journey) {
      candidates.push({
        kind: "traveller",
        score: Math.min(18, 8 + journey.distance * 1.2),
        x: journey.x,
        y: journey.y,
        agent: journey.agent,
        label: journey.label,
      });
    }

    candidates.push({
      kind: "world", score: 8, x: this.snapshot.grid.width / 2, y: this.snapshot.grid.height / 2,
      label: "the world", forceKind: "establish", span: (this.snapshot.grid.width / 2) * TILE,
    });

    let best = null;
    let bestScore = -Infinity;
    for (const candidate of candidates) {
      // A little jitter, so two candidates of equal standing do not lock into one order.
      const score = penalty(candidate) * (0.9 + this.rng() * 0.2);
      if (score > bestScore) {
        bestScore = score;
        best = candidate;
      }
    }
    return best;
  }

  // Where the subject is *now* — a followed agent keeps moving after the cut.
  _subjectPosition() {
    const subject = this.shot.subject;
    if (subject.agent !== undefined && this.snapshot) {
      const agent = this.snapshot.agents.find((a) => a.id === subject.agent);
      if (agent) return { x: agent.x, y: agent.y };
      // The subject died mid-shot, which is itself worth staying on for a beat.
      this.shot.duration = Math.min(this.shot.duration, this.elapsed + 2);
    }
    return { x: subject.x, y: subject.y };
  }

  _applyShot(dt) {
    const shot = this.shot;
    const at = this._subjectPosition();
    const t = this.elapsed;

    switch (shot.kind) {
      case "orbit": {
        // A slow circle. The camera drifts round the subject at a fixed rate rather than
        // easing to a fixed angle, which is what makes it read as a moving shot.
        this.view.flyTo(at.x, at.y, shot.span, {
          theta: shot.theta0 + shot.spin * t * 0.09,
          phi: Math.PI * 0.34,
          ease: 2.2,
        });
        break;
      }
      case "push": {
        // Dolly in: hold the angle, close the distance over the shot.
        const progress = Math.min(1, t / shot.duration);
        const span = shot.span * (1 - 0.45 * progress);
        this.view.flyTo(at.x, at.y, span, {
          theta: shot.theta0,
          phi: Math.PI * 0.30 + progress * 0.12,
          ease: 1.1,
        });
        break;
      }
      case "follow": {
        // Track the subject from a low angle, letting the eased camera lag behind the
        // movement — the lag is the point, it looks like a camera operator keeping up.
        this.view.flyTo(at.x, at.y, shot.span, {
          theta: shot.theta0 + shot.spin * t * 0.03,
          phi: Math.PI * 0.42,
          ease: 1.5,
        });
        break;
      }
      case "vista": {
        // Standing in the settlement looking across it, drifting slowly sideways. Height is
        // specified directly rather than via a span, because fitting a span at this angle
        // pulls the camera back up into an aerial.
        this.view.flyTo(at.x, at.y, undefined, {
          theta: shot.theta0 + shot.spin * t * 0.02,
          phi: Math.PI * 0.455,
          // Low, because at this angle distance is height/cos(phi): a taller camera is also
          // a further one, and standing tall put the lens outside the village.
          height: TILE * (1.6 + Math.sin(t * 0.12) * 0.35),
          ease: 1.0,
        });
        break;
      }
      case "survey": {
        // A long slow crane across the area, rising as it goes.
        const progress = Math.min(1, t / shot.duration);
        this.view.flyTo(at.x, at.y, shot.span * (1 + progress * 0.8), {
          theta: shot.theta0 + shot.spin * t * 0.05,
          phi: Math.PI * 0.30 - progress * 0.1,
          ease: 0.9,
        });
        break;
      }
      case "establish":
      default: {
        this.view.flyTo(at.x, at.y, shot.span, {
          theta: shot.theta0 + shot.spin * t * 0.035,
          phi: Math.PI * 0.26,
          ease: 0.8,
        });
        break;
      }
    }
    return dt;
  }

  describe() {
    if (!this.enabled) return null;
    if (!this.shot) return "finding a shot";
    return `${this.shot.kind} · ${this.shot.label}`;
  }
}
