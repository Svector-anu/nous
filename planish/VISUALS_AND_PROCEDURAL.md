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
overlay opens on demand over one 25-tile square — a clan centre, or the densest hut
cluster — and nothing outside it is ever meshed. ~700 meshes in focus against ~2 860 for
the whole world.

- `src/viewer/focus3d.js` — the view, the materials, hand-rolled orbit/pan/zoom
- `src/viewer/vendor/three.module.min.js` — vendored, because the reference is offline-only
- backend cost: one field (`owner` on buildings). the overlay reads the existing per-tick
  snapshot, so it is live with no new endpoint

**materials** follow the claude-of-duty rules — zero art assets, generated at init, nothing
per frame — but not its full gpu forge. height-first on the cpu: fbm value noise → height
→ albedo ramp and roughness → normal by sobel. three surfaces: plank wood, mottled plaster,
shingle roof.

**huts are modular already**: four walls with real thickness, a doorway gap with a lintel,
a pitched roof of two leaning slabs, and a per-hut rotation so a cluster is not stamped.

## not built

the full 19-surface gpu forge, triplanar and parallax occlusion, enterable interiors,
curvature edge wear, agent models beyond capsules, the cinematic follow camera, and any
kind of terrain — the simulation has no elevation or biome to render.
