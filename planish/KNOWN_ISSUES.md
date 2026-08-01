# Known issues and recent fixes

## Viewer / movement presentation

### Agents visually passing through hut walls

**Status:** fixed in viewer.

**Why it existed:** The simulation deliberately lets agents occupy the same tile as a hut. The movement system cannot treat huts as solid obstacles: a test run with building-collision enabled dropped the world into famine and made every clan raid (`12` of `30` agents survived, all clans stuck on `gather_food`, healthy worlds raided). The economy relies on agents walking through the hut tile to reach resources on the other side.

**Fix applied:** in `src/viewer/world3d.js`, agents on a hut tile are now rendered at the hut's door opening instead of the tile centre. This is a purely cosmetic correction — the simulation still sees the agent on its original tile.

### Agents sinking / floating due to wrong animation pivots

**Status:** fixed in viewer.

**Why it existed:** `HIP_Y` and `SHOULDER_Y` in the walk shader were left at the old placeholder values (`TILE * 0.30` and `TILE * 0.58`) while `buildHumanoid()` had been rewritten with taller proportions, legs, feet, a neck and face. The leg swing and torso lean rotated around the wrong points, so feet lifted off the ground and the torso detached from the hips during gather/rest poses.

**Fix applied:** pivots updated to match the merged geometry.

- `HIP_Y = TILE * 0.325` — top of the legs / bottom of the torso.
- `SHOULDER_Y = TILE * 0.625` — arm attachment to the torso.

A test was added to verify these against the actual geometry bounding data.

### Agent gait still looks mechanical

**Status:** addressed but not perfect.

The current walk is a two-rise bob plus limb swing via vertex shader. It reads as a walk at the distances the camera usually sits, but it is not a full skeletal gait. Per-limb CPU animation was rejected because it costs six InstancedMeshes per clan (60–90 draw calls for agents alone). The shader-based limb swing keeps the cost at two batches per clan.

## Simulation

### Movement collision with buildings

**Status:** explicitly *not* implemented in simulation.

A prototype that made huts solid obstacles caused mass starvation and raiding in the standard test seeds, breaking the established survival metrics. The fix lives entirely in the viewer. If the design intent later changes, the economy thresholds and resource layout would need to be re-measured and retuned together with any obstacle system.

## Tests

- Full suite: `358 passed`.
- `tests/test_viewer_3d.py`: `22 passed`, including new coverage for pivot points.
- `scripts/verify_world3d.mjs`: **39/39 pass** against a live server. Playwright lives in a
  scratch directory, never in the repo — `npm i playwright` somewhere outside the tree and
  symlink `node_modules` for the run. "Not installed in the repo" is the correct state, not
  a reason to skip the gate.

## Files touched

- `src/viewer/world3d.js` — pivot constants, door placement for agents on huts.
- `tests/test_viewer_3d.py` — new pivot test.
