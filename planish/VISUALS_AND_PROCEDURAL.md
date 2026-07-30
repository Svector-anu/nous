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

**3d is the main view (phase b).** the whole world is meshed and the three.js canvas fills
the page; the phase 0 canvas is now a 200 px minimap in the corner showing territory plus a
ring for where the camera is looking. click the minimap to travel, click an agent in 3d to
inspect it, click a clan in the legend to fly to it.

this replaced the earlier "selective focus square" because the reason for that constraint
went away. it existed when every hut was seven separate meshes — 651 draw calls for 93
huts. after instancing, the *entire* world measured **50 draw calls and 179k triangles at a
vsync-locked 60fps**, the same frame time as the old 25-tile overlay. measured at radius 12,
24, 32 and 40 before committing to the change; all four were 16.6–16.7 ms p50.

so there is no streaming and no chunk loading: everything is resident, and the camera zooms
from a whole-world overview down to street level.

- `src/viewer/world3d.js` — the view, the materials, hand-rolled orbit/pan/zoom, picking
- `src/viewer/vendor/three.module.min.js` — vendored, because the reference is offline-only.
  it imports a sibling `three.core.min.js`; r150+ splits the build, so **both** files are
  needed. vendoring only the entry point resolves to a 404 and the whole viewer goes dark
- backend cost: one field (`owner` on buildings). the view reads the existing per-tick
  snapshot, so it is live with no new endpoint. **no simulation rule changed for any of
  this** — no adjacency bias, no hut-cap change, and nothing in the sim reads `terrainHeight`

**materials** follow the claude-of-duty rules — zero art assets, generated at init, nothing
per frame — but not its full gpu forge. height-first on the cpu: fbm value noise → height
→ albedo ramp and roughness → normal by sobel. five surfaces: plank wood with knots,
mottled and spalled plaster, coursed rubble stone, weathered shingle roof, and grass that
dries to earth where the turf thins.

**the noise is periodic**, which the reference calls out ("periodic noise so everything
tiles seamlessly") and which is not optional. the lattice wraps, so u=0 and u=1 hit the
same lattice point and there is no discontinuity at the tile edge. two things have to wrap,
not one: the noise lattice *and* any per-cell index derived from uv (the stone block index
and roof tile index each pick a hash — unwrapped they read index 5 on one edge and 0 on the
other). any sine term also has to complete a whole number of cycles across the axis.

measured, wrap-edge against the strongest edge the surface already contains itself: all
five surfaces ≤ 1.01x. before this, plaster jumped 4.0x and grass 3.0x, which drew a grid
over every wall and the entire ground — grass tiles 60x60 across the plot.

**terrain noise is deliberately *not* periodic.** it is sampled by world position over one
finite plane and never tiled, so a period would show as a repeating landscape. that is why
there are two noise paths (`fbm` for textures, `openFbm` for terrain) and they must not be
crossed. both directions are pinned by tests.

**huts are modular**: a stone plinth that beds into the ground, four walls with real
thickness, a doorway gap with a lintel, a pitched roof of two leaning slabs, and a per-hut
rotation so a cluster is not stamped.

**everything is instanced.** every box in every hut is the same unit cube, sized by its
instance matrix, and agents are batched per clan. the whole world draws in ~50 calls. the
per-tick path allocates nothing: huts rebuild only when the set of buildings changes, and
agents just get new matrices. instanced batches set `frustumCulled = false`, because culling
is computed from the source geometry rather than the instances and a whole batch can
otherwise vanish while its members are plainly on screen.

**picking is a raycast against the agent batches**, resolving `instanceId` back to an agent
id through a per-batch id array. bodies and heads share one array so a hit on either
resolves to the same person. when testing this, project from the *instance matrix*, not from
simulation coordinates — agents stand on displaced terrain, and a guessed height misses a
13 px capsule entirely. that mistake made working picking look broken for a while.

**it is lit for daylight.** `ACESFilmicToneMapping` at 1.35 exposure — with three's default
`NoToneMapping` the sun clips and the whole scene reads as night. a vertex-graded sky dome
supplies the horizon colour, and the fog is that same colour so the ground dissolves into it.
fog range is re-derived from the camera distance every frame, because that distance now
spans street level to a whole-world overview.

**the sky dome's vertex colours are converted to linear explicitly.** vertex colours in a
`BufferAttribute` are consumed raw and bypass three's colour management, while `fog.color`
is converted for us. feeding sRGB values there rendered the horizon at rgb(196,206,215)
against the fogged ground's rgb(159,178,196) and drew a hard V where they met — measured off
the actual framebuffer, not guessed.

**shadows follow the look-at point.** one 2048 map stretched over the whole world gives a hut
about 26 shadow pixels, so instead the shadow camera covers a fixed window that rides with
the camera target. detail stays where you are looking and simply drops off in the distance,
where the fog eats it anyway.

**two ground planes.** a detailed displaced one covering the world, and a very large flat one
carrying the horizon. the detailed plane has to stay modest so its segments land where the
settlements are, which puts its own edge inside the view — and a silhouette edge there meets
the dome at a point where the dome is already graded toward the zenith. the far plane is two
triangles and reaches well past the fog, so the horizon is always fogged ground.

## known shape of the ground

the ground is displaced by `terrainHeight`, and the same function plants every hut, tree
and agent, so nothing floats. relief is deliberately gentle (< 1 unit across a world tile)
because huts have square footprints and would gape on a real slope. beyond the world the
displacement tapers to flat, so the detailed plane's square corners never ride up over the
skyline.

this is scenery, not simulation: the sim still has no elevation, and nothing in it reads
`terrainHeight`.

## verifying it

`scripts/verify_world3d.mjs` drives real chrome against a running server — 26 checks on
`renderer.info` and on the view's own state: leaks across 60 synthetic ticks *and* across ~12
seconds of real ones (agents actually moving and clans changing size is what rebuilds the
instanced batches, so it is the path most likely to leak), draw calls, mount/unmount
symmetry, webgl context exhaustion, camera framing for every clan, zoom and orbit clamps
driven through the real event handlers, picking accuracy against projected instance matrices,
and minimap/camera agreement. it needs `npm i playwright` in a scratch directory; playwright
is never a project dependency.

`tests/test_viewer_3d.py` covers the gpu-free logic (cluster search proved equivalent to the
exhaustive scan, terrain determinism and gradient bounds, texture seams, clan colours) under
node and runs in the default suite.

## not built

the full 19-surface gpu forge, triplanar and parallax occlusion, enterable interiors,
curvature edge wear, agent models beyond capsule-and-head, and the cinematic follow camera.
