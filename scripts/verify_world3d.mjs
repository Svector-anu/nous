// Adversarial check for the 3d world view.
//
// The python suite cannot see a gpu, so the things that actually go wrong in a webgl view
// — leaked materials, orphaned contexts, a camera that goes NaN when its container is
// collapsed, picking that silently never hits — are invisible to it. This drives a real
// browser against a running server and asserts on renderer.info and on the view's own
// state rather than on how the picture looks.
//
//   1. start the sim:  .venv/bin/python -m uvicorn src.api.server:create_app --factory
//   2. one-time setup: npm i playwright   (dev-only; never a project dependency)
//   3. node scripts/verify_world3d.mjs
//
// Exits non-zero if any check fails.

import { chromium } from "playwright";

const BASE_URL = process.env.NEOCIV_URL ?? "http://127.0.0.1:8000/";
const URL = BASE_URL + (BASE_URL.includes("?") ? "&" : "?") + "skipIntro=1";
const failures = [];
const results = [];

function check(name, ok, detail) {
  results.push(`  ${ok ? "pass" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
  if (!ok) failures.push(name);
}

async function openCameraTours() {
  const panel = await page.locator("#objectivesCard").first();
  if (await panel.isVisible().catch(() => false)) return;
  await page.click('[data-panel="settingsPanel"]');
  await page.click("#settingsShowTours");
  await panel.waitFor({ state: "visible" });
}

async function cleanupMobileGateAgents() {
  // The gate deploys a "MobileGate" agent every run; remove stale ones so the
  // world log and My Agents list do not accumulate test spam.
  try {
    const list = await (await fetch(`${BASE_URL}agents`)).json();
    const ids = (list.agents || [])
      .filter((a) => a.name === "MobileGate")
      .map((a) => a.id);
    for (const id of ids) {
      await fetch(`${BASE_URL}agents/${id}`, { method: "DELETE" });
    }
  } catch (error) {
    console.warn("could not clean up MobileGate agents:", error.message);
  }
}

await cleanupMobileGateAgents();

const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });

const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));

await page.goto(URL, { waitUntil: "networkidle" });
await page.waitForFunction(
  () => document.getElementById("status").textContent.trim() === "live",
  { timeout: 20000 }
);
await page.waitForTimeout(2500);

const info = () => page.evaluate(() => {
  const view = window["__world"];
  if (!view.active || !view.renderer) {
    return { mounted: false, geometries: 0, textures: 0, disposables: 0, pickIndex: 0 };
  }
  const memory = view.renderer.info.memory;
  return {
    mounted: true,
    geometries: memory.geometries,
    textures: memory.textures,
    programs: view.renderer.info.programs.length,
    calls: view.renderer.info.render.calls,
    triangles: view.renderer.info.render.triangles,
    disposables: view.disposables.length,
    agentBatches: view.agentMeshes.size,
    pickIndex: view.agentIndex.size,
  };
});

// --- 1. it mounts itself and meshes the whole world -------------------------
const mounted = await info();
check("mounts automatically on the first snapshot", mounted.mounted);
check(
  "whole world is meshed in a modest number of draw calls",
  mounted.calls > 0 && mounted.calls < 90,
  `${mounted.calls} calls, ${(mounted.triangles / 1000).toFixed(0)}k triangles`
);
const worldStats = await page.evaluate(async () => {
  const snapshot = await (await fetch("/state")).json();
  return { snapshot: snapshot.buildings.length, view: window["__world"].stats.huts };
});
check(
  "every hut in the world is present, not a focus square",
  worldStats.view === worldStats.snapshot,
  `${worldStats.view} of ${worldStats.snapshot}`
);

// --- 2. nothing accumulates while it runs ------------------------------------
await page.evaluate(async () => {
  const snapshot = await (await fetch("/state")).json();
  for (let i = 0; i < 60; i++) window["__world"].update(snapshot);
});
const ticked = await info();
check(
  "no geometry growth across 60 ticks",
  ticked.geometries === mounted.geometries,
  `${mounted.geometries} -> ${ticked.geometries}`
);
check(
  "no material growth across 60 ticks",
  ticked.disposables === mounted.disposables,
  `${mounted.disposables} -> ${ticked.disposables}`
);
check(
  "the pick index does not grow across 60 ticks",
  ticked.pickIndex === mounted.pickIndex,
  `${mounted.pickIndex} -> ${ticked.pickIndex}`
);

// --- 3. a real run, not a synthetic loop ------------------------------------
// Agents move and clans change size for real here, which is what rebuilds the instanced
// batches — the path most likely to leak.
const before = await info();
await page.waitForTimeout(12000);
const after = await info();
check(
  "geometry stable across ~12s of live ticks",
  after.geometries === before.geometries,
  `${before.geometries} -> ${after.geometries}`
);
check(
  "materials stable across ~12s of live ticks",
  after.disposables === before.disposables,
  `${before.disposables} -> ${after.disposables}`
);
check(
  "pick index stays bounded by the number of clan batches",
  after.pickIndex <= after.agentBatches * 2,
  `${after.pickIndex} entries for ${after.agentBatches} batches`
);

// --- 4. mount/unmount is symmetric ------------------------------------------
// Read the counters inside the same evaluate as the unmount. Waiting first leaves a window
// for the next snapshot to arrive and re-mount the view through ensureMounted, which made
// this check pass or fail on the timing of a once-a-second tick.
const unmounted = await page.evaluate(() => {
  const view = window["__world"];
  const renderer = view.renderer;
  view.unmount();
  return { geometries: renderer.info.memory.geometries, textures: renderer.info.memory.textures };
});
check(
  "unmount frees everything",
  unmounted.geometries === 0 && unmounted.textures === 0,
  `${unmounted.geometries} geometries, ${unmounted.textures} textures`
);

const cycles = await page.evaluate(async () => {
  const snapshot = await (await fetch("/state")).json();
  const view = window["__world"];
  for (let i = 0; i < 10; i++) {
    view.mount(snapshot);
    view.unmount();
  }
  const idle = view.renderer === null ? 0 : -1;
  // Browsers cap live webgl contexts (~16). Without forceContextLoss the cycles above
  // would exhaust them and this final mount would silently render nothing.
  view.mount(snapshot);
  return { idle, geometries: view.renderer.info.memory.geometries };
});
check("10 mount/unmount cycles release the renderer each time", cycles.idle === 0);
check("still renders after many cycles", cycles.geometries > 0, `${cycles.geometries} geometries`);

// --- 5. camera navigation ---------------------------------------------------
const camera = await page.evaluate(async () => {
  const snapshot = await (await fetch("/state")).json();
  const view = window["__world"];
  // The camera glides toward its goal, so it is not at the destination the instant a move
  // is requested. settle() collapses the glide, which is what makes these assertions about
  // *where a move aims* rather than about how far along the glide happens to be.
  const read = () => {
    view.settle();
    const f = view.viewFootprint();
    return { x: f.x, y: f.y, d: view.orbit.distance };
  };

  view.overview();
  const overview = read();

  const clan = snapshot.clans.find((c) => c.centre);
  view.flyTo(clan.centre[0], clan.centre[1], 18);
  const flown = read();

  // pan far past the world edge; the target must stay somewhere useful
  view.target.x = 1e6;
  view.target.z = -1e6;
  view._clampTarget();
  const clamped = { x: view.target.x, z: view.target.z };

  let finite = true;
  for (const c of snapshot.clans) {
    if (!c.centre) continue;
    view.flyTo(c.centre[0], c.centre[1], 18);
    if (!Number.isFinite(view.orbit.distance) || view.orbit.distance <= 0) finite = false;
    if (!Number.isFinite(view.orbit.theta) || !Number.isFinite(view.orbit.phi)) finite = false;
  }

  return { overview, flown, clamped, finite, worldSpan: view.worldSpan, grid: view.grid };
});
check(
  "overview centres on the world",
  Math.abs(camera.overview.x - camera.grid.width / 2) < 2 &&
    Math.abs(camera.overview.y - camera.grid.height / 2) < 2,
  `(${camera.overview.x.toFixed(1)}, ${camera.overview.y.toFixed(1)})`
);
check(
  "flying to a clan moves closer than the overview",
  camera.flown.d < camera.overview.d,
  `${camera.flown.d.toFixed(0)} vs ${camera.overview.d.toFixed(0)}`
);
check(
  "the target is clamped to the world",
  Math.abs(camera.clamped.x) <= camera.worldSpan && Math.abs(camera.clamped.z) <= camera.worldSpan,
  `(${camera.clamped.x.toFixed(0)}, ${camera.clamped.z.toFixed(0)})`
);
check("every clan frames to a finite camera", camera.finite);

// The checks above use settle() to skip the glide, so the glide itself needs proving
// separately: without this, easing could be broken outright and everything would still
// pass. A move must be gradual (not there yet after one frame) and must actually arrive.
await page.evaluate(() => {
  window["__director"].disable();
  window["__world"].overview();
  window["__world"].settle();
});
await page.waitForTimeout(400);
const glide = await page.evaluate(async () => {
  const view = window["__world"];
  const snapshot = await (await fetch("/state")).json();
  const clan = snapshot.clans.find((c) => c.centre);
  const from = view.orbit.distance;
  view.flyTo(clan.centre[0], clan.centre[1], 18);
  const goal = view.goal.distance;
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  const afterTwoFrames = view.orbit.distance;
  await new Promise((done) => setTimeout(done, 3500));
  const arrived = view.orbit.distance;
  return { from, goal, afterTwoFrames, arrived };
});
check(
  "a move glides rather than cutting",
  Math.abs(glide.afterTwoFrames - glide.goal) > Math.abs(glide.from - glide.goal) * 0.1,
  `${glide.from.toFixed(0)} -> ${glide.afterTwoFrames.toFixed(0)} after 2 frames, aiming at ${glide.goal.toFixed(0)}`
);
check(
  "a glide arrives at its goal",
  Math.abs(glide.arrived - glide.goal) < Math.max(0.5, glide.goal * 0.02),
  `settled at ${glide.arrived.toFixed(1)}, goal ${glide.goal.toFixed(1)}`
);

// --- 6. input clamps, through the real handlers ------------------------------
const edges = await page.evaluate(() => {
  const view = window["__world"];
  const canvas = view.renderer.domElement;
  const wheel = (deltaY) => canvas.dispatchEvent(new WheelEvent("wheel", { deltaY, cancelable: true }));
  for (let i = 0; i < 400; i++) wheel(-400);
  const zoomedIn = view.orbit.distance;
  for (let i = 0; i < 400; i++) wheel(400);
  const zoomedOut = view.orbit.distance;

  const drag = (dy) => {
    canvas.dispatchEvent(new PointerEvent("pointerdown", { pointerId: 1, clientX: 0, clientY: 0, bubbles: true }));
    canvas.dispatchEvent(new PointerEvent("pointermove", { pointerId: 1, clientX: 0, clientY: dy, bubbles: true }));
    canvas.dispatchEvent(new PointerEvent("pointerup", { pointerId: 1, clientX: 0, clientY: dy, bubbles: true }));
  };
  drag(-100000);
  const phiHigh = view.orbit.phi;
  drag(100000);
  const phiLow = view.orbit.phi;

  canvas.dispatchEvent(new PointerEvent("pointerdown", { pointerId: 2, clientX: 0, clientY: 0, bubbles: true }));
  canvas.dispatchEvent(new PointerEvent("pointercancel", { pointerId: 2, bubbles: true }));
  const beforeStray = view.orbit.theta;
  canvas.dispatchEvent(new PointerEvent("pointermove", { pointerId: 2, clientX: 500, clientY: 0, bubbles: true }));
  const dragStuck = view.orbit.theta !== beforeStray;

  const host = view.container;
  const previous = host.style.height;
  host.style.height = "0px";
  view.resize();
  const collapsed = Number.isFinite(view.camera.aspect) && view.camera.aspect > 0;
  host.style.height = previous;
  view.resize();

  return {
    zoomedIn, zoomedOut, phiHigh, phiLow, dragStuck, collapsed,
    min: view.minDistance, max: view.maxDistance,
  };
});
check(
  "wheel zoom clamps at both stops",
  edges.zoomedIn >= edges.min - 0.01 && edges.zoomedOut <= edges.max + 0.01,
  `${edges.zoomedIn.toFixed(2)} .. ${edges.zoomedOut.toFixed(0)}, limits ${edges.min.toFixed(2)}..${edges.max.toFixed(0)}`
);
check(
  "orbit never reaches either pole",
  edges.phiHigh > 0.1 && edges.phiLow < Math.PI / 2,
  `phi ${edges.phiHigh.toFixed(3)} .. ${edges.phiLow.toFixed(3)}`
);
check("a cancelled pointer does not leave the drag stuck", !edges.dragStuck);
check("collapsed container keeps a finite aspect", edges.collapsed);

// --- 7. picking ------------------------------------------------------------
// Projected from the *instance matrices*, not from simulation coordinates: agents stand on
// displaced terrain, so a guessed height misses a 13px-tall capsule entirely. Getting that
// wrong once made working picking look broken.
// Autopilot has to be off for this: projecting 40-odd agents and raycasting each takes real
// time, and a camera gliding underneath it invalidates every projection computed before it
// moved. That alone pushed misses from 2 to 8.
await openCameraTours();
await page.evaluate(() => window["__director"].disable());
await page.click("#flyDensest");
await page.waitForTimeout(1200);
const TORSO_Y = 0.9; // world units from the feet to mid-torso
const picking = await page.evaluate((TORSO_Y) => {
  const view = window["__world"];
  view.settle();
  const rect = view.renderer.domElement.getBoundingClientRect();
  const matrix = view.scratch.matrix;

  const positions = new Map();
  for (const entry of view.agentMeshes.values()) {
    for (let i = 0; i < entry.count; i++) {
      entry.body.getMatrixAt(i, matrix);
      // The humanoid is modelled with its feet at local y=0, so the instance matrix
      // translation is the ground under the agent, not its middle. Aiming a pick ray there
      // hits grass. Offset to torso height — this is a probe correction, not a code fix.
      positions.set(entry.ids[i], {
        x: matrix.elements[12], y: matrix.elements[13] + TORSO_Y, z: matrix.elements[14],
      });
    }
  }
  const eye = view.camera.position;
  const range = (q) => Math.hypot(q.x - eye.x, q.y - eye.y, q.z - eye.z);

  const point = view.target.clone();
  let onScreen = 0, exact = 0, nearerWon = 0, fartherWon = 0, missed = 0;
  for (const [id, q] of positions) {
    point.set(q.x, q.y, q.z).project(view.camera);
    if (Math.abs(point.x) > 1 || Math.abs(point.y) > 1 || point.z > 1) continue;
    onScreen++;
    const got = view.pick(
      rect.left + (point.x * 0.5 + 0.5) * rect.width,
      rect.top + (-point.y * 0.5 + 0.5) * rect.height
    );
    if (got === id) exact++;
    else if (got === null) missed++;
    else {
      const other = positions.get(got);
      // A ray hits a surface; `range` measures to a torso centre. An agent standing
      // shoulder to shoulder with the one aimed at can present a nearer surface while its
      // centre sits marginally farther, so anything inside a body width is legitimate
      // geometry rather than a mismapped instance. A humanoid here is about 0.3 world
      // units across the shoulders (TILE * 0.076 either side of the spine, TILE = 2).
      const BODY_WIDTH = 0.36;
      if (other && range(other) <= range(q) + BODY_WIDTH) nearerWon++;
      else fartherWon++;
    }
  }
  return { onScreen, exact, nearerWon, fartherWon, missed };
}, TORSO_Y);
// Aiming at an agent may legitimately return a different one when somebody stands in
// front — that is what picking means. What must never happen is returning an agent that is
// *further away* than the one aimed at, which is the signature of a broken instanceId to
// agent-id mapping and would hide behind a plain "mostly correct" assertion.
//
// Misses used to be written off as the sim being alive, which was wrong — this whole loop
// runs inside one synchronous evaluate, so no frame renders and no snapshot lands while it
// is going. They were a real bug: InstancedMesh caches its bounding sphere on first raycast
// and never refreshes it, so once agents wandered out of that stale sphere the ray
// early-outed and clicking a plainly visible agent did nothing. With the sphere invalidated
// on every pose the miss count is 0, so it is now asserted as 0 rather than budgeted.
//
// `fartherWon` stays a small proportion rather than exactly zero: it compares torso centres
// while a ray hits a surface, and the BODY_WIDTH tolerance above absorbs the honest cases.
// A genuinely broken instanceId mapping shows up as a large fraction, not one in forty.
check(
  "clicking an agent never resolves to one behind it",
  picking.fartherWon <= Math.max(1, picking.onScreen * 0.04) &&
    picking.onScreen > 5 &&
    picking.missed === 0,
  `${picking.exact}/${picking.onScreen} exact, ${picking.nearerWon} occluded by a nearer agent, ` +
    `${picking.fartherWon} resolved to a farther agent, ${picking.missed} moved out from under the cursor`
);
check(
  "clicking empty sky selects nothing",
  (await page.evaluate(() => window["__world"].pick(5, 5))) === null
);

// --- 8. the minimap stays in step with the camera ----------------------------
const minimap = await page.evaluate(async () => {
  const view = window["__world"];
  const snapshot = await (await fetch("/state")).json();
  const clan = snapshot.clans.find((c) => c.centre);
  view.flyTo(clan.centre[0], clan.centre[1], 18);
  view.settle();
  const footprint = view.viewFootprint();
  const canvas = document.getElementById("map");
  const context = canvas.getContext("2d");
  const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
  let lit = 0;
  for (let i = 0; i < data.length; i += 4 * 128) if (data[i] + data[i + 1] + data[i + 2] > 40) lit++;
  return {
    wantX: clan.centre[0], wantY: clan.centre[1],
    gotX: footprint.x, gotY: footprint.y,
    lit,
  };
});
check(
  "the minimap footprint reports where the camera actually is",
  Math.abs(minimap.gotX - minimap.wantX) < 1.5 && Math.abs(minimap.gotY - minimap.wantY) < 1.5,
  `camera at (${minimap.gotX.toFixed(1)}, ${minimap.gotY.toFixed(1)}), flew to (${minimap.wantX}, ${minimap.wantY})`
);
check("the minimap still draws", minimap.lit > 0, `${minimap.lit} lit samples`);

const travelled = await page.evaluate(() => {
  const canvas = document.getElementById("map");
  const rect = canvas.getBoundingClientRect();
  canvas.dispatchEvent(new PointerEvent("pointerdown", {
    pointerId: 9, clientX: rect.left + rect.width * 0.2, clientY: rect.top + rect.height * 0.2, bubbles: true,
  }));
  canvas.dispatchEvent(new PointerEvent("pointerup", { pointerId: 9, bubbles: true }));
  window["__world"].settle();
  const f = window["__world"].viewFootprint();
  return { x: f.x, y: f.y };
});
check(
  "clicking the minimap travels there",
  travelled.x < 25 && travelled.y < 25,
  `camera moved to (${travelled.x.toFixed(1)}, ${travelled.y.toFixed(1)})`
);

// --- 9. the cinematic autopilot ---------------------------------------------
const reading = () => page.evaluate(() => {
  const view = window["__world"];
  return {
    theta: view.orbit.theta, phi: view.orbit.phi, distance: view.orbit.distance,
    x: view.target.x, z: view.target.z,
    enabled: window["__director"].enabled,
    shot: window["__director"].shot?.kind ?? null,
    label: window["__director"].shot?.label ?? null,
  };
});
const moved = (a, b) =>
  Math.abs(a.theta - b.theta) > 1e-4 || Math.abs(a.phi - b.phi) > 1e-4 ||
  Math.abs(a.distance - b.distance) > 1e-3 ||
  Math.abs(a.x - b.x) > 1e-3 || Math.abs(a.z - b.z) > 1e-3;

await page.evaluate(() => window["__director"].enable());
await page.waitForTimeout(1500);
const auto0 = await reading();
await page.waitForTimeout(2500);
const auto1 = await reading();
check("the camera moves with nobody touching it", moved(auto0, auto1), `${auto0.shot} -> ${auto1.shot}`);
check("a shot is named for the ui", auto1.shot !== null && auto1.label !== null, `${auto1.shot} · ${auto1.label}`);

// Shots have to change over time, or it is a single static hold pretending to be a film.
const kinds = new Set();
const labels = new Set();
for (let i = 0; i < 10; i++) {
  const state = await reading();
  if (state.shot) kinds.add(state.shot);
  if (state.label) labels.add(state.label);
  await page.waitForTimeout(2500);
}
check(
  "it cuts between different shots and subjects",
  kinds.size > 1 || labels.size > 1,
  `${kinds.size} shot kinds, ${labels.size} subjects over 25s`
);

// Dragging must take the camera back at once, and it must then stay put.
await page.mouse.move(700, 500);
await page.mouse.down();
await page.mouse.move(820, 525, { steps: 6 });
await page.mouse.up();
await page.waitForTimeout(400);
const seized = await reading();
check("dragging takes control immediately", seized.enabled === false);
await page.waitForTimeout(2500);
const stillHeld = await reading();
const drift = (a, b) =>
  ["theta", "phi", "distance", "x", "z"]
    .map((k) => `${k}=${Math.abs(a[k] - b[k]).toExponential(1)}`)
    .join(" ");
check(
  "the camera stays put while you hold it",
  !moved(seized, stillHeld),
  `a drifting camera after letting go means the goal was not pinned — ` +
    `${drift(seized, stillHeld)}, director ${seized.enabled}->${stillHeld.enabled}`
);

// Autopilot leaks nothing: it drives flyTo every frame for a long stretch.
const leak0 = await page.evaluate(() => {
  window["__director"].enable();
  const view = window["__world"];
  return { g: view.renderer.info.memory.geometries, d: view.disposables.length, p: view.agentIndex.size };
});
await page.waitForTimeout(15000);
const leak1 = await page.evaluate(() => {
  const view = window["__world"];
  return { g: view.renderer.info.memory.geometries, d: view.disposables.length, p: view.agentIndex.size };
});
check(
  "15s of autopilot allocates nothing",
  leak0.g === leak1.g && leak0.d === leak1.d && leak0.p === leak1.p,
  `${JSON.stringify(leak0)} -> ${JSON.stringify(leak1)}`
);

// --- 10. the lens never ends up inside somebody --------------------------
// An agent's head sits ~1.24 above ground and the look-at point ~0.75, so a camera less
// than minEyeHeight above the target can sit level with — or inside — a skull, which put a
// giant head across the frame. The floor applies to manual input too: you can still zoom
// right in to look at someone, you just cannot end up in them.
const clearance = await page.evaluate(async () => {
  const view = window["__world"];
  window["__director"].disable();
  const snapshot = await (await fetch("/state")).json();
  const busy = snapshot.agents[0];
  view.flyTo(busy.x, busy.y, 18);
  view.settle();

  const canvas = view.renderer.domElement;
  const eye = () => view.orbit.distance * Math.cos(view.orbit.phi);

  // zoom in as hard as the wheel allows
  for (let i = 0; i < 600; i++) {
    canvas.dispatchEvent(new WheelEvent("wheel", { deltaY: -400, cancelable: true }));
  }
  const zoomed = eye();

  // then drag the angle toward the horizon, which lowers the lens for a fixed distance
  for (let i = 0; i < 40; i++) {
    canvas.dispatchEvent(new PointerEvent("pointerdown", { pointerId: 3, clientX: 0, clientY: 0, bubbles: true }));
    canvas.dispatchEvent(new PointerEvent("pointermove", { pointerId: 3, clientX: 0, clientY: 400, bubbles: true }));
    canvas.dispatchEvent(new PointerEvent("pointerup", { pointerId: 3, clientX: 0, clientY: 400, bubbles: true }));
  }
  const dragged = eye();

  // and every shot the director composes, across many cuts
  window["__director"].enable();
  let worst = Infinity;
  for (let i = 0; i < 900; i++) {
    window["__director"].advance(1 / 60);
    worst = Math.min(worst, eye());
  }
  return { zoomed, dragged, worst, floor: view.minEyeHeight };
});
check(
  "zooming in cannot put the lens below head height",
  clearance.zoomed >= clearance.floor - 0.01,
  `${clearance.zoomed.toFixed(2)} vs floor ${clearance.floor.toFixed(2)}`
);
check(
  "dragging toward the horizon cannot either",
  clearance.dragged >= clearance.floor - 0.01,
  `${clearance.dragged.toFixed(2)} vs floor ${clearance.floor.toFixed(2)}`
);
check(
  "no directed shot drops the lens below head height",
  clearance.worst >= clearance.floor - 0.01,
  `worst over 900 frames ${clearance.worst.toFixed(2)} vs floor ${clearance.floor.toFixed(2)}`
);

// --- 11. street level is low and among the huts, not an aerial -------------
await openCameraTours();
await page.evaluate(() => window["__director"].disable());
await page.click("#flyStreet");
await page.waitForTimeout(2500);
const street = await page.evaluate(() => {
  const view = window["__world"];
  return {
    phi: view.orbit.phi,
    distance: view.orbit.distance,
    eye: view.orbit.distance * Math.cos(view.orbit.phi),
  };
});
check(
  "street level is a low angle",
  street.phi > Math.PI * 0.4,
  `phi ${(street.phi * 180 / Math.PI).toFixed(0)} deg`
);
check(
  "street level stands among the huts rather than outside them",
  street.distance < 35,
  `${street.distance.toFixed(0)} units out, eye ${street.eye.toFixed(1)} up ` +
    "(distance is height/cos(phi) at this angle, so a tall camera is also a distant one)"
);

// --- 11b. desktop navigation: one dock, one panel, nothing stuck open ------------
// Guards the regression this refactor fixed: a duplicated CSS block forced every
// overlay to display:flex on desktop, so panels clustered on top of the world and
// their close buttons looked dead. Each dock click runs closeAllPanels(), so the
// resulting visible set is deterministic no matter what earlier tests left open.
const nav = await page.evaluate(() => {
  const PANELS = [
    "worldOverviewCard", "marketsCard", "messagesCard", "worldLogCard", "agentCards",
    "deployPanel", "legendPanel", "settingsPanel", "controlsPanel", "objectivesCard",
  ];
  const visible = () => PANELS.filter((id) => {
    const el = document.getElementById(id);
    return el && getComputedStyle(el).display !== "none";
  });
  const clickDock = (panelId) => {
    document.querySelector(`#bottomDock .dock-btn[data-panel="${panelId}"]`).click();
  };
  clickDock("worldOverviewCard");
  const afterWorld = visible();
  clickDock("marketsCard");
  const afterBets = visible();
  clickDock("marketsCard"); // second click toggles it back off
  const afterToggleOff = visible();
  return {
    afterWorld, afterBets, afterToggleOff,
    dockButtons: document.querySelectorAll("#bottomDock .dock-btn").length,
    // Navigation is one rail down the left edge. What matters is that it is a single
    // vertical group — the old failure was navigation split across two places, which is
    // why this asserts the shape rather than merely counting buttons.
    rail: (() => {
      const dock = document.getElementById("bottomDock");
      const box = dock.getBoundingClientRect();
      const first = dock.querySelector(".dock-btn").getBoundingClientRect();
      const last = [...dock.querySelectorAll(".dock-btn")].pop().getBoundingClientRect();
      return {
        vertical: box.height > box.width,
        onLeft: box.left < window.innerWidth / 2,
        // Stacked, not laid out side by side.
        stacked: last.top > first.top,
        width: box.width,
        // Hugs its items rather than stretching down the whole window. Pinned top and
        // bottom it spread eight buttons over the full viewport and read as an empty
        // sidebar. Measured as a fraction of the window so it holds at any height.
        heightFraction: box.height / window.innerHeight,
      };
    })(),
  };
});
check(
  "opening World shows only the World panel",
  nav.afterWorld.length === 1 && nav.afterWorld[0] === "worldOverviewCard",
  `visible: ${nav.afterWorld.join(", ") || "none"}`
);
check(
  "opening Bets replaces World — one panel at a time",
  nav.afterBets.length === 1 && nav.afterBets[0] === "marketsCard",
  `visible: ${nav.afterBets.join(", ") || "none"}`
);
check(
  "toggling a dock button off hides its panel",
  nav.afterToggleOff.length === 0,
  `still visible: ${nav.afterToggleOff.join(", ") || "none"}`
);
check(
  // 9 since Payments joined the rail. The count is asserted rather than left loose so a
  // stray button cannot be added without somebody deciding the rail should grow.
  "navigation is one vertical 9-button rail down the left edge",
  nav.dockButtons === 9 && nav.rail.vertical && nav.rail.onLeft && nav.rail.stacked,
  `buttons=${nav.dockButtons}, vertical=${nav.rail.vertical}, onLeft=${nav.rail.onLeft}, ` +
    `stacked=${nav.rail.stacked}, width=${nav.rail.width.toFixed(0)}px`
);
check(
  "the rail hugs its items instead of stretching down the window",
  nav.rail.heightFraction < 0.8,
  `rail is ${(nav.rail.heightFraction * 100).toFixed(0)}% of window height`
);

