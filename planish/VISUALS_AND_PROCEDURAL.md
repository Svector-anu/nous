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

**the camera directs itself (phase b2).** `src/viewer/director.js` watches the per-tick
snapshot, works out what is worth looking at, and composes a shot around it. this is the
"cinematic follow" from the original visual spec, generalised: you do not have to drive.

- **events come from diffing two snapshots.** a single snapshot cannot tell you a raid just
  happened, only that raid counts are non-zero. so the director keeps the previous one and
  reports raids, deaths, births of user agents, new huts, clan goal changes and fleeing
  agents.
- **scores are the editorial policy**: violence > construction > an empty field. pinned by a
  test, because if it inverts the camera dutifully films nothing while a war happens.
- **shot kinds**: orbit (slow circle), push (dolly in), follow (track a mover from low),
  survey (crane across), establish (wide, slow drift), and **vista** — low and among the
  huts, the one that reads as being *in* the settlement rather than above it. chosen with a
  *seeded* rng, so the same world produces the same film and behaviour changes show up as a
  diff.
- **a vista is specified by camera height, not by a span.** at that angle the distance is
  height/cos(phi), so a taller camera is also a *further* one: asking for 6.8 units up put
  the lens 48 units out, outside the village looking across an empty field. 3.2 up lands it
  around 23 out, among the huts.
- **the lens never sits below head height.** an agent's head is ~1.24 above ground and the
  look-at point ~0.75, so a camera less than `minEyeHeight` above the target can end up
  level with — or inside — a skull. the floor adjusts *distance* rather than angle, because
  raising the angle would quietly undo the low shots, and it applies to manual zoom and drag
  as well as to directed shots: you can still zoom right in on someone, you just cannot end
  up in them.
- **the sky gradient is flat near the horizon** (`t**1.6`, not `sqrt(t)`). sqrt is steepest
  at t=0, which is exactly where it must be flattest: the ground silhouette sits a couple of
  degrees *below* horizontal, so the dome pixels immediately above it were already 19% toward
  the zenith — measured as a 108-value rgb step against the fogged ground, i.e. a hard line
  across the sky.
- **nothing sets the camera directly any more.** everything sets a goal and the frame loop
  eases toward it, frame-rate independently, with distance eased in log space — from a
  whole-world overview to street level is two orders of magnitude, and easing that linearly
  crawls then lurches.
- **touching the view takes control instantly** and pins the goal to where the camera
  actually is; without that pinning the view keeps drifting toward the director's in-flight
  destination after you let go. autopilot resumes after 12s idle, unless you turned it off
  by hand, in which case it stays off.
- **standing events are penalised on repeat.** a user-deployed agent generates an event every
  tick, so without a repeat penalty *and* treating the crowd/world as real candidates rather
  than an empty-list fallback, one agent held the camera for the whole session. both were
  needed; the first alone did not fix it.

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

**agents are humanoids, merged into two geometries.** head, torso, arms, legs, hands — but
six InstancedMeshes per clan would be ~90 draw calls for the agents alone, more than the
whole world costs. so the parts are merged at init into one clan-coloured geometry and one
skin geometry, which is exactly the two batches per clan the capsule-and-sphere placeholder
used. measured: **40 -> 38 draw calls and 147k -> 134k triangles**. blocky boxes are cheaper
than the smooth capsule they replaced, so readable people cost less than the placeholder.

**limbs swing in the vertex shader.** merging costs independent limb transforms on the cpu,
so each vertex is tagged with the limb it belongs to and each *instance* carries a walk
phase. the shader rotates legs about the hip and arms about the shoulder, counter-swinging,
which buys real locomotion while keeping two batches per clan. the alternative — a batch per
limb so each could carry its own matrix — is four to six per clan, 60-90 draw calls for the
agents alone.

the first attempt was a bob and a lean with no limb motion, and it was **worse than nothing**:
`abs(sin)` on body height makes a figure *hop* rather than walk. legs have to articulate for
the eye to read steps. the bob that survives is small and runs at twice the stride frequency,
because a walk lifts you twice per cycle.

