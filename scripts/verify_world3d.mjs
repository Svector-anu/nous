// Adversarial check for the 3d world view.
//
// The python suite cannot see a gpu, so the things that actually go wrong in a webgl view
// — leaked materials, orphaned contexts, a camera that goes NaN when its container is
// collapsed, picking that silently never hits — are invisible to it. This drives a real
// browser against a running server and asserts on renderer.info and on the view's own
// state rather than on how the picture looks.
//
//   1. start the sim:  .venv/bin/python -m uvicorn src.api.server:app --factory
//   2. one-time setup: npm i playwright   (dev-only; never a project dependency)
//   3. node scripts/verify_world3d.mjs
//
// Exits non-zero if any check fails.

import { chromium } from "playwright";

const URL = process.env.NEOCIV_URL ?? "http://127.0.0.1:8000/";
const failures = [];
const results = [];

function check(name, ok, detail) {
  results.push(`  ${ok ? "pass" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
  if (!ok) failures.push(name);
}

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
await page.evaluate(() => window["__director"].disable());
await page.click("#flyDensest");
await page.waitForTimeout(1200);
const picking = await page.evaluate(() => {
  const view = window["__world"];
  view.settle();
  const rect = view.renderer.domElement.getBoundingClientRect();
  const matrix = view.scratch.matrix;

  const positions = new Map();
  for (const entry of view.agentMeshes.values()) {
    for (let i = 0; i < entry.count; i++) {
      entry.body.getMatrixAt(i, matrix);
      positions.set(entry.ids[i], {
        x: matrix.elements[12], y: matrix.elements[13], z: matrix.elements[14],
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
      if (other && range(other) <= range(q) + 1e-6) nearerWon++;
      else fartherWon++;
    }
  }
  return { onScreen, exact, nearerWon, fartherWon, missed };
});
// Aiming at an agent may legitimately return a different one when somebody stands in
// front — that is what picking means. What must never happen is returning an agent that is
// *further away* than the one aimed at, which is the signature of a broken instanceId to
// agent-id mapping and would hide behind a plain "mostly correct" assertion.
//
// Misses are environmental, not a fault: the world ticks once a second while this loop
// projects and raycasts forty-odd agents, so some of them genuinely walk out from under the
// cursor mid-loop. The invariant that actually detects a broken instanceId mapping is
// fartherWon === 0, which is asserted exactly; the miss budget is loose on purpose so this
// check fails for real reasons rather than for the sim being alive.
check(
  "clicking an agent never resolves to one behind it",
  picking.fartherWon === 0 &&
    picking.onScreen > 5 &&
    picking.missed <= Math.max(3, picking.onScreen * 0.25),
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
check(
  "the camera stays put while you hold it",
  !moved(seized, stillHeld),
  "a drifting camera after letting go means the goal was not pinned"
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

check("no console errors from our code", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));

await browser.close();

console.log(results.join("\n"));
if (failures.length) {
  console.log(`\n${failures.length} failed: ${failures.join(", ")}`);
  process.exit(1);
}
console.log(`\nall ${results.length} checks passed`);
