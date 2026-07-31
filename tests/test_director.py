"""The cinematic director's editorial logic, exercised under node.

The director decides what the camera looks at. Its judgements are pure functions of two
snapshots, so they can be tested without a gpu — and they need to be, because a regression
here is invisible: the camera still moves, it just stops showing you the interesting thing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

VIEWER_DIR = Path(__file__).resolve().parents[1] / "src" / "viewer"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(body: str, tmp_path: Path) -> dict:
    """director.js imports nothing, so it runs as-is."""
    shutil.copy(VIEWER_DIR / "director.js", tmp_path / "director.mjs")
    (tmp_path / "main.mjs").write_text(
        textwrap.dedent(
            """
            import { Director, findEvents, busiestPlace } from "./director.mjs";
            """
        )
        + body
    )
    result = subprocess.run(
        ["node", "main.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _agent(entity_id: int, **overrides) -> dict:
    agent = {
        "id": entity_id, "name": f"A{entity_id}", "x": 10, "y": 10, "state": "IDLE",
        "energy": 80, "hunger": 80, "food": 1, "wood": 1, "clan": 1, "user": False,
        "personality": "", "wants": "FOOD", "huts": 0, "raids_won": 0, "raids_lost": 0,
        "received": 0, "target": None,
    }
    agent.update(overrides)
    return agent


def _snapshot(agents=None, buildings=None, clans=None) -> dict:
    return {
        "tick": 1, "day": 0, "grid": {"width": 64, "height": 64},
        "agents": agents if agents is not None else [],
        "buildings": buildings if buildings is not None else [],
        "resources": [],
        "clans": clans if clans is not None else [],
        "stats": {},
    }


def test_a_first_snapshot_invents_no_history(tmp_path):
    """With nothing to compare against, the only events are things visible in one frame.
    Reporting a death or a new building here would mean the opening shot is a lie."""
    snapshot = _snapshot(agents=[_agent(1), _agent(2)], buildings=[{"id": 9, "x": 3, "y": 3}])
    out = _run(
        f"console.log(JSON.stringify({{ kinds: findEvents(null, {json.dumps(snapshot)}).map(e => e.kind) }}));",
        tmp_path,
    )
    assert "death" not in out["kinds"]
    assert "building" not in out["kinds"]
    assert "raid" not in out["kinds"]


def test_it_notices_a_raid(tmp_path):
    before = _snapshot(agents=[_agent(1, raids_won=0)])
    after = _snapshot(agents=[_agent(1, raids_won=1)])
    out = _run(
        f"""
        const events = findEvents({json.dumps(before)}, {json.dumps(after)});
        const raid = events.find(e => e.kind === "raid");
        console.log(JSON.stringify({{ found: !!raid, score: raid?.score, agent: raid?.agent }}));
        """,
        tmp_path,
    )
    assert out["found"]
    assert out["agent"] == 1


def test_it_notices_a_death_and_a_new_building(tmp_path):
    before = _snapshot(agents=[_agent(1), _agent(2)], buildings=[])
    after = _snapshot(agents=[_agent(1)], buildings=[{"id": 5, "x": 7, "y": 8}])
    out = _run(
        f"""
        const events = findEvents({json.dumps(before)}, {json.dumps(after)});
        console.log(JSON.stringify({{
          kinds: events.map(e => e.kind),
          building: events.find(e => e.kind === "building"),
        }}));
        """,
        tmp_path,
    )
    assert "death" in out["kinds"]
    assert "building" in out["kinds"]
    # the shot has to be aimed at the hut, not at the origin
    assert (out["building"]["x"], out["building"]["y"]) == (7, 8)


def test_a_raid_outranks_a_building_site(tmp_path):
    """The scores are the editorial policy: violence beats construction beats an empty
    field. If this inverts, the camera dutifully films nothing while a war happens."""
    before = _snapshot(agents=[_agent(1, raids_won=0)], buildings=[])
    after = _snapshot(agents=[_agent(1, raids_won=1)], buildings=[{"id": 5, "x": 7, "y": 8}])
    out = _run(
        f"""
        const events = findEvents({json.dumps(before)}, {json.dumps(after)});
        const score = (kind) => events.find(e => e.kind === kind)?.score ?? 0;
        console.log(JSON.stringify({{
          raid: score("raid"), building: score("building"), death: score("death"),
        }}));
        """,
        tmp_path,
    )
    assert out["raid"] > out["building"] > 0


def test_it_finds_the_crowd_when_nothing_happens(tmp_path):
    """A quiet world still needs a subject."""
    agents = [_agent(i, x=40, y=40) for i in range(1, 9)] + [_agent(99, x=2, y=60)]
    out = _run(
        f"console.log(JSON.stringify({{ busy: busiestPlace({json.dumps(_snapshot(agents=agents))}) }}));",
        tmp_path,
    )
    assert out["busy"]["count"] == 8
    assert abs(out["busy"]["x"] - 40) < 2 and abs(out["busy"]["y"] - 40) < 2


def test_an_empty_world_does_not_crash_the_director(tmp_path):
    out = _run(
        f"""
        const empty = {json.dumps(_snapshot())};
        const view = {{ active: true, flyTo() {{}} }};
        const director = new Director(view);
        director.enable();
        director.observe(empty);
        for (let i = 0; i < 30; i++) director.advance(1 / 60);
        console.log(JSON.stringify({{ busy: busiestPlace(empty), shot: director.shot?.kind ?? null }}));
        """,
        tmp_path,
    )
    assert out["busy"] is None
    # it must still choose something rather than throwing or sitting on null
    assert out["shot"] is not None


def test_shot_choice_is_reproducible(tmp_path):
    """Seeded rng, not Math.random, so the same world produces the same film and a
    behavioural change shows up as a diff rather than as folklore."""
    snapshot = _snapshot(agents=[_agent(i, x=20 + i, y=20) for i in range(1, 6)])
    out = _run(
        f"""
        const snapshot = {json.dumps(snapshot)};
        const runOnce = () => {{
          const kinds = [];
          const view = {{ active: true, flyTo() {{}} }};
          const director = new Director(view, {{ seed: 4242 }});
          director.enable();
          for (let tick = 0; tick < 8; tick++) {{
            director.observe(snapshot);
            // a full shot's worth of frames, so each pass cuts
            for (let f = 0; f < 60 * 14; f++) director.advance(1 / 60);
            kinds.push(director.shot.kind);
          }}
          return kinds;
        }};
        console.log(JSON.stringify({{ first: runOnce(), second: runOnce() }}));
        """,
        tmp_path,
    )
    assert out["first"] == out["second"]
    assert len(set(out["first"])) > 1, "every shot was the same kind; the director is stuck"


def test_the_camera_is_actually_driven(tmp_path):
    """The director's whole job is calling flyTo. A shot that never moves the camera is
    indistinguishable from a broken one."""
    snapshot = _snapshot(agents=[_agent(i, x=20 + i, y=20) for i in range(1, 6)])
    out = _run(
        f"""
        const calls = [];
        const view = {{ active: true, flyTo(x, y, span, options) {{ calls.push({{ x, y, span, options }}); }} }};
        const director = new Director(view, {{ seed: 7 }});
        director.enable();
        director.observe({json.dumps(snapshot)});
        for (let f = 0; f < 240; f++) director.advance(1 / 60);
        const angles = new Set(calls.map(c => c.options.theta.toFixed(4)));
        console.log(JSON.stringify({{
          calls: calls.length,
          movingAngles: angles.size,
          finite: calls.every(c => Number.isFinite(c.x) && Number.isFinite(c.y) && Number.isFinite(c.span)),
        }}));
        """,
        tmp_path,
    )
    assert out["calls"] >= 200, "the director stopped driving the camera"
    assert out["finite"], "a shot aimed the camera at a non-finite position"
    assert out["movingAngles"] > 1, "the shot is static; nothing would appear to move"


def test_a_disabled_director_never_touches_the_camera(tmp_path):
    out = _run(
        f"""
        let calls = 0;
        const view = {{ active: true, flyTo() {{ calls++; }} }};
        const director = new Director(view);
        director.observe({json.dumps(_snapshot(agents=[_agent(1)]))});
        for (let f = 0; f < 600; f++) director.advance(1 / 60);
        console.log(JSON.stringify({{ calls }}));
        """,
        tmp_path,
    )
    assert out["calls"] == 0


def test_it_stays_on_a_shot_long_enough_to_read(tmp_path):
    """Cutting every tick would be unwatchable. A new event may interrupt, but not
    instantly, and not for something no more interesting than the current subject."""
    quiet = _snapshot(agents=[_agent(1, x=30, y=30)])
    out = _run(
        f"""
        const view = {{ active: true, flyTo() {{}} }};
        const director = new Director(view, {{ seed: 11 }});
        director.enable();
        director.observe({json.dumps(quiet)});
        director.advance(1 / 60);
        const first = director.shot;
        let cutsWhileFedSameEvents = 0;
        for (let tick = 0; tick < 5; tick++) {{
          director.observe({json.dumps(quiet)});
          for (let f = 0; f < 60; f++) director.advance(1 / 60);
          if (director.shot !== first) cutsWhileFedSameEvents++;
        }}
        console.log(JSON.stringify({{ cutsWhileFedSameEvents, duration: first.duration }}));
        """,
        tmp_path,
    )
    # five seconds of identical snapshots must not trigger a cut
    assert out["cutsWhileFedSameEvents"] == 0
    assert out["duration"] >= 5


def test_vista_is_in_the_shot_rotation(tmp_path):
    """The low wide shot is the one that reads as being *in* the settlement. Every other
    wide shot looks down from 20-30 units up, which reads as a map rather than a place."""
    snapshot = _snapshot(agents=[_agent(i, x=20 + i, y=20) for i in range(1, 6)])
    out = _run(
        f"""
        const kinds = new Set();
        const view = {{ active: true, flyTo() {{}} }};
        const director = new Director(view, {{ seed: 3 }});
        director.enable();
        for (let tick = 0; tick < 40; tick++) {{
          director.observe({json.dumps(snapshot)});
          for (let f = 0; f < 60 * 15; f++) director.advance(1 / 60);
          kinds.add(director.shot.kind);
        }}
        console.log(JSON.stringify({{ kinds: [...kinds] }}));
        """,
        tmp_path,
    )
    assert "vista" in out["kinds"], f"vista never chosen; got {out['kinds']}"


def test_a_vista_shot_asks_for_a_height_not_a_span(tmp_path):
    """At this angle distance is height/cos(phi), so a taller camera is also a further one.
    Deriving the distance from a span pulls the lens back out into an aerial — which is
    exactly what put it 48 units away, outside the village looking in."""
    snapshot = _snapshot(agents=[_agent(i, x=20 + i, y=20) for i in range(1, 6)])
    out = _run(
        f"""
        const calls = [];
        const view = {{ active: true, flyTo(x, y, span, options) {{ calls.push({{ span, options }}); }} }};
        const director = new Director(view, {{ seed: 3 }});
        director.enable();
        director.observe({json.dumps(snapshot)});
        // drive shots until a vista turns up
        let seen = null;
        for (let tick = 0; tick < 40 && !seen; tick++) {{
          director.observe({json.dumps(snapshot)});
          for (let f = 0; f < 60 * 15; f++) director.advance(1 / 60);
          if (director.shot.kind === "vista") {{
            calls.length = 0;
            for (let f = 0; f < 60; f++) director.advance(1 / 60);
            seen = calls[calls.length - 1];
          }}
        }}
        console.log(JSON.stringify({{ seen }}));
        """,
        tmp_path,
    )
    shot = out["seen"]
    assert shot is not None, "no vista shot was produced"
    # JSON.stringify drops undefined keys, so an absent span is the passing shape.
    assert shot.get("span") is None, "vista must not drive distance from a span"
    assert shot["options"].get("height") is not None, "vista is specified by camera height"
    assert shot["options"]["height"] < 6, "a tall camera is also a distant one at this angle"
    assert shot["options"]["phi"] > 1.3, "a vista is a low angle, near the horizon"