// Top bar: stat deltas and the "Following X" chip are pure derivations exposed on
// window.__topbar, so they are asserted directly rather than raced against live snapshots.
const topbar = await page.evaluate(() => {
  const t = window.__topbar;
  const delta = document.getElementById("topAgentsDelta");
  t.setStatDelta("topAgentsDelta", 5, 3);
  const up = { text: delta.textContent, cls: delta.className };
  t.setStatDelta("topAgentsDelta", 2, 6);
  const down = { text: delta.textContent, cls: delta.className };
  t.setStatDelta("topAgentsDelta", 4, 4);
  const same = { text: delta.textContent, cls: delta.className };

  // The day counter gained its own delta with the two-row bar; it is a distinct element
  // from the agent counter, so it is asserted separately.
  const dayDelta = document.getElementById("topDayDelta");
  t.setStatDelta("topDayDelta", 251, 250);
  const day = { text: dayDelta ? dayDelta.textContent : null, cls: dayDelta ? dayDelta.className : null };

  const chip = document.getElementById("followChip");
  const subject = document.getElementById("followSubject");
  const isActive = () => chip.classList.contains("active");
  t.setFollowFromLabel("vista · Ada is following food");
  const followed = { active: isActive(), subject: subject.textContent };

  // The chip sits between the shelf status and the control hints. It overlapped both when
  // it was absolutely centred, so its box is measured against theirs while it is showing.
  const box = (el) => { const b = el.getBoundingClientRect(); return { l: b.left, r: b.right, w: b.width }; };
  const chipBox = box(chip);
  const statusBox = box(document.querySelector("#topBar .shelf-status"));
  const hintsBox = box(document.querySelector("#topBar .shelf-hints"));
  const overlaps = (a, b) => a.w > 0 && b.w > 0 && a.l < b.r && b.l < a.r;
  const layout = {
    overlapsStatus: overlaps(chipBox, statusBox),
    overlapsHints: overlaps(chipBox, hintsBox),
    chipWidth: chipBox.w,
  };

  t.setFollowFromLabel("whole world");
  const cleared = { active: isActive(), subject: subject.textContent };
  return { up, down, same, day, followed, cleared, layout };
});
check(
  "a rising stat shows a green ▲delta",
  topbar.up.text === "▲2" && topbar.up.cls.includes("up"),
  `text="${topbar.up.text}" cls="${topbar.up.cls}"`
);
check(
  "a falling stat shows a red ▼delta",
  topbar.down.text === "▼4" && topbar.down.cls.includes("down"),
  `text="${topbar.down.text}" cls="${topbar.down.cls}"`
);
check(
  "an unchanged stat shows no delta",
  topbar.same.text === "" && !/\b(up|down)\b/.test(topbar.same.cls),
  `text="${topbar.same.text}" cls="${topbar.same.cls}"`
);
check(
  "the day counter has its own rising delta",
  topbar.day.text === "▲1" && topbar.day.cls.includes("up"),
  `text="${topbar.day.text}" cls="${topbar.day.cls}"`
);
check(
  "following a subject shows the chip with just the subject name",
  topbar.followed.active === true && topbar.followed.subject === "Ada",
  `active=${topbar.followed.active}, subject="${topbar.followed.subject}"`
);
check(
  "returning to the whole world hides the follow chip",
  topbar.cleared.active === false && topbar.cleared.subject === "",
  `active=${topbar.cleared.active}, subject="${topbar.cleared.subject}"`
);
check(
  "the follow chip clears the shelf status and the control hints",
  topbar.layout.chipWidth > 0 && !topbar.layout.overlapsStatus && !topbar.layout.overlapsHints,
  `width=${topbar.layout.chipWidth}, status=${topbar.layout.overlapsStatus}, hints=${topbar.layout.overlapsHints}`
);

