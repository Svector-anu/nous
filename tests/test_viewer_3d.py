"""The focus view's pure logic, exercised under node.

Anything touching webgl needs a real gpu and lives in scripts/verify_world3d.mjs. What is
left is still worth guarding here: the cluster search picks where the camera goes, and the
terrain function decides where every hut is planted, so a silent change to either moves
the whole scene.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

VIEWER_DIR = Path(__file__).resolve().parents[1] / "src" / "viewer"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _run(body: str, tmp_path: Path) -> dict:
    """Run a snippet against world3d.js. The module imports three.js by absolute url, which
    only resolves when the server is serving it, so rewrite that to a relative path."""
    shutil.copytree(VIEWER_DIR / "vendor", tmp_path / "vendor")
    source = (VIEWER_DIR / "world3d.js").read_text()
    source = source.replace('"/static/vendor/three.module.min.js"', '"./vendor/three.module.min.js"')
    (tmp_path / "world3d.mjs").write_text(source)
    (tmp_path / "main.mjs").write_text(
        textwrap.dedent(
            """
            import { densestCluster, terrainHeight, clanHue, buildHumanoid } from "./world3d.mjs";
            """
        )
        + body
    )
    result = subprocess.run(
        ["node", "main.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _grid(count: int) -> list[dict]:
    return [{"id": i, "x": (i * 7) % 60, "y": (i * 13) % 60} for i in range(count)]


def test_the_module_loads_and_exports_its_surface(tmp_path):
    out = _run(
        'console.log(JSON.stringify({ ok: typeof densestCluster === "function" '
        '&& typeof terrainHeight === "function" && typeof clanHue === "function" }));',
        tmp_path,
    )
    assert out["ok"]


def test_densest_cluster_matches_the_exhaustive_search(tmp_path):
    """The bucketed search replaced an O(n^2) scan. It has to agree with it exactly, at
    every radius — it decides which settlement the camera opens on."""
    buildings = json.dumps(_grid(300))
    out = _run(
        f"""
        const buildings = {buildings};
        const snapshot = {{ buildings }};
        const naive = (radius) => {{
          let best = null, bestCount = -1;
          for (const a of buildings) {{
            let count = 0;
            for (const o of buildings) {{
              if (Math.abs(o.x - a.x) <= radius && Math.abs(o.y - a.y) <= radius) count++;
            }}
            if (count > bestCount) {{ bestCount = count; best = a; }}
          }}
          return best ? {{ centre: [best.x, best.y], count: bestCount }} : null;
        }};
        const rows = [1, 2, 3, 5, 8, 12, 20, 40].map((radius) => ({{
          radius,
          naive: naive(radius),
          bucketed: densestCluster(snapshot, radius),
        }}));
        console.log(JSON.stringify({{ rows }}));
        """,
        tmp_path,
    )
    for row in out["rows"]:
        assert row["naive"] == row["bucketed"], f"radius {row['radius']}"


def test_densest_cluster_handles_a_world_with_no_buildings(tmp_path):
    out = _run(
        'console.log(JSON.stringify({ empty: densestCluster({ buildings: [] }, 12) }));',
        tmp_path,
    )
    assert out["empty"] is None


def test_terrain_is_deterministic_and_bounded(tmp_path):
    """Huts, trees and agents are all planted at terrainHeight, so it must return the same
    value for the same point every call, and must stay shallow enough that a hut's square
    footprint does not hang off a slope."""
    out = _run(
        """
        const points = [];
        for (let x = -60; x <= 60; x += 3) for (let z = -60; z <= 60; z += 3) points.push([x, z]);
        const first = points.map(([x, z]) => terrainHeight(x, z));
        const second = points.map(([x, z]) => terrainHeight(x, z));
        const stable = first.every((v, i) => v === second[i]);
        const finite = first.every((v) => Number.isFinite(v));
        // steepest change across one world tile (TILE = 2)
        let steepest = 0;
        for (const [x, z] of points) {
          steepest = Math.max(steepest, Math.abs(terrainHeight(x + 2, z) - terrainHeight(x, z)));
        }
        console.log(JSON.stringify({
          stable, finite, steepest,
          min: Math.min(...first), max: Math.max(...first),
        }));
        """,
        tmp_path,
    )
    assert out["stable"]
    assert out["finite"]
    assert abs(out["min"]) < 6 and abs(out["max"]) < 6
    assert out["steepest"] < 1.0


def test_every_surface_tiles_without_a_seam(tmp_path):
    """The claude-of-duty reference calls for "periodic noise so everything tiles
    seamlessly", and it is load-bearing: the grass texture repeats 60x60 across the
    ground, so a discontinuity at the tile boundary draws a grid over the whole scene.

    The measure is the wrap edge against the *strongest* edge the surface already has
    inside itself. Comparing against the texture's midpoint instead is misleading — the
    stone wrap lands on a mortar joint, which is legitimately a hard edge, and mid-texture
    happens to be flat mid-course.
    """
    out = _run(
        """
        import { buildSurface } from "./world3d.mjs";
        const size = 128;
        const report = {};
        for (const name of ["grass", "plaster", "wood", "roof", "stone"]) {
          const material = buildSurface(name);
          const maps = { albedo: material.map, normal: material.normalMap, rough: material.roughnessMap };
          for (const [label, map] of Object.entries(maps)) {
            const d = map.image.data;
            const at = (x, y, c) => d[((y * size) + x) * 4 + c];
            const col = (x0) => {
              let s = 0;
              for (let y = 0; y < size; y++) for (let c = 0; c < 3; c++) s += Math.abs(at((x0 + 1) % size, y, c) - at(x0, y, c));
              return s;
            };
            const row = (y0) => {
              let s = 0;
              for (let x = 0; x < size; x++) for (let c = 0; c < 3; c++) s += Math.abs(at(x, (y0 + 1) % size, c) - at(x, y0, c));
              return s;
            };
            const cols = [], rows = [];
            for (let i = 0; i < size; i++) { cols.push(col(i)); rows.push(row(i)); }
            report[`${name}/${label}`] = [
              cols[size - 1] / Math.max(0.001, Math.max(...cols.slice(0, size - 1))),
              rows[size - 1] / Math.max(0.001, Math.max(...rows.slice(0, size - 1))),
            ];
          }
        }
        console.log(JSON.stringify(report));
        """,
        tmp_path,
    )
    for surface, (ratio_x, ratio_y) in out.items():
        assert ratio_x < 1.15, f"{surface} seam across u: {ratio_x:.2f}x"
        assert ratio_y < 1.15, f"{surface} seam across v: {ratio_y:.2f}x"


def test_terrain_noise_does_not_visibly_repeat(tmp_path):
    """Terrain wants the opposite property from the textures: it is sampled by world
    position over one plane and never tiled, so a period would show as a repeating
    landscape. Guards against someone routing it through the periodic noise."""
    out = _run(
        """
        // if terrain were periodic with any period up to 400 units, these would match
        const probes = [];
        for (const period of [64, 128, 200, 256, 400]) {
          let same = 0, total = 0;
          for (let x = -40; x <= 40; x += 7) for (let z = -40; z <= 40; z += 7) {
            total++;
            if (Math.abs(terrainHeight(x, z) - terrainHeight(x + period, z)) < 1e-6) same++;
          }
          probes.push({ period, fraction: same / total });
        }
        console.log(JSON.stringify({ probes }));
        """,
        tmp_path,
    )
    for probe in out["probes"]:
        assert probe["fraction"] < 0.5, f"terrain repeats at period {probe['period']}"


def test_clan_colours_are_stable_and_distinct(tmp_path):
    """A clan keeps its colour between the 2d map and the 3d view, so this must not drift."""
    out = _run(
        """
        const hues = {};
        for (let id = 0; id < 24; id++) hues[id] = clanHue(id);
        const repeat = clanHue(7) === hues[7];
        console.log(JSON.stringify({ hues, repeat, distinct: new Set(Object.values(hues)).size }));
        """,
        tmp_path,
    )
    assert out["repeat"]
    assert out["distinct"] >= 20


def test_the_humanoid_is_two_merged_geometries_not_six(tmp_path):
    """Six parts as six InstancedMeshes would be ~90 draw calls for the agents alone, more
    than the entire world costs. Merging into a clan-coloured batch and a skin batch keeps
    the exact two batches per clan the capsule-and-sphere placeholder used."""
    out = _run(
        """
        const h = buildHumanoid();
        const shape = (g) => ({
          verts: g.attributes.position.count,
          hasNormal: !!g.attributes.normal,
          hasUv: !!g.attributes.uv,
          indexed: !!g.index,
        });
        console.log(JSON.stringify({ body: shape(h.body), skin: shape(h.skin) }));
        """,
        tmp_path,
    )
    for part in ("body", "skin"):
        assert out[part]["verts"] > 0
        assert out[part]["hasNormal"], f"{part} lost its normals in the merge"
        assert out[part]["hasUv"], f"{part} lost its uvs in the merge"
        assert not out[part]["indexed"], "merged parts are non-indexed by construction"


def test_the_humanoid_stands_on_its_feet(tmp_path):
    """Both merged geometries are modelled with the feet at local y=0, so body and skin
    share one instance matrix. If either drifts the figure floats or sinks."""
    out = _run(
        """
        const h = buildHumanoid();
        const box = (g) => { g.computeBoundingBox(); const b = g.boundingBox;
          return { minY: b.min.y, maxY: b.max.y, halfWidth: Math.max(Math.abs(b.min.x), b.max.x) }; };
        console.log(JSON.stringify({ body: box(h.body), skin: box(h.skin) }));
        """,
        tmp_path,
    )
    assert abs(out["body"]["minY"]) < 0.05, "feet are not on the ground"
    total_height = out["skin"]["maxY"]
    assert 1.2 < total_height < 1.9, f"a person should be about 1.5 units tall, got {total_height}"


def test_the_head_clears_the_torso(tmp_path):
    """A head sunk into the shoulders reads as a bollard, not a person. This was the first
    pass, and it only showed at magnification."""
    out = _run(
        """
        const h = buildHumanoid();
        h.body.computeBoundingBox(); h.skin.computeBoundingBox();
        console.log(JSON.stringify({ torsoTop: h.body.boundingBox.max.y, skinTop: h.skin.boundingBox.max.y }));
        """,
        tmp_path,
    )
    # the head must sit above the shoulders, not inside them
    assert out["skinTop"] > out["torsoTop"] + 0.1


def test_a_person_stays_cheap(tmp_path):
    """The whole point of the blocky silhouette. If this grows, LOD stops being optional."""
    out = _run(
        """
        const h = buildHumanoid();
        const tris = (g) => g.attributes.position.count / 3;
        console.log(JSON.stringify({ tris: tris(h.body) + tris(h.skin) }));
        """,
        tmp_path,
    )
    assert out["tris"] < 900, f"a humanoid costs {out['tris']} triangles; budget is 900"


def test_every_limb_is_tagged_for_the_walk_shader(tmp_path):
    """Merging the parts costs independent limb transforms on the cpu; tagging each vertex
    with its limb hands them back in the vertex shader. Untagged vertices never swing, so a
    missing tag is a limb that stops moving with no other symptom."""
    out = _run(
        """
        import { LIMB } from "./world3d.mjs";
        const h = buildHumanoid();
        const tally = (g) => {
          const a = g.getAttribute("limb");
          const counts = {};
          for (let i = 0; i < a.count; i++) counts[a.array[i]] = (counts[a.array[i]] || 0) + 1;
          return counts;
        };
        console.log(JSON.stringify({ LIMB, body: tally(h.body), skin: tally(h.skin) }));
        """,
        tmp_path,
    )
    limb = out["LIMB"]
    body = out["body"]
    # torso static, both legs and both arms tagged and non-empty
    for name in ("LEG_L", "LEG_R", "ARM_L", "ARM_R"):
        assert body.get(str(limb[name]), 0) > 0, f"{name} carries no tagged vertices"
    assert body.get(str(limb["NONE"]), 0) > 0, "the torso must not swing"
    # hands ride with their arm, or they are left hanging in mid-air
    skin = out["skin"]
    assert skin.get(str(limb["ARM_L"]), 0) > 0 and skin.get(str(limb["ARM_R"]), 0) > 0
    assert skin.get(str(limb["NONE"]), 0) > 0, "the head must not swing"


def test_the_walk_shader_is_injected_and_cacheable(tmp_path):
    out = _run(
        """
        import { applyWalkShader } from "./world3d.mjs";
        import * as THREE from "./vendor/three.module.min.js";
        const m = applyWalkShader(new THREE.MeshStandardMaterial());
        const shader = { vertexShader: `#include <common>
#include <beginnormal_vertex>
#include <begin_vertex>` };
        m.onBeforeCompile(shader);
        console.log(JSON.stringify({
          hasLimb: shader.vertexShader.includes("attribute float limb"),
          hasPhase: shader.vertexShader.includes("attribute float phase"),
          hasSpeed: shader.vertexShader.includes("attribute float speed"),
          hasLean: shader.vertexShader.includes("attribute float lean"),
          hasBreath: shader.vertexShader.includes("attribute float breath"),
          swings: shader.vertexShader.includes("swingLimb"),
          postures: shader.vertexShader.includes("applyPosture"),
          usesWalkPos: shader.vertexShader.includes("vec3 transformed = walkPos"),
          cacheKey: m.customProgramCacheKey(),
        }));
        """,
        tmp_path,
    )
    assert out["hasLimb"] and out["hasPhase"], "the shader needs limb and phase"
    assert out["hasSpeed"] and out["hasLean"] and out["hasBreath"], "the shader needs gait and posture attributes"
    assert out["swings"], "the swing function was not injected"
    assert out["postures"], "the posture function was not injected"
    assert out["usesWalkPos"], "begin_vertex still uses the unswung position"
    assert out["cacheKey"], "identical programs must share a cache entry"


def test_lerp_angle_chooses_the_shortest_path(tmp_path):
    """Facing is a single angle, so interpolating the naive difference makes a figure spin
    the long way around. The helper must wrap across the +/-PI boundary."""
    out = _run(
        """
        import { lerpAngle } from "./world3d.mjs";
        const cases = [
          { a: 0, b: 1, t: 0.5, expected: 0.5 },
          { a: 0, b: 3, t: 0.5, expected: 1.5 },
          { a: 3, b: -3, t: 1, expected: -3 },
          { a: 0.1, b: 6.2, t: 1, expected: 6.2 },
        ];
        const results = cases.map(c => ({ ...c, got: lerpAngle(c.a, c.b, c.t) }));
        console.log(JSON.stringify({ results }));
        """,
        tmp_path,
    )
    for row in out["results"]:
        diff = row["got"] - row["expected"]
        diff = ((diff + math.pi) % (math.pi * 2)) - math.pi
        if diff < -math.pi:
            diff += math.pi * 2
        assert abs(diff) < 0.01, f"lerpAngle({row['a']}, {row['b']}, {row['t']}) = {row['got']} expected {row['expected']}"


def test_agent_animation_maps_states_to_gait_and_posture(tmp_path):
    """A fleeing agent should move faster and lean harder than a resting one. These are
    viewer-only cosmetic parameters, but they are the contract between the CPU pose and the
    vertex shader."""
    out = _run(
        """
        import { agentAnimation } from "./world3d.mjs";
        console.log(JSON.stringify({
          idle: agentAnimation('IDLE', false),
          rest: agentAnimation('REST', false),
          gather: agentAnimation('GATHER', false),
          build: agentAnimation('BUILD', false),
          seek: agentAnimation('SEEK_NEED', true),
          follow: agentAnimation('FOLLOW', true),
          flee: agentAnimation('FLEE', true),
        }));
        """,
        tmp_path,
    )
    assert out["idle"]["breath"] > 0 and out["idle"]["lean"] == 0
    assert out["flee"]["speed"] > out["seek"]["speed"]
    assert out["flee"]["lean"] > out["seek"]["lean"]

    # The real requirement is that a *standing still* agent is distinguishable, because two
    # thirds of the world is resting or idle at any moment and with only a few degrees of
    # lean between them the whole crowd read as doing nothing. Crouch is what carries that.
    assert out["rest"]["crouch"] > out["idle"]["crouch"], "resting must not look like idling"
    assert out["gather"]["lean"] > out["build"]["lean"] > out["idle"]["lean"], (
        "gathering stoops hardest, then building, then standing"
    )
    stationary = [out["idle"], out["rest"], out["gather"], out["build"]]
    poses = {(round(p["lean"], 3), round(p["crouch"], 3)) for p in stationary}
    assert len(poses) == len(stationary), f"two stationary states look identical: {poses}"

    # A large squash at constant width reads as melting rather than sitting.
    assert out["rest"]["crouch"] < 1.0


def test_standing_is_signalled_by_a_negative_phase(tmp_path):
    """Not by a large one. The phase grows without bound (clock * rate), so a 'greater than
    N' sentinel starts matching real walkers within a minute of the page loading — which it
    did, and every agent silently stopped swinging."""
    source = (VIEWER_DIR / "world3d.js").read_text()
    assert "phase < 0.0" in source, "the shader must test for a negative phase"
    assert "phase > 90" not in source, "a magnitude sentinel on an unbounded value is a bug"
    assert "let phase = -1;" in source


def test_agents_scatter_within_their_tile(tmp_path):
    """Nothing in the simulation stops two agents occupying the same tile — 78 of 112 do in a
    live world, and some of those tiles hold agents of different clans. Drawn at the tile
    centre they render at the identical point and interleave into one chimera with another
    clan's legs. The scatter is viewer-only and deterministic, so an agent keeps its spot
    across frames and across a reload."""
    source = (VIEWER_DIR / "world3d.js").read_text()
    assert "const [tileX, tileZ] = this.local(agent);" in source, "tile centre is used directly"
    assert "hash2(agent.id" in source, "the offset must be deterministic, not random"


def test_the_humanoid_has_a_readable_silhouette(tmp_path):
    """Measured: an agent is 10px tall at the overview and 73px at street level, so a face is
    1-9 pixels. What reads at that size is silhouette — a neck gap, feet giving the figure a
    base, hair breaking the bare-skull outline. Detail below that threshold is invisible."""
    out = _run(
        """
        const h = buildHumanoid();
        h.body.computeBoundingBox(); h.skin.computeBoundingBox();
        const colour = h.skin.getAttribute("color");
        let darkVerts = 0;
        for (let i = 0; i < colour.count; i++) if (colour.array[i * 3] < 0.5) darkVerts++;
        console.log(JSON.stringify({
          bodyTop: h.body.boundingBox.max.y,
          skinTop: h.skin.boundingBox.max.y,
          bodyBottom: h.body.boundingBox.min.y,
          hairVerts: darkVerts,
        }));
        """,
        tmp_path,
    )
    assert abs(out["bodyBottom"]) < 0.05, "feet must sit on the ground"
    assert out["skinTop"] > out["bodyTop"], "the head must clear the shoulders"
    assert out["hairVerts"] > 0, "hair carries a dark vertex tint; without it the head is bald"
