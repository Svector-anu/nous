// Adversarial lifecycle check for the 3d focus view.
//
// The python suite cannot see a gpu, so the things that actually go wrong in a webgl
// view — leaked materials, orphaned contexts, a camera that goes NaN when its container
// is collapsed — are invisible to it. This drives a real browser against a running
// server and asserts on renderer.info rather than on how the picture looks.
//
//   1. start the sim:  .venv/bin/python -m uvicorn src.api.server:app --factory
//   2. one-time setup: npm i playwright   (dev-only; never a project dependency)
//   3. node scripts/verify_focus3d.mjs
//
// Exits non-zero on the first failure so it can gate a commit.

import { chromium } from "playwright";

const URL = process.env.NEOCIV_URL ?? "http://127.0.0.1:8000/";
const failures = [];
const results = [];

function check(name, ok, detail) {
  results.push(`  ${ok ? "pass" : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
  if (!ok) failures.push(name);
}

const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));

await page.goto(URL, { waitUntil: "networkidle" });
await page.waitForFunction(() => document.getElementById("status").textContent.trim() === "live", { timeout: 20000 });

const info = () => page.evaluate(() => {
  const view = window["__focus"];
  if (!view.renderer) return { open: false, geometries: 0, textures: 0, disposables: 0 };
  const memory = view.renderer.info.memory;
  return {
    open: true,
    geometries: memory.geometries,
    textures: memory.textures,
    programs: view.renderer.info.programs.length,
    calls: view.renderer.info.render.calls,
    disposables: view.disposables.length,
    agents: view.agentMeshes.size,
    clanMaterials: view.clanMaterials.size,
  };
});

// --- 1. nothing accumulates while the view is open --------------------------
await page.click("#focusCluster");
await page.waitForTimeout(1500);
const openState = await info();
await page.evaluate(async () => {
  const snapshot = await (await fetch("/state")).json();
  for (let i = 0; i < 40; i++) window["__focus"].update(snapshot);
});
const ticked = await info();
check(
  "no geometry growth across 40 ticks",
  ticked.geometries === openState.geometries,
  `${openState.geometries} -> ${ticked.geometries}`
);
check(
  "no material growth across 40 ticks",
  ticked.disposables === openState.disposables,
  `${openState.disposables} -> ${ticked.disposables}`
);
check("draw calls stay modest", ticked.calls < 60, `${ticked.calls} calls`);

// --- 2. open/close is symmetric ---------------------------------------------
await page.keyboard.press("Escape");
await page.waitForTimeout(300);
const afterFirstClose = await info();
check(
  "close frees everything",
  afterFirstClose.geometries === 0 && afterFirstClose.textures === 0,
  `${afterFirstClose.geometries} geometries, ${afterFirstClose.textures} textures`
);

for (let i = 0; i < 10; i++) {
  await page.click("#focusCluster");
  await page.waitForTimeout(200);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(120);
}
const afterCycles = await info();
check(
  "10 open/close cycles leak nothing",
  afterCycles.geometries === 0 && afterCycles.textures === 0,
  `${afterCycles.geometries} geometries, ${afterCycles.textures} textures`
);

// Browsers cap live webgl contexts (~16). Without forceContextLoss the 10 cycles above
// would have exhausted them and the next open would silently render nothing.
await page.click("#focusCluster");
await page.waitForTimeout(800);
const reopened = await info();
check("still renders after many cycles", reopened.open && reopened.geometries > 0, `${reopened.geometries} geometries`);

// --- 3. focusing each clan ---------------------------------------------------
const clanReport = await page.evaluate(async () => {
  const snapshot = await (await fetch("/state")).json();
  const out = [];
  for (const clan of snapshot.clans.slice(0, 8)) {
    if (!clan.centre) continue;
    window["__focus"].open(snapshot, clan.centre, 12, `clan ${clan.id}`);
    const orbit = window["__focus"].orbit;
    out.push({
      id: clan.id,
      huts: window["__focus"].stats.huts,
      distance: orbit.distance,
      finite: Number.isFinite(orbit.distance) && Number.isFinite(orbit.theta) && Number.isFinite(orbit.phi),
    });
  }
  window["__focus"].close();
  return out;
});
check(
  "every clan frames to a finite camera",
  clanReport.length > 0 && clanReport.every((c) => c.finite && c.distance > 0),
  `${clanReport.length} clans, distances ${clanReport.map((c) => c.distance.toFixed(0)).join("/")}`
);

// --- 4. camera edge cases ----------------------------------------------------
const edges = await page.evaluate(async () => {
  const snapshot = await (await fetch("/state")).json();
  const view = window["__focus"];

  // an empty patch of map: no huts, no agents, nothing to frame
  view.open(snapshot, [0, 0], 1, "empty");
  const empty = {
    finite: Number.isFinite(view.orbit.distance) && view.orbit.distance > 0,
    huts: view.stats.huts,
  };
  view.close();

  // a collapsed container must not produce a NaN aspect ratio
  view.open(snapshot, [30, 39], 12, "collapsed");
  const host = view.container;
  const previous = host.style.height;
  host.style.height = "0px";
  view.resize();
  const collapsed = Number.isFinite(view.camera.aspect) && view.camera.aspect > 0;
  host.style.height = previous;
  view.resize();

  // Zoom and orbit through the real event handlers. Writing view.orbit directly would
  // sail straight past the clamps and prove nothing — the clamps live in the handlers.
  const canvas = view.renderer.domElement;
  const wheel = (deltaY) => canvas.dispatchEvent(new WheelEvent("wheel", { deltaY, cancelable: true }));
  for (let i = 0; i < 400; i++) wheel(-400);
  const zoomedIn = view.orbit.distance;
  for (let i = 0; i < 400; i++) wheel(400);
  const zoomedOut = view.orbit.distance;

  // Drag far past the poles; phi must stay off both of them or the view rolls over.
  const drag = (dy) => {
    canvas.dispatchEvent(new PointerEvent("pointerdown", { pointerId: 1, clientX: 0, clientY: 0, bubbles: true }));
    canvas.dispatchEvent(new PointerEvent("pointermove", { pointerId: 1, clientX: 0, clientY: dy, bubbles: true }));
    canvas.dispatchEvent(new PointerEvent("pointerup", { pointerId: 1, clientX: 0, clientY: dy, bubbles: true }));
  };
  drag(-100000);
  const phiHigh = view.orbit.phi;
  drag(100000);
  const phiLow = view.orbit.phi;

  // A cancelled pointer (interrupted touch) must not leave the drag stuck on.
  canvas.dispatchEvent(new PointerEvent("pointerdown", { pointerId: 2, clientX: 0, clientY: 0, bubbles: true }));
  canvas.dispatchEvent(new PointerEvent("pointercancel", { pointerId: 2, bubbles: true }));
  const beforeStray = view.orbit.theta;
  canvas.dispatchEvent(new PointerEvent("pointermove", { pointerId: 2, clientX: 500, clientY: 0, bubbles: true }));
  const dragStuck = view.orbit.theta !== beforeStray;

  view.close();

  return { empty, collapsed, zoomedIn, zoomedOut, phiHigh, phiLow, dragStuck };
});
check("empty area still frames", edges.empty.finite, `${edges.empty.huts} huts`);
check("collapsed container keeps a finite aspect", edges.collapsed);
check(
  "wheel zoom clamps at both stops",
  edges.zoomedIn >= 1 && edges.zoomedOut <= 24 * 4 + 1,
  `${edges.zoomedIn.toFixed(2)} .. ${edges.zoomedOut.toFixed(1)}`
);
check(
  "orbit never reaches either pole",
  edges.phiHigh > 0.1 && edges.phiLow < Math.PI / 2,
  `phi ${edges.phiHigh.toFixed(3)} .. ${edges.phiLow.toFixed(3)}`
);
check("a cancelled pointer does not leave the drag stuck", !edges.dragStuck);

// --- 5. the main canvas survives ---------------------------------------------
// The sections above drive FocusView directly, which leaves the overlay element open
// even though the view is closed. Put the ui back in sync before clicking again, or the
// overlay sits on top of the sidebar and swallows the click.
await page.keyboard.press("Escape");
await page.waitForTimeout(250);
await page.click("#focusCluster");
await page.waitForTimeout(300);
await page.keyboard.press("Escape");
await page.waitForTimeout(600);
const mapAlive = await page.evaluate(() => {
  const canvas = document.getElementById("map");
  const context = canvas.getContext("2d");
  const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
  let lit = 0;
  for (let i = 0; i < data.length; i += 4 * 512) if (data[i] + data[i + 1] + data[i + 2] > 40) lit++;
  return lit;
});
check("2d map still draws after using the 3d view", mapAlive > 0, `${mapAlive} lit samples`);

check("no console errors from our code", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));

await browser.close();

console.log(results.join("\n"));
if (failures.length) {
  console.log(`\n${failures.length} failed: ${failures.join(", ")}`);
  process.exit(1);
}
console.log(`\nall ${results.length} checks passed`);