// --- 11a. the wallet control --------------------------------------------------
// There is no wallet extension in this browser, so the states are driven through
// window.__wallet rather than through a real signature. What is asserted is the part the
// spectator sees: a feature that is off costs the bar no chrome, a half-configured server
// says so instead of failing silently, and a connected wallet actually authorizes calls.
const walletUi = await page.evaluate(() => {
  const w = window.__wallet;
  const button = document.getElementById("walletBtn");
  const label = document.getElementById("walletLabel");
  const read = () => ({
    shown: button.classList.contains("show"),
    disabled: button.disabled,
    label: label.textContent,
    connected: button.classList.contains("connected"),
  });

  w.setWalletSession("", "");
  w.applyChainConfig({ identity_enabled: false, ready: false });
  const off = read();

  w.applyChainConfig({ identity_enabled: true, ready: false });
  const halfConfigured = read();

  w.applyChainConfig({ identity_enabled: true, ready: true });
  const idle = read();
  const anonymousHeaders = w.walletAuthHeaders();

  w.setWalletSession("0xabcdef0123456789abcdef0123456789abcdef01", "session-token-xyz");
  const connected = read();
  const authHeaders = w.walletAuthHeaders();

  // Disconnecting must actually drop the token, or "log out" is a lie.
  w.setWalletSession("", "");
  const afterDisconnect = { ...read(), headers: w.walletAuthHeaders() };

  w.applyChainConfig({ identity_enabled: false, ready: false });
  return { off, halfConfigured, idle, connected, anonymousHeaders, authHeaders, afterDisconnect };
});
check(
  "the wallet control stays out of the bar while wallet identity is off",
  walletUi.off.shown === false,
  `shown=${walletUi.off.shown}`
);
// The button's copy, in one place. It says what the control is for rather than how it
// works, so it is the kind of text that gets rewritten — and three checks depend on it.
const WALLET_IDLE_LABEL = "Claim your agents";
const WALLET_OFF_LABEL = "Claiming off";

