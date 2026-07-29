Claude-of-Duty style (exact technique to port)

Zero art assets. Only dependency: three.
GPU texture forge: 19 surfaces (concrete, brick, plaster, wood, metal, etc.).
Fragment shader writes height → albedo → ORM → Sobel normal.
Periodic noise, parallax occlusion, triplanar, curvature edge wear.

Modular building kit with real wall thickness + enterable interiors.
~120×120 m market street example proves the quality.

Our hybrid application

Wilderness & far areas: lightweight 2D / simple 3D / billboards.
Major cities, fortresses, battle zones, landmarks, disasters: full procedural modular 3D + Claude-of-Duty materials.
Agents: optional low-poly 3D models (or billboards when distant).
Camera: default top-down/isometric + optional cinematic third-person follow for interesting agents (BG3-mod inspired).

check these references:
 https://github.com/mshumer/Claude-of-Duty (especially ARCHITECTURE.md, src/materials/, src/world/)
Existing Three.js procedural building generators on GitHub (search “procedural building three.js”).

## what is built

**selective focus, not a 3d world.** the live map is still the phase 0 canvas. a three.js
overlay opens on demand over one 25-tile square — a clan centre, the densest hut cluster,
or wherever you double-click — and nothing outside it is ever meshed.

- `src/viewer/focus3d.js` — the view, the materials, hand-rolled orbit/pan/zoom
- `src/viewer/vendor/three.module.min.js` — vendored, because the reference is offline-only.
  it imports a sibling `three.core.min.js`; r150+ splits the build, so **both** files are
  needed. vendoring only the entry point resolves to a 404 and the whole viewer goes dark
- backend cost: one field (`owner` on buildings). the overlay reads the existing per-tick
  snapshot, so it is live with no new endpoint

**materials** follow the claude-of-duty rules — zero art assets, generated at init, nothing
per frame — but not its full gpu forge. height-first on the cpu: fbm value noise → height
→ albedo ramp and roughness → normal by sobel. five surfaces: plank wood with knots,
mottled and spalled plaster, coursed rubble stone, weathered shingle roof, and grass that
dries to earth where the turf thins.

**huts are modular**: a stone plinth that beds into the ground, four walls with real
thickness, a doorway gap with a lintel, a pitched roof of two leaning slabs, and a per-hut
rotation so a cluster is not stamped.

**everything is instanced.** every box in every hut is the same unit cube, sized by its
instance matrix, and agents are batched per clan. a settlement of 93 huts and 30 people
draws in ~21 calls. the per-tick path allocates nothing: huts rebuild only when the set of
buildings changes, and agents just get new matrices.

**it is lit for daylight.** `ACESFilmicToneMapping` at 1.35 exposure — with three's default
`NoToneMapping` the sun clips and the whole scene reads as night. a vertex-graded sky dome
supplies the horizon colour, and the fog is that same colour so the ground dissolves into
it. the fog range is derived from the framed camera distance, not the plot size.

## known shape of the ground

the ground is displaced by `terrainHeight`, and the same function plants every hut, tree
and agent, so nothing floats. relief is deliberately gentle (< 1 unit across a world tile)
because huts have square footprints and would gape on a real slope. beyond the settlement
the displacement tapers to flat and the plane runs far past the fog, so its square corners
never ride up over the skyline.

this is scenery, not simulation: the sim still has no elevation, and nothing in it reads
`terrainHeight`.

## verifying it

`scripts/verify_focus3d.mjs` drives real chrome against a running server and asserts on
`renderer.info` — leaks across ticks and open/close cycles, draw calls, camera edge cases,
pointer handling, and that the 2d map survives. it needs `npm i playwright` in a scratch
directory; playwright is never a project dependency. `tests/test_viewer_3d.py` covers the
gpu-free logic (cluster search, terrain, clan colours) under node and runs by default.

## not built

the full 19-surface gpu forge, triplanar and parallax occlusion, enterable interiors,
curvature edge wear, agent models beyond capsule-and-head, and the cinematic follow camera.
