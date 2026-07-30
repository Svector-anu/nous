"""The focus view's pure logic, exercised under node.

Anything touching webgl needs a real gpu and lives in scripts/verify_world3d.mjs. What is
left is still worth guarding here: the cluster search picks where the camera goes, and the
terrain function decides where every hut is planted, so a silent change to either moves
the whole scene.
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
    """Run a snippet against world3d.js. The module imports three.js by absolute url, which
    only resolves when the server is serving it, so rewrite that to a relative path."""
    shutil.copytree(VIEWER_DIR / "vendor", tmp_path / "vendor")
    source = (VIEWER_DIR / "world3d.js").read_text()
    source = source.replace('"/static/vendor/three.module.min.js"', '"./vendor/three.module.min.js"')
    (tmp_path / "world3d.mjs").write_text(source)
    (tmp_path / "main.mjs").write_text(
        textwrap.dedent(
            """
            import { densestCluster, terrainHeight, clanHue } from "./world3d.mjs";
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