check(
  "a server that cannot verify signatures shows a disabled wallet button, not a dead one",
  walletUi.halfConfigured.shown === true && walletUi.halfConfigured.disabled === true &&
    walletUi.halfConfigured.label === WALLET_OFF_LABEL,
  `shown=${walletUi.halfConfigured.shown}, disabled=${walletUi.halfConfigured.disabled}, label="${walletUi.halfConfigured.label}"`
);
check(
  "a ready server invites a claim and sends no authorization",
  walletUi.idle.shown === true && walletUi.idle.disabled === false &&
    walletUi.idle.label === WALLET_IDLE_LABEL &&
    walletUi.anonymousHeaders.Authorization === undefined,
  `label="${walletUi.idle.label}", disabled=${walletUi.idle.disabled}, auth=${walletUi.anonymousHeaders.Authorization}`
);
// Every wallet failure used to read "Cancelled" or "Failed". A locked wallet and a
// rejected signature both raise 4001, so the case the user caused and the case they
// cannot even see said the same thing — and these paths only run when a wallet
// misbehaves, which is exactly the code nobody notices breaking.
const trouble = await page.evaluate(() => {
  const t = window.__wallet.walletTrouble;
  const of = (e) => { const r = t(e); return { label: r.label, hint: r.hint }; };
  return {
    locked: of({ code: 4001, message: "wallet must has at least one account" }),
    rejected: of({ code: 4001, message: "User rejected the request." }),
    pending: of({ code: -32002, message: "Request already pending" }),
    disconnected: of({ code: 4900, message: "disconnected" }),
  };
});
// A phone has no extensions at all, so "install one and reload" is advice that cannot
// be followed there — the page has to say something different depending on where it is.
const noWallet = await page.evaluate(() => {
  const t = window.__wallet.walletTrouble;
  const real = navigator.userAgent;
  const at = (ua) => {
    Object.defineProperty(navigator, "userAgent", { value: ua, configurable: true });
    const r = t({ message: "no provider" });
    Object.defineProperty(navigator, "userAgent", { value: real, configurable: true });
    return r;
  };
  return {
    phone: at("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"),
    desktop: at("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"),
  };
});
check(
  "a phone is not told to install a browser extension",
  noWallet.phone.label === "Open in wallet" &&
    /wallet app/i.test(noWallet.phone.hint) &&
    !/install/i.test(noWallet.phone.hint) &&
    /install/i.test(noWallet.desktop.hint),
  `phone="${noWallet.phone.label}", desktop="${noWallet.desktop.label}"`
);
check(
  "a locked wallet is not reported as a cancelled one",
  trouble.locked.label === "Unlock wallet" && trouble.rejected.label === "Cancelled" &&
    trouble.locked.hint !== trouble.rejected.hint,
  `locked="${trouble.locked.label}", rejected="${trouble.rejected.label}"`
);
check(
  "each wallet failure explains what to do about it",
  [trouble.locked, trouble.rejected, trouble.pending, trouble.disconnected]
    .every((t) => t.hint.length > 30 && /\b(open|install|finish|sign)/i.test(t.hint)),
  `hints: ${[trouble.locked, trouble.pending, trouble.disconnected].map((t) => t.label).join(", ")}`
);
check(
  "a connected wallet shows its address and authorizes requests",
  walletUi.connected.connected === true && walletUi.connected.label === "0xabcd…ef01" &&
    walletUi.authHeaders.Authorization === "Bearer session-token-xyz",
  `label="${walletUi.connected.label}", auth=${walletUi.authHeaders.Authorization}`
);
check(
  "disconnecting drops the session token",
  walletUi.afterDisconnect.connected === false &&
  walletUi.afterDisconnect.label === WALLET_IDLE_LABEL &&
    walletUi.afterDisconnect.headers.Authorization === undefined,
  `label="${walletUi.afterDisconnect.label}", auth=${walletUi.afterDisconnect.headers.Authorization}`
);