two bugs on the way in, both worth remembering. the standing sentinel was `phase > 90`, but
phase grows without bound (`clock * rate`), so within a minute of page load every real walker
matched the sentinel and silently stopped swinging — a magnitude test on an unbounded value
is never a sentinel. and the `InstancedBufferAttribute` was set on the *shared* humanoid
geometry, so every clan wrote its phases into one buffer and the last batch mounted won; each
batch needs its own geometry clone.

every gap in the figure is load-bearing. flush against the torso the arms read as shoulders
and the head reads as fused — the first pass looked like a bollard, and it only showed at
magnification, not in a full-frame screenshot. `tests/test_viewer_3d.py` pins the head
clearing the torso, the feet sitting at local y=0, and the triangle budget.

the walk runs from the render clock in `_poseAgents`, called per frame rather than per
snapshot — animated on tick arrival it would step at 1 Hz. it is cosmetic and touches no
simulation state.

**the fidelity ceiling is set by on-screen size, not by ambition.** measured: an agent is
10 px tall at the overview, 32 at a settlement, 73 at street level. a face is 1-9 pixels for
almost all viewing. metahuman-grade detail would be invisible — polygons spent on a nose are
polygons nobody can see. what reads at that size is **silhouette, proportion and variation**,
so that is where the effort goes:

- neck gap, feet, and hair, because each changes the outline. flush limbs read as shoulders
  and a bald sphere reads as a bollard — both were the first pass, and both were invisible in
  a full-frame screenshot and obvious at 4x.
- **per-agent height and build**, varied deterministically off the agent id via the instance
  matrix scale. free, and it is the single biggest cure for a crowd reading as clones.
- **per-agent skin tone** via `setColorAt`, with hair carried as a dark *vertex* tint in the
  same batch — a third batch per clan would be 50% more draw calls for a few hundred pixels.

**agents are scattered within their tile, and this is not cosmetic polish.** nothing in the
simulation stops two agents sharing a tile: 78 of 112 do, and some tiles hold agents of
*different clans*. drawn at the tile centre they render at the identical point and interleave
into one figure wearing another clan's legs. the scatter is deterministic off the id, so an
agent keeps its spot across frames and reloads. viewer-only — the simulation still sees one
tile, and no rule changed.

**what is not achievable here**: metahuman is unreal-only, and a browser equivalent caps out
around 5-20 characters rather than 500. the real blocker is not triangles but instancing —
rigged skeletal characters cannot share one instanced draw call without baking animation into
a texture. the place for hero-detail characters is an intro or deploy screen, where one
character at 600 px earns the budget that 500 at 32 px never will.

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

`scripts/verify_world3d.mjs` drives real chrome against a running server — 39 checks on
`renderer.info` and on the view's own state: leaks across 60 synthetic ticks *and* across ~12
seconds of real ones (agents actually moving and clans changing size is what rebuilds the
instanced batches, so it is the path most likely to leak), draw calls, mount/unmount
symmetry, webgl context exhaustion, camera framing for every clan, zoom and orbit clamps
driven through the real event handlers, picking accuracy against projected instance matrices,
minimap/camera agreement, and the autopilot (it moves unattended, it cuts between subjects,
a drag seizes it, and it holds still afterwards). it needs `npm i playwright` in a scratch
directory; playwright is never a project dependency.

two traps in that harness, both of which hid real defects:

- **`settle()` before asserting a camera position.** the camera glides now, so it is not at
  its destination the instant a move is requested. but a check that only ever settles could
  not tell easing from a hard cut, so there are separate assertions that a move *is* gradual
  after two frames and that it *does* arrive.
- **read `renderer.info` in the same evaluate as `unmount()`.** the old check read a helper
  that returned hardcoded zeros for an unmounted view, so it asserted nothing at all —
  and had been passing vacuously while a 2048² shadow map leaked on every unmount. the
  shadow map is owned by the light, not the scene graph, and `renderer.dispose()` does not
  free it.

`tests/test_viewer_3d.py` covers the gpu-free logic (cluster search proved equivalent to the
exhaustive scan, terrain determinism and gradient bounds, texture seams, clan colours) under
node and runs in the default suite.

## not built

the full 19-surface gpu forge, triplanar and parallax occlusion, enterable interiors,
curvature edge wear, agent models beyond capsule-and-head, and the cinematic follow camera.