// --- 11b. the three events a spectator must not miss ----------------------------
// A death, a fight and a succession are rare and cannot be scheduled, so waiting for the
// live world to produce one would make this gate flaky. Instead a pair of hand-built
// snapshots is pushed through the real detector, the real log renderer and the real flash
// spawner. Nothing here is a mock: window.__events.feed calls processWorldLog, and
// view.update is the same method the websocket calls.
const spectator = await page.evaluate(() => {
  const agent = (id, name, extra = {}) => ({
    id, name, x: 10 + id, y: 10, state: "IDLE", energy: 50, hunger: 50, food: 0, wood: 0,
    clan: 1, user: false, personality: "steady", wants: "", huts: 0, raids_won: 0,
    raids_lost: 0, received: 0, target: null, standing: 0, rank: "", rest_mode: false,
    owner: "", ...extra,
  });
  const snap = (agents, clans, tick) => ({
    tick, day: 1, agents, buildings: [], resources: [], clans,
    grid: { width: 64, height: 64 },
  });
  const clan = (leader) => ({ id: 1, size: 2, goal: "gather food", leader, centre: [12, 10] });

  const before = snap([agent(1, "Ada"), agent(2, "Bo")], [clan(1)], 1);
  const kindsOf = (entries) => entries.map((e) => e.kind);

  // 1. a death: the agent is simply absent from the next snapshot.
  const died = window.__events.feed(before, snap([agent(2, "Bo")], [clan(2)], 2));

  // 2. a raid: the winner's raids_won goes up.
  const raided = window.__events.feed(
    before,
    snap([agent(1, "Ada", { raids_won: 1 }), agent(2, "Bo")], [clan(1)], 2)
  );

  // 3. a succession: same people, same place, new leader.
  const led = window.__events.feed(before, snap([agent(1, "Ada"), agent(2, "Bo")], [clan(2)], 2));

  // The flashes are counted on the real view. update() is what the socket calls.
  const view = window["__world"];
  view.update(before);
  const flashesBefore = view.flashSprites.length;
  view.update(snap([agent(2, "Bo")], [clan(2)], 2));
  const flashesAfter = view.flashSprites.length;

  return {
    died: { kinds: kindsOf(died), label: died.find((e) => e.kind === "death")?.label },
    raided: { kinds: kindsOf(raided), label: raided.find((e) => e.kind === "raid")?.label },
    led: { kinds: kindsOf(led), label: led.find((e) => e.kind === "leader")?.label },
    logText: document.getElementById("worldLog").textContent,
    flashGain: flashesAfter - flashesBefore,
    configured: Object.keys(view.flashTextures),
  };
});
check(
  "a death is reported in plain English",
  spectator.died.kinds.includes("death") && /died/.test(spectator.died.label || ""),
  `kinds=[${spectator.died.kinds}] label="${spectator.died.label}"`
);
check(
  "a raid is reported in plain English",
  spectator.raided.kinds.includes("raid") && /raid/.test(spectator.raided.label || ""),
  `kinds=[${spectator.raided.kinds}] label="${spectator.raided.label}"`
);
check(
  "a new clan leader is reported by name",
  spectator.led.kinds.includes("leader") && /Bo now leads clan 1/.test(spectator.led.label || ""),
  `kinds=[${spectator.led.kinds}] label="${spectator.led.label}"`
);
// A leader that reasoned says why, and that sentence must reach the log. It only appears
// when a model actually answered — the rules leave goal_reason empty — so nothing in a
// default $0 world would notice this breaking.
const reasoned = await page.evaluate(() => {
  const agent = (id) => ({
    id, name: `A${id}`, x: 10, y: 10, state: "IDLE", energy: 50, hunger: 50, food: 0,
    wood: 0, clan: 1, user: false, personality: "", wants: "", huts: 0, raids_won: 0,
    raids_lost: 0, received: 0, target: null, standing: 0, rank: "", rest_mode: false,
    owner: "",
  });
  const snap = (goal, reason) => ({
    tick: 2, day: 1, agents: [agent(1)], buildings: [], resources: [],
    clans: [{ id: 1, size: 2, goal, goal_reason: reason, leader: 1, centre: [12, 10] }],
    grid: { width: 64, height: 64 },
  });
  const before = snap("gather food", "");
  const after = snap("build huts", "The young cannot afford to wait.");
  const events = window.__events.findEvents(before, after);
  window.__events.feed(before, after);
  const goalEvent = events.find((e) => e.kind === "goalChange");
  return {
    carried: Boolean(goalEvent && goalEvent.reason),
    rendered: !!document.querySelector("#worldLog .feed-reason"),
    text: document.querySelector("#worldLog .feed-reason")?.textContent || "",
  };
});
// A payment that leaves no trace is indistinguishable from one that did nothing. This
// only fires when somebody actually pays, so nothing else in the suite would notice it
// breaking — and it is the one path that involves real money.
const receipt = await page.evaluate(() => {
  // The link needs an explorer, which normally arrives from /chain/config. Set it
  // explicitly so this checks the receipt rather than the timing of a fetch.
  window.__wallet.applyChainConfig({
    chain_id: 4663, chain_name: "Robinhood Chain",
    explorer_url: "https://robinhoodchain.blockscout.com",
    identity_enabled: true, ready: true, x402_enabled: true,
  });
  const agent = (id) => ({
    id, name: `A${id}`, x: 10, y: 10, state: "IDLE", energy: 50, hunger: 50, food: 0,
    wood: 0, clan: 1, user: false, personality: "", wants: "", huts: 0, raids_won: 0,
    raids_lost: 0, received: 0, target: null, standing: 0, rank: "", rest_mode: false,
    owner: "",
  });
  const snap = (paid) => ({
    tick: 2, day: 1, agents: [agent(1)], buildings: [], resources: [], paid,
    clans: [{ id: 1, size: 2, goal: "rally", goal_reason: "", leader: 1, centre: [12, 10] }],
    grid: { width: 64, height: 64 },
  });
  const before = snap([]);
  const after = snap([{ clan_id: 1, tick: 2, proof: "tx:0xfeed1234" }]);
  const events = window.__events.findEvents(before, after);
  window.__events.feed(before, after);
  const link = document.querySelector("#worldLog .feed-proof");
  return {
    reported: events.some((e) => e.kind === "paid"),
    linked: !!link,
    href: link ? link.getAttribute("href") : "",
  };
});
check(
  "a payment is announced in the world with a checkable receipt",
  receipt.reported && receipt.linked && /0xfeed1234$/.test(receipt.href),
  `reported=${receipt.reported}, linked=${receipt.linked}, href=${receipt.href.slice(-30)}`
);
check(
  "a leader's reasoning reaches the world log",
  reasoned.carried && reasoned.rendered && /young cannot afford/.test(reasoned.text),
  `carried=${reasoned.carried}, rendered=${reasoned.rendered}, text=${reasoned.text.slice(0, 48)}`
);
check(
  "the world log actually renders the event text",
  /now leads clan/.test(spectator.logText),
  `log="${spectator.logText.replace(/\s+/g, " ").trim().slice(0, 80)}"`
);
check(
  "death and succession have their own map flashes",
  spectator.configured.includes("death") && spectator.configured.includes("leader"),
  `configured: ${spectator.configured.join(", ")}`
);
check(
  "a death and a succession put flashes on the map",
  spectator.flashGain >= 2,
  `${spectator.flashGain} new flashes (expected a death and a leader change)`
);

// Hand the view and the log back a real snapshot. Without this the next socket message is
// diffed against a two-agent fixture, which would report the whole population as dead.
await page.evaluate(async () => {
  const real = await (await fetch("/state")).json();
  window["__world"].update(real);
  window.__events.feed(real, real);
});

// --- 11y. payments are visible after the moment they happen -----------------------
// The world log announced a payment on the tick it landed and then it scrolled away, so
// every receipt the world held was invisible a minute later. This asserts the standing
// list: the amount, and a link to the transaction it can be checked against.
const payments = await page.evaluate(() => {
  const render = window.__payments.renderPayments;
  render({ paid: [] });
  const empty = document.getElementById("paymentsList").textContent.trim();

  // Chronological, the way the world appends them — the list reverses for display.
  render({
    paid: [
      // Predates the world recording an amount: the row must not invent one.
      { clan_id: 3, tick: 109000, proof: "tx:0xdef456" },
      { clan_id: 7, tick: 110400, proof: "tx:0xabc123", amount: "0.10", currency: "USDG" },
    ],
  });
  const list = document.getElementById("paymentsList");
  const rows = [...list.querySelectorAll("li")];
  return {
    empty,
    count: rows.length,
    // Newest first, so the 0.10 receipt at the later tick leads.
    first: rows[0]?.textContent.replace(/\s+/g, " ").trim() ?? "",
    second: rows[1]?.textContent.replace(/\s+/g, " ").trim() ?? "",
    links: rows.map((r) => r.querySelector("a.feed-proof")?.getAttribute("href") ?? ""),
  };
});
check(
  "a payment is listed with what it cost and a link to the transaction",
  payments.count === 2 &&
    payments.first.includes("0.10 USDG") &&
    payments.first.includes("clan 7") &&
    payments.links[0].includes("0xabc123"),
  `${payments.first} | link=${payments.links[0] || "(none)"}`
);
check(
  "a receipt with no recorded amount does not invent one",
  !/\d+\.\d+/.test(payments.second) && payments.second.includes("clan 3"),
  payments.second
);
check(
  "an empty payments list says so rather than showing nothing",
  payments.empty.length > 0,
  payments.empty
);

// --- 11z. the link preview -------------------------------------------------------
// A launch link with no og:image renders as a grey rectangle, and that rectangle is the
// first thing most people ever see of this. The image is also asserted to actually load:
// a tag pointing at a 404 looks correct in the html and shows nothing in a feed.
const share = await page.evaluate(async () => {
  const meta = (sel) => document.querySelector(sel)?.getAttribute("content") ?? "";
  const image = meta('meta[property="og:image"]');
  // The tag has to be absolute — a scraper resolves it against nothing — but the file it
  // names is fetched from *this* server. Fetching the production url instead would make
  // the gate pass or fail on whatever is deployed, which is not what it is checking.
  let status = 0;
  try {
    status = (await fetch(new URL(image).pathname)).status;
  } catch {
    status = -1;
  }
  return {
    image,
    status,
    card: meta('meta[name="twitter:card"]'),
    twitterImage: meta('meta[name="twitter:image"]'),
    description: meta('meta[property="og:description"]'),
  };
});
check(
  "a shared link carries an image that actually loads",
  share.image.startsWith("https://") && share.status === 200 && share.twitterImage === share.image,
  `${share.image || "(no og:image)"} -> ${share.status}`
);
check(
  "the share card is the large format, not a thumbnail",
  share.card === "summary_large_image",
  `twitter:card=${share.card || "(unset)"}`
);
check(
  "the share text claims no agent count",
  !/\d+\s*agents/i.test(share.description),
  share.description.slice(0, 80)
);

// --- 11a. "my agents" means mine ------------------------------------------------
// agent.user means "deployed by a visitor" — any visitor. Filtering on it showed every
// player every other player's agents under My Agents, with Rest and Claim on top of a
// stranger's character. The most expensive bug in this build, and invisible on a laptop
// where you are the only player.
const ownership = await page.evaluate(() => {
  const m = window.__mine;
  localStorage.removeItem(m.MINE_KEY);
  localStorage.removeItem(m.LEGACY_NAME_KEY);
  // Two agents called "elsie": one a stranger's, one ours. This is the shape that was
  // wrong on the live site — the local record was the *name*, so both looked ours.
  const strangerElsie = { id: 1, name: "elsie", user: true, owner: "" };
  const ourElsie = { id: 2, name: "elsie", user: true, owner: "" };
  const snap = {
    agents: [
      strangerElsie,
      ourElsie,
      { id: 3, name: "bo", user: true, owner: "0xAAAA" },        // stranger's, claimed
      { id: 4, name: "mine-claimed", user: true, owner: "0xBBBB" }, // ours, by wallet
      { id: 9, name: "native", user: false, owner: "" },         // not a visitor's at all
    ],
  };
  const ids = (list) => list.map((a) => a.id).sort();

  window.__wallet.setWalletSession("", "");
  const strangerSeesNothing = ids(m.myAgents(snap));

  // A name in the old storage key must buy nothing at all.
  localStorage.setItem(m.LEGACY_NAME_KEY, JSON.stringify(["elsie"]));
  const legacyNameBuysNothing = ids(m.myAgents(snap));
  localStorage.removeItem(m.LEGACY_NAME_KEY);

  m.rememberMine(ourElsie);
  const deployedOnly = ids(m.myAgents(snap));

  window.__wallet.setWalletSession("0xbbbb", "session");
  const withWallet = ids(m.myAgents(snap));

  // The same predicate every control uses, checked directly: a stranger's agent must
  // never be actionable, and our own must be — even sharing a name.
  const strangerControls = m.isMine(strangerElsie);
  const ownControls = m.isMine(ourElsie);

  window.__wallet.setWalletSession("", "");
  localStorage.removeItem(m.MINE_KEY);
  return {
    strangerSeesNothing,
    legacyNameBuysNothing,
    deployedOnly,
    withWallet,
    strangerControls,
    ownControls,
  };
});
// Paying required opening a browser console until now, which is not a product.
const payUi = await page.evaluate(() => {
  window.__wallet.applyChainConfig({
    chain_id: 4663, chain_name: "Robinhood Chain", x402_enabled: true,
    x402_price: "0.10", x402_currency: "USDG", identity_enabled: true, ready: true,
    explorer_url: "https://robinhoodchain.blockscout.com",
  });
  document.querySelector('#bottomDock .dock-btn[data-panel="legendPanel"]').click();
  const button = document.querySelector("#legend .nudge");
  const paysFor = window.__mine.chainPaysFor();
  document.querySelector('#bottomDock .dock-btn[data-panel="legendPanel"]').click();
  return { paysFor, present: !!button, label: button ? button.textContent : "" };
});
check(
  "paying does not require a console",
  payUi.paysFor === true && payUi.present && /0\.10 USDG/.test(payUi.label),
  `button=${payUi.present}, label="${payUi.label}"`
);
// A typed handle is not an identity — anyone can enter any name and collect another
// starting balance. A connected wallet is, so it has to win.
const who = await page.evaluate(() => {
  const w = window.__wallet, m = window.__mine;
  document.getElementById("whoami").value = "pretender";
  w.setWalletSession("", "");
  const guest = m.bettor();
  w.setWalletSession("0xD0A2362c6cF02f8FdaCD3E2aBCbfBc625AA0f967", "s");
  const owned = m.bettor();
  w.setWalletSession("", "");
  document.getElementById("whoami").value = "";
  return { guest, owned };
});
check(
  "a connected wallet bets as itself, not as a typed handle",
  who.guest === "pretender" && who.owned !== "pretender" && /0xD0A2|0xd0a2/.test(who.owned),
  `guest="${who.guest}", connected="${who.owned}"`
);
check(
  "an agent sharing a name with mine is still not mine",
  ownership.strangerControls === false && ownership.ownControls === true &&
    JSON.stringify(ownership.deployedOnly) === JSON.stringify([2]),
  `stranger=${ownership.strangerControls}, own=${ownership.ownControls}, mine=[${ownership.deployedOnly}]`
);
check(
  "a visitor who deployed nothing owns nothing",
  ownership.strangerSeesNothing.length === 0,
  `saw: ${ownership.strangerSeesNothing.join(", ") || "nothing"}`
);
check(
  "a name left in the old storage key claims nothing",
  ownership.legacyNameBuysNothing.length === 0,
  `saw: ${ownership.legacyNameBuysNothing.join(", ") || "nothing"}`
);
check(
  "my agents never contains somebody else's agent",
  JSON.stringify(ownership.deployedOnly) === JSON.stringify([2]) &&
    JSON.stringify(ownership.withWallet) === JSON.stringify([2, 4]),
  `deployed-only: [${ownership.deployedOnly}] · with wallet: [${ownership.withWallet}]`
);

// --- 11bis. a strip panel is long and horizontal, and nothing is cut off ----------
// The failure this catches: PREDICTIONS opened as a full-width panel whose content was
// stacked vertically, so a market was sliced in half by the bottom edge and the
// leaderboard was never reachable at all.
const strip = await page.evaluate(() => {
  document.querySelector('#bottomDock .dock-btn[data-panel="marketsCard"]').click();
  const card = document.getElementById("marketsCard");
  const body = card.querySelector(".card-body");
  const markets = document.getElementById("markets");
  const board = card.querySelector(".markets-board");
  const dock = document.getElementById("bottomDock");
  const box = (el) => el.getBoundingClientRect();
  const cardBox = box(card);
  const styles = getComputedStyle(body);
  // Every market must fit inside the body, not just the first — the first one to render
  // is often a short settled card, so testing only that one passed while open markets
  // had their YES/NO buttons sliced off by the bottom edge.
  const all = [...markets.querySelectorAll(".market")];
  const bodyBottom = box(body).bottom;
  const overflowBottom = all.reduce((worst, m) => Math.max(worst, box(m).bottom - bodyBottom), -Infinity);
  // Measuring the card's box is not enough: with overflow-y on the card, the box fits
  // neatly while the YES/NO buttons inside are scrolled out of sight. What a spectator
  // actually loses is content, so compare each card's content height to its visible one.
  const hiddenInside = all.reduce((worst, m) => Math.max(worst, m.scrollHeight - m.clientHeight), 0);
  return {
    wide: cardBox.width > 700,
    row: styles.flexDirection === "row",
    marketsRow: getComputedStyle(markets).flexDirection === "row",
    boardVisible: board ? box(board).width > 0 : false,
    // Clear of the rail: with navigation down the left edge, a panel that started at
    // the window edge would sit underneath it.
    gapToDock: cardBox.left - box(dock).right,
    overflowBottom,
    hiddenInside,
    hasMarket: all.length > 0,
    marketCount: all.length,
  };
});
check(
  "an open strip panel is long and horizontal",
  strip.wide === true && strip.row === true && strip.marketsRow === true,
  `width>700=${strip.wide}, body=row:${strip.row}, markets=row:${strip.marketsRow}`
);
check(
  "the panel clears the rail instead of sliding under it",
  strip.gapToDock > 8,
  `${strip.gapToDock.toFixed(1)}px between rail and panel`
);
check(
  "no market is cut off by the bottom of the panel",
  strip.hasMarket === false || (strip.overflowBottom <= 1 && strip.hiddenInside <= 1),
  `worst of ${strip.marketCount}: ${strip.overflowBottom.toFixed(0)}px past the body, ` +
    `${strip.hiddenInside.toFixed(0)}px hidden inside the card`
);
check(
  "the leaderboard is reachable, not pushed out of the panel",
  strip.boardVisible === true,
  `leaderboard column visible: ${strip.boardVisible}`
);
await page.evaluate(() => {
  document.querySelector('#bottomDock .dock-btn[data-panel="marketsCard"]').click();
});

// --- 11d. paying on chain: the calldata must be exactly right --------------------
// A wrong offset here sends real money to the wrong address, and no amount of testing
// downstream would catch it. Asserted against a known-good encoding rather than by
// driving a wallet, which the gate has no way to do.
const pay = await page.evaluate(async () => {
  const p = window.__pay;
  const to = "0x1111111111111111111111111111111111111111";
  const data = p.encodeTransfer(to, "100000");
  // 402 handling: a response that is not payable must be returned as-is, never paid.
  const notPayable = await p.fetchWithPayment("/chain/config");
  return {
    data,
    selector: data.slice(0, 10),
    length: data.length,
    addressWord: data.slice(10, 74),
    amountWord: data.slice(74, 138),
    passesThroughNon402: notPayable.status,
  };
});
check(
  "an erc-20 transfer is encoded exactly",
  // 4-byte selector + two 32-byte words = 4 + 32 + 32 bytes = 138 hex chars with 0x.
  pay.selector === "0xa9059cbb" &&
    pay.length === 138 &&
    pay.addressWord === "0".repeat(24) + "1".repeat(40) &&
    // 100000 = 0x186a0, right-aligned in its word.
    pay.amountWord === "0".repeat(59) + "186a0",
  `selector=${pay.selector} len=${pay.length} addr=…${pay.addressWord.slice(-6)} amt=…${pay.amountWord.slice(-6)}`
);
check(
  "a response that is not a 402 is passed straight through",
  pay.passesThroughNon402 === 200,
  `status ${pay.passesThroughNon402}`
);

// --- 11c. ambient sound: silent by default, and never required -------------------
// The bed is synthesised, so there is nothing to download and nothing to hear in a
// headless browser. What matters is asserted instead: that it stays off until asked,
// that the mood mapping tracks the world, and that the graph really starts and stops.
const audioIdle = await page.evaluate(() => {
  const a = window.__audio;
  const agents = (n, state) => Array.from({ length: n }, (_, i) => ({ id: i + 1, state }));
  return {
    supported: a.supported(),
    enabled: a.ambience.enabled,
    sounding: document.getElementById("soundToggle").classList.contains("sounding"),
    pressed: document.getElementById("soundToggle").getAttribute("aria-pressed"),
    // Pure mapping, asserted without a speaker.
    calm: a.moodFor({ agents: agents(10, "IDLE") }),
    active: a.moodFor({ agents: [...agents(6, "GATHER"), ...agents(4, "IDLE")] }),
    tense: a.moodFor({ agents: [...agents(9, "IDLE"), ...agents(1, "FLEE")] }),
    empty: a.moodFor({ agents: [] }),
  };
});
check(
  "ambient sound is off until a spectator asks for it",
  audioIdle.enabled === false && audioIdle.sounding === false && audioIdle.pressed === "false",
  `enabled=${audioIdle.enabled}, sounding=${audioIdle.sounding}, pressed=${audioIdle.pressed}`
);
check(
  "the bed follows what the world is doing",
  audioIdle.calm === "calm" && audioIdle.active === "active" &&
    audioIdle.tense === "tense" && audioIdle.empty === "calm",
  `idle=${audioIdle.calm}, working=${audioIdle.active}, fleeing=${audioIdle.tense}, empty=${audioIdle.empty}`
);

// A real click, because a stored preference is not a gesture and a context started
// without one stays suspended and silent.
await page.click("#soundToggle");
// Wait for the graph to exist before timing the fade. Loading and decoding the beds takes
// however long it takes, so a fixed sleep would read the ramp part-way up on a slow decode
// and call the music quiet when it is merely still arriving.
await page.waitForFunction(() => window.__audio.ambience.enabled === true, { timeout: 20000 });
await page.waitForTimeout(1500);
const audioOn = await page.evaluate(() => {
  const a = window.__audio;
  return {
    enabled: a.ambience.enabled,
    running: a.ambience.ctx ? a.ambience.ctx.state : null,
    voices: a.ambience.voices.length,
    sounding: document.getElementById("soundToggle").classList.contains("sounding"),
    stored: (() => { try { return localStorage.getItem("nous.sound"); } catch { return null; } })(),
    master: a.ambience.master ? a.ambience.master.gain.value : 0,
    hasLimiter: !!a.ambience.limiter,
    defaultVolume: a.DEFAULT_VOLUME,
    usingBeds: a.ambience.usingBeds,
    bedCount: a.ambience.bedNodes ? Object.keys(a.ambience.bedNodes).length : 0,
  };
});
check(
  "turning sound on builds a running audio graph",
  audioOn.enabled === true && audioOn.sounding === true && audioOn.running !== "closed" &&
    // Either path is a valid graph: real loops when the manifest names them, the synth
    // when it does not. Asserting only one would go red the moment the beds changed.
    (audioOn.usingBeds ? audioOn.bedCount === 3 : audioOn.voices >= 3),
  `enabled=${audioOn.enabled}, ctx=${audioOn.running}, beds=${audioOn.usingBeds}, ` +
    `bedCount=${audioOn.bedCount}, voices=${audioOn.voices}, stored=${audioOn.stored}`
);
// The shipped manifest names three CC0 loops, so this deployment must actually be playing
// them. If it silently fell back to the synth the music would be wrong and nothing else
// here would notice.
check(
  "the shipped loops are what plays, not the fallback synth",
  audioOn.usingBeds === true && audioOn.bedCount === 3,
  `usingBeds=${audioOn.usingBeds}, loaded=${audioOn.bedCount}`
);
check(
  "the choice is remembered",
  audioOn.stored === "on",
  `localStorage nous.sound = ${audioOn.stored}`
);
// The whole point of the loudness pass: the master must actually sit high, and it must be
// safe to do so. A high master with no limiter is how the old bed would have clipped.
check(
  "the music plays loud enough to hear, through a limiter",
  audioOn.master >= 0.7 && audioOn.hasLimiter === true,
  `master gain ${audioOn.master}, limiter ${audioOn.hasLimiter}, default ${audioOn.defaultVolume}`
);

const volume = await page.evaluate(async () => {
  const a = window.__audio;
  const slider = document.getElementById("musicVolume");
  a.setVolume(0.3);
  const lowered = a.ambience.volume;
  // The graph is retargeted rather than set, so read the destination the ramp is heading
  // for instead of racing it.
  slider.value = "90";
  slider.dispatchEvent(new Event("input", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 120));
  return {
    lowered,
    fromSlider: a.ambience.volume,
    shown: document.getElementById("musicVolumeValue").textContent,
    stored: (() => { try { return localStorage.getItem("nous.volume"); } catch { return null; } })(),
    muteStillWorks: (() => { a.ambience.setVolume(0); return a.ambience.volume === 0; })(),
  };
});
check(
  "the volume control moves the music and is remembered",
  Math.abs(volume.lowered - 0.3) < 1e-6 && Math.abs(volume.fromSlider - 0.9) < 1e-6 &&
    volume.shown === "90" && Math.abs(parseFloat(volume.stored) - 0.9) < 1e-6,
  `set=${volume.lowered}, slider=${volume.fromSlider}, shown=${volume.shown}, stored=${volume.stored}`
);
check(
  "the volume control reaches silence",
  volume.muteStillWorks === true,
  `zero accepted: ${volume.muteStillWorks}`
);

await page.click("#soundToggle");
await page.waitForTimeout(300);
const audioOff = await page.evaluate(() => {
  const a = window.__audio;
  // The world must keep running with sound fully off — this is the path every visitor
  // who never touches the button takes.
  a.ambience.setMood("tense");
  return {
    enabled: a.ambience.enabled,
    sounding: document.getElementById("soundToggle").classList.contains("sounding"),
    stored: (() => { try { return localStorage.getItem("nous.sound"); } catch { return null; } })(),
    live: document.getElementById("status").textContent.trim(),
  };
});
check(
  "turning it off silences it and the world carries on",
  audioOff.enabled === false && audioOff.sounding === false && audioOff.stored === "off" &&
    audioOff.live === "live",
  `enabled=${audioOff.enabled}, stored=${audioOff.stored}, status=${audioOff.live}`
);

// --- 12. mobile viewport: usable at ~390px without horizontal breakage -----------
// The desktop floating-card layout is too wide for a phone. This checks the adaptive
// layout (bottom sheets, icon dock, capped pixel ratio) and that the deploy → find
// → rest/resume flow can be driven with touch events.
const mobileBrowser = await chromium.launch({ channel: "chrome" });
const mobilePage = await mobileBrowser.newPage({
  viewport: { width: 390, height: 844 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 2,
});
const mobileErrors = [];
mobilePage.on("console", (m) => { if (m.type() === "error") mobileErrors.push(m.text()); });
mobilePage.on("pageerror", (e) => mobileErrors.push(`pageerror: ${e.message}`));

await mobilePage.goto(URL, { waitUntil: "networkidle" });
await mobilePage.waitForFunction(
  () => document.getElementById("status").textContent.trim() === "live",
  { timeout: 20000 }
);
await mobilePage.waitForTimeout(2500);

const mobileInfo = await mobilePage.evaluate(() => {
  const view = window["__world"];
  const docWidth = document.documentElement.scrollWidth;
  const clientWidth = document.documentElement.clientWidth;
  return {
    ratio: view.renderer.getPixelRatio(),
    docWidth,
    clientWidth,
    docOverflow: docWidth > clientWidth + 1,
    activeCards: Array.from(document.querySelectorAll("#rightCards .floating-card.open, #agentCards.open, #deployPanel.open, #legendPanel.open, #settingsPanel.open")).map((el) => el.id),
  };
});
check(
  "mobile pixel ratio is capped to save GPU",
  mobileInfo.ratio <= 1.3,
  `ratio ${mobileInfo.ratio.toFixed(2)}`
);
check(
  "mobile page has no horizontal overflow",
  !mobileInfo.docOverflow,
  `${mobileInfo.docWidth} vs ${mobileInfo.clientWidth}`
);
check(
  "mobile panels start closed",
  mobileInfo.activeCards.length === 0,
  `open: ${mobileInfo.activeCards.join(", ")}`
);

// Open the deploy sheet, deploy an agent, then find it and rest it.
await mobilePage.click('[data-panel="deployPanel"]');
await mobilePage.waitForTimeout(300);
await mobilePage.fill("#agentName", "MobileGate");
await mobilePage.click('button[type="submit"]');
// Wait for the agent to actually arrive rather than guessing at a duration. A deploy is
// queued and spawns on the next tick, so the true wait is at least one tick plus a render
// — 1200ms was enough on this machine and not on a slower ci runner, where this failed as
// "0 agents". renderMyAgents runs on every snapshot whether or not the panel is open, so
// the list is a fair thing to poll before opening it.
await mobilePage
  .waitForFunction(
    () => document.querySelectorAll("#myAgentsList .my-agent-item").length > 0,
    { timeout: 15000 }
  )
  .catch(() => {}); // let the check below report it rather than throwing here
await mobilePage.click('[data-agents="1"]');
await mobilePage.waitForTimeout(300);
const deployedFlow = await mobilePage.evaluate(() => {
  const list = document.getElementById("myAgentsList");
  const items = list ? list.querySelectorAll(".my-agent-item").length : 0;
  const deployOpen = document.getElementById("deployPanel").classList.contains("open");
  const myAgentsOpen = document.getElementById("myAgentsCard").classList.contains("open");
  return { items, deployOpen, myAgentsOpen };
});
check(
  "mobile deploy adds an agent and switches to my agents",
  deployedFlow.items > 0 && !deployedFlow.deployOpen && deployedFlow.myAgentsOpen,
  `${deployedFlow.items} agents, deploy ${deployedFlow.deployOpen}, myAgents ${deployedFlow.myAgentsOpen}`
);

// The My Agents row has the same fault the inspector had: rebuilt every snapshot, so its
// Locate / Rest / Copy buttons were destroyed once a second and a tap in flight hit a
// detached node. This is the list ci actually caught it on, so it is asserted directly by
// node identity rather than left to the timing of the click below.
const rowSurvives = await mobilePage.evaluate(async () => {
  const actions = () => document.querySelector("#myAgentsList .my-agent-actions");
  const first = actions();
  if (!first) return { tested: false };
  await new Promise((resolve) => setTimeout(resolve, 3400));
  return {
    tested: true,
    sameNode: actions() === first,
    stillInDocument: document.contains(first),
  };
});
check(
  "the my-agents row survives the list redrawing around it",
  rowSurvives.tested === true && rowSurvives.sameNode && rowSurvives.stillInDocument,
  rowSurvives.tested
    ? `sameNode=${rowSurvives.sameNode}, inDocument=${rowSurvives.stillInDocument}`
    : "no agent row — nothing was tested"
);

const findBtn = await mobilePage.locator(".find-agent").first();
if (await findBtn.isVisible().catch(() => false)) {
  await findBtn.click();
  await mobilePage.waitForTimeout(800);
  const inspectorOpen = await mobilePage.evaluate(() =>
    document.getElementById("agentCards").classList.contains("open")
  );
  check("mobile find agent opens inspector", inspectorOpen);

  // The card is redrawn every snapshot. Its buttons used to be destroyed and rebuilt with
  // it, so a tap in flight landed on a node that had already left the document and did
  // nothing — rare with a mouse, routine with a finger, and reliable enough on a slow ci
  // runner to fail the toggle check below every time. Node identity is the only thing that
  // distinguishes the two cases: the card looks identical either way.
  const survives = await mobilePage.evaluate(async () => {
    const actions = () => document.querySelector("#inspector .agent-card-actions");
    const first = actions();
    if (!first) return { tested: false };
    await new Promise((resolve) => setTimeout(resolve, 3400));
    return {
      tested: true,
      sameNode: actions() === first,
      stillInDocument: document.contains(first),
    };
  });
  check(
    "the inspector's buttons survive the card redrawing around them",
    survives.tested === true && survives.sameNode && survives.stillInDocument,
    survives.tested
      ? `sameNode=${survives.sameNode}, inDocument=${survives.stillInDocument}`
      : "inspector had no actions row — nothing was tested"
  );

  // Target the inspector's button specifically. ".rest-toggle" also matches the My Agents
  // list, so .first() picked whichever happened to be earlier in the dom — a different
  // control depending on which panel was open.
  const restBtn = await mobilePage.locator("#inspector .rest-toggle").first();
  if (await restBtn.isVisible().catch(() => false)) {
    const beforeText = await restBtn.textContent();
    await restBtn.click();
    // The server applies the toggle on the next tick; wait for the snapshot to arrive.
    await mobilePage.waitForTimeout(2200);
    const afterText = await mobilePage.locator("#inspector .rest-toggle").first().textContent();
    const resting =
      (beforeText.toLowerCase().includes("rest") && afterText.toLowerCase().includes("resume")) ||
      (beforeText.toLowerCase().includes("resume") && afterText.toLowerCase().includes("rest"));
    check("mobile rest toggle updates state", resting, `"${beforeText.trim()}" -> "${afterText.trim()}"`);
  }
}

check("no mobile console errors", mobileErrors.length === 0, mobileErrors.slice(0, 3).join(" | "));
await mobileBrowser.close();

// --- 13. selection indicator appears after picking an agent ------------------
await page.evaluate(() => {
  window["__world"].overview();
  window["__world"].settle();
});
await page.waitForTimeout(500);
const pickingIndicator = await page.evaluate(() => {
  const view = window["__world"];
  // Pick any agent id from the current batches; the indicator is purely visual.
  let id = null;
  for (const entry of view.agentMeshes.values()) {
    if (entry.count > 0) { id = entry.ids[0]; break; }
  }
  if (!id) return { selected: false, id: null };
  const before = view.selectionIndicator.visible;
  view.setSelected(id);
  const after = view.selectionIndicator.visible;
  return { selected: true, before, after, id };
});
check("selection indicator is hidden before picking", !pickingIndicator.before);
check("selection indicator shows after picking", pickingIndicator.after);


// Reset selection so later checks do not carry it.
await page.evaluate(() => window["__world"].setSelected(null));

check("no console errors from our code", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));

await browser.close();

// Clean up the test agent deployed by the mobile browser. The server persists the
// world, so this prevents repeated gate runs from filling the log with "MobileGate".
await cleanupMobileGateAgents();

console.log(results.join("\n"));
if (failures.length) {
  console.log(`\n${failures.length} failed: ${failures.join(", ")}`);
  process.exit(1);
}
console.log(`\nall ${results.length} checks passed`);
