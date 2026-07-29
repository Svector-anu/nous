// Selective 3D focus view.
//
// The whole world stays on the cheap canvas. This opens on demand over a *local square*
// of it — a clan's centre, or a cluster of huts — and only that square gets meshes.
//
// Materials follow the Claude-of-Duty rules rather than its full GPU forge: zero art
// assets, everything generated at init, nothing allocated per frame. The pipeline is
// height-first — fbm value noise builds a height field, albedo and roughness are read
// off that height, and the normal map is a Sobel derivative of it. That ordering is the
// point: it makes plank gaps and shingle edges line up with their own shading instead of
// merely looking painted.
//
// Everything that can be shared is built once per open and reused: one unit box carries
// every wall, roof and plinth through InstancedMesh, and agents draw from one capsule and
// one material per clan. The per-tick path allocates nothing in the steady state — an
// earlier version built a material per agent per tick and only freed them on close, which
// measured at +27 live materials every second.
//
// It reads the same websocket snapshot the canvas does, so it is live for free, and it
// disposes every geometry, material, texture and the renderer itself on close.

import * as THREE from "/static/vendor/three.module.min.js";

const TILE = 2; // world metres per simulation tile
const TEXTURE_SIZE = 128;

// --- noise ------------------------------------------------------------------

function hash2(x, y, seed) {
  const h = Math.sin(x * 127.1 + y * 311.7 + seed * 74.7) * 43758.5453;
  return h - Math.floor(h);
}

function smooth(t) {
  // smoothstep keeps the lattice from showing as diamonds
  return t * t * (3 - 2 * t);
}

// --- periodic noise, for anything that becomes a tiling texture --------------
//
// The lattice wraps, so u=0 and u=1 land on the same lattice point and a texture built
// from this has no seam. This is the technique the claude-of-duty reference calls out
// ("periodic noise so everything tiles seamlessly") and it is not optional: with plain
// non-wrapping noise the plaster albedo jumped 4x its interior gradient at the tile
// boundary, which is a visible grid line repeated across every wall and the whole ground.
//
// cellsX/cellsY are lattice counts across the unit square and must be integers.

function periodicNoise(u, v, seed, cellsX, cellsY) {
  const x = u * cellsX;
  const y = v * cellsY;
  const xi = Math.floor(x);
  const yi = Math.floor(y);
  const sx = smooth(x - xi);
  const sy = smooth(y - yi);
  const wrapX = (i) => ((i % cellsX) + cellsX) % cellsX;
  const wrapY = (i) => ((i % cellsY) + cellsY) % cellsY;
  const x0 = wrapX(xi);
  const x1 = wrapX(xi + 1);
  const y0 = wrapY(yi);
  const y1 = wrapY(yi + 1);
  const a = hash2(x0, y0, seed);
  const b = hash2(x1, y0, seed);
  const c = hash2(x0, y1, seed);
  const d = hash2(x1, y1, seed);
  return (a * (1 - sx) + b * sx) * (1 - sy) + (c * (1 - sx) + d * sx) * sy;
}

// Each octave doubles the lattice count, so every octave keeps the same period and the
// sum still tiles. Anisotropic counts let a surface be fine in one axis and coarse in the
// other — wood grain runs along the plank.
function fbm(u, v, seed, octaves = 4, cellsX = 4, cellsY = cellsX) {
  let total = 0;
  let amplitude = 1;
  let norm = 0;
  let cx = cellsX;
  let cy = cellsY;
  for (let i = 0; i < octaves; i++) {
    total += periodicNoise(u, v, seed + i, cx, cy) * amplitude;
    norm += amplitude;
    amplitude *= 0.5;
    cx *= 2;
    cy *= 2;
  }
  return total / norm;
}

// --- open noise, for terrain -------------------------------------------------
//
// Terrain is sampled by world position over one finite plane and is never tiled, so it
// wants the opposite property: no period at all, or the landscape would visibly repeat.

function openNoise(x, y, seed) {
  const xi = Math.floor(x);
  const yi = Math.floor(y);
  const sx = smooth(x - xi);
  const sy = smooth(y - yi);
  const a = hash2(xi, yi, seed);
  const b = hash2(xi + 1, yi, seed);
  const c = hash2(xi, yi + 1, seed);
  const d = hash2(xi + 1, yi + 1, seed);
  return (a * (1 - sx) + b * sx) * (1 - sy) + (c * (1 - sx) + d * sx) * sy;
}

function openFbm(x, y, seed, octaves = 4) {
  let total = 0;
  let amplitude = 1;
  let frequency = 1;
  let norm = 0;
  for (let i = 0; i < octaves; i++) {
    total += openNoise(x * frequency, y * frequency, seed + i) * amplitude;
    norm += amplitude;
    amplitude *= 0.5;
    frequency *= 2;
  }
  return total / norm;
}

// Ground is not flat. The same function drives the displaced mesh and the height every
// hut, tree and agent is placed at, so nothing floats or sinks as the camera moves.
// Deliberately gentle: huts have square footprints and would gape on a real slope.
export function terrainHeight(x, z) {
  const roll = (openFbm(x * 0.028, z * 0.028, 91, 4) - 0.5) * 4.4;
  const bump = (openFbm(x * 0.10, z * 0.10, 97, 3) - 0.5) * 0.7;
  return roll + bump;
}

// --- procedural materials ---------------------------------------------------

const SURFACES = {
  wood: {
    seed: 11,
    tint: [0.42, 0.28, 0.16],
    height(u, v, seed) {
      const planks = 6;
      const plank = Math.floor(v * planks);
      const gap = Math.abs((v * planks) % 1 - 0.5) > 0.46 ? 0.3 : 1;
      // Grain runs along the plank: fine across u, coarse across v. The plank index goes
      // into the seed rather than into the coordinate, so each board gets its own grain
      // without pushing the sample off the wrapping lattice.
      const grain = fbm(u, v, seed + plank * 7, 3, 40, 4);
      // knots: occasional tight whorls that break the stripe rhythm
      const knot = fbm(u, v, seed + 13, 2, 7);
      const whorl = knot > 0.78 ? 0.55 + Math.sin(knot * 90) * 0.12 : 1;
      return gap * whorl * (0.58 + grain * 0.42);
    },
  },
  plaster: {
    seed: 23,
    tint: [0.80, 0.76, 0.68],
    height(u, v, seed) {
      const coarse = fbm(u, v, seed, 4, 6);
      const pit = fbm(u, v, seed + 5, 2, 30);
      // Trowel sweep: broad banding from being applied by hand. The sine has to complete
      // a whole number of cycles across u (hence the 2π·3) or it reintroduces the seam
      // the periodic noise just removed.
      const trowel = Math.sin(u * Math.PI * 2 * 3 + fbm(u, v, seed + 2, 2, 3) * 5) * 0.05;
      // patches where the render has flaked back to something browner underneath
      const flake = fbm(u, v, seed + 21, 3, 5);
      const spall = flake > 0.72 ? 0.72 : 1;
      return spall * (0.74 + coarse * 0.2 + trowel - (pit > 0.82 ? 0.32 : 0));
    },
  },
  stone: {
    seed: 67,
    tint: [0.50, 0.49, 0.46],
    height(u, v, seed) {
      // rough coursed rubble: rows of blocks, offset course to course, with deep joints
      const courses = 7;
      const blocks = 5;
      const course = Math.floor(v * courses);
      // A direct lattice hash per course, not a noise sample at a scaled coordinate —
      // the latter cannot wrap because its argument is not on the unit square.
      const offset = (course % 2) * 0.5 + hash2(0, course, seed + 4) * 0.3;
      const withinRow = (u * blocks + offset) % 1;
      const rowPos = (v * courses) % 1;
      const joint = withinRow > 0.9 || withinRow < 0.06 || rowPos > 0.9 || rowPos < 0.08;
      const face = fbm(u, v, seed, 3, 26);
      // The block index must wrap too: unwrapped it reads 5 at the right edge and 0 at
      // the left, so every block picked a different stone across the seam.
      const index = Math.floor(u * blocks + offset) % blocks;
      const block = hash2(index, course, seed + 8);
      return joint ? 0.28 + face * 0.1 : 0.68 + block * 0.22 + face * 0.16;
    },
  },
  grass: {
    seed: 53,
    tint: [0.31, 0.40, 0.19],
    height(u, v, seed) {
      const clump = fbm(u, v, seed, 4, 10);
      const blade = fbm(u, v, seed + 3, 2, 70);
      const worn = fbm(u, v, seed + 9, 3, 3);
      // bare earth showing through where the turf is thin. Kept subtle: a strong patch
      // here becomes an obvious repeating blotch once the texture tiles across the plot.
      return 0.45 + clump * 0.32 + blade * 0.23 - (worn > 0.82 ? 0.1 : 0);
    },
    // grass reads better with the tint shifting toward earth in the worn patches
    tintAt(h, base) {
      const dry = Math.max(0, 0.5 - h) * 1.6;
      return [base[0] + dry * 0.22, base[1] + dry * 0.06, base[2] + dry * 0.02];
    },
  },
  roof: {
    seed: 37,
    tint: [0.36, 0.17, 0.13],
    height(u, v, seed) {
      const rows = 10;
      const across = 8;
      const row = Math.floor(v * rows);
      const stagger = row % 2 === 0 ? 0 : 0.5;
      const withinRow = (u * across + stagger) % 1;
      const edge = withinRow > 0.9 || (v * rows) % 1 > 0.88 ? 0.42 : 1;
      // each tile sits slightly differently, so the courses are not a printed grid.
      // Index wraps for the same reason the stone block index does.
      const tile = hash2(Math.floor(u * across + stagger) % across, row, seed + 6);
      return edge * (0.66 + tile * 0.2 + fbm(u, v, seed, 2, 20) * 0.24);
    },
    tintAt(h, base) {
      // weathering: the lower, wetter parts of a tile go greener
      const damp = Math.max(0, 0.75 - h);
      return [base[0] - damp * 0.06, base[1] + damp * 0.09, base[2] + damp * 0.02];
    },
  },
};

export function buildSurface(name) {
  const spec = SURFACES[name];
  const size = TEXTURE_SIZE;
  const heights = new Float32Array(size * size);

  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      heights[y * size + x] = spec.height(x / size, y / size, spec.seed);
    }
  }

  const albedo = new Uint8Array(size * size * 4);
  const normal = new Uint8Array(size * size * 4);
  const rough = new Uint8Array(size * size * 4);
  const at = (x, y) => heights[((y + size) % size) * size + ((x + size) % size)];

  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      const h = heights[y * size + x];

      // albedo: the tint darkened where the surface sits lower, plus any per-surface
      // recolouring (weathered roof tiles, dry earth under thin grass)
      const shade = 0.55 + h * 0.45;
      const tint = spec.tintAt ? spec.tintAt(h, spec.tint) : spec.tint;
      albedo[i] = Math.min(255, Math.max(0, tint[0] * shade * 255));
      albedo[i + 1] = Math.min(255, Math.max(0, tint[1] * shade * 255));
      albedo[i + 2] = Math.min(255, Math.max(0, tint[2] * shade * 255));
      albedo[i + 3] = 255;

      // roughness: recesses hold dirt and scatter more
      const r = Math.min(255, (1.05 - h * 0.5) * 255);
      rough[i] = rough[i + 1] = rough[i + 2] = r;
      rough[i + 3] = 255;

      // normal: sobel over the height field
      const dx =
        at(x - 1, y - 1) + 2 * at(x - 1, y) + at(x - 1, y + 1) -
        (at(x + 1, y - 1) + 2 * at(x + 1, y) + at(x + 1, y + 1));
      const dy =
        at(x - 1, y - 1) + 2 * at(x, y - 1) + at(x + 1, y - 1) -
        (at(x - 1, y + 1) + 2 * at(x, y + 1) + at(x + 1, y + 1));
      const strength = 2.5;
      const nx = dx * strength;
      const ny = dy * strength;
      const nz = 1;
      const len = Math.hypot(nx, ny, nz);
      normal[i] = ((nx / len) * 0.5 + 0.5) * 255;
      normal[i + 1] = ((ny / len) * 0.5 + 0.5) * 255;
      normal[i + 2] = ((nz / len) * 0.5 + 0.5) * 255;
      normal[i + 3] = 255;
    }
  }

  const make = (data, srgb) => {
    const texture = new THREE.DataTexture(data, size, size, THREE.RGBAFormat);
    texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
    if (srgb) texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = 4;
    texture.needsUpdate = true;
    return texture;
  };

  return new THREE.MeshStandardMaterial({
    map: make(albedo, true),
    normalMap: make(normal, false),
    roughnessMap: make(rough, false),
    normalScale: new THREE.Vector2(1.2, 1.2),
    metalness: 0,
  });
}

// --- hut kit ----------------------------------------------------------------
//
// Every box in a hut is the same unit cube; its real size lives in the instance matrix.
// That means one geometry and four draw calls for a whole settlement instead of seven
// meshes per hut (651 draw calls at the density we actually hit).

const HUT = (() => {
  const w = TILE * 0.82;
  const h = TILE * 0.75;
  const t = TILE * 0.1;
  const door = w * 0.34;
  const jamb = (w - door) / 2;
  const slope = TILE * 0.62;
  const plinth = TILE * 0.34;

  const piece = (scale, position, rotationX = 0) => ({ scale, position, rotationX });

  return {
    w, h, height: h,
    plaster: [
      piece([w, h, t], [0, h / 2, -w / 2]),                          // back
      piece([t, h, w], [-w / 2, h / 2, 0]),                          // left
      piece([t, h, w], [w / 2, h / 2, 0]),                           // right
      piece([jamb, h, t], [-(door + jamb) / 2, h / 2, w / 2]),       // front, door left
      piece([jamb, h, t], [(door + jamb) / 2, h / 2, w / 2]),        // front, door right
    ],
    wood: [
      piece([door, h * 0.22, t], [0, h - h * 0.11, w / 2]),          // lintel
    ],
    // The plinth sinks below the surface, so a hut on a slope beds into the ground
    // instead of hanging off it.
    stone: [
      piece([w * 1.12, plinth, w * 1.12], [0, -plinth * 0.34, 0]),
    ],
    roof: [-1, 1].map((side) =>
      piece([w * 1.16, t * 1.4, slope * 1.25], [0, h + slope * 0.28, side * slope * 0.42], side * 0.62)
    ),
  };
})();

// Sky and fog share this colour at the horizon so the ground dissolves rather than
// stopping at a visible line.
const HORIZON = 0x9fb2c4;
const ZENITH = 0x2d4a72;

// --- the view ---------------------------------------------------------------

export class FocusView {
  constructor(container) {
    this.container = container;
    this.active = false;
    this.disposables = [];
    this.onClose = null;
  }

  open(snapshot, centre, radius, label) {
    if (this.active) this.close();
    this.active = true;
    this.centre = centre;
    this.radius = radius;
    this.label = label;
    this.staticKey = null;
    this.agentMeshes = new Map();
    this.clanMaterials = new Map();
    // Reused every tick so the per-frame path allocates nothing.
    this.scratch = {
      matrix: new THREE.Matrix4(),
      position: new THREE.Vector3(),
      quaternion: new THREE.Quaternion(),
      scale: new THREE.Vector3(1, 1, 1),
      euler: new THREE.Euler(),
    };

    const { width, height } = this._size();

    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(width, height);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    // Without tone mapping the sun clips to white and everything below it reads muddy,
    // which is what made the first render look like a night scene.
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.35;
    this.container.appendChild(this.renderer.domElement);

    const span = this.radius * TILE;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(HORIZON);
    // Fog is the horizon colour rather than the page's, so the ground dissolves into the
    // sky instead of into a dark band the eye reads as an edge. Range is set properly in
    // _frameCamera once the camera distance is known; the plot size alone cannot say
    // where the ground edge will actually fall on screen.
    this.scene.fog = new THREE.Fog(HORIZON, span, span * 3);

    this.camera = new THREE.PerspectiveCamera(50, width / height, 0.1, span * 12);

    this.materials = {
      wood: buildSurface("wood"),
      plaster: buildSurface("plaster"),
      stone: buildSurface("stone"),
      roof: buildSurface("roof"),
    };
    for (const material of Object.values(this.materials)) {
      this._own(material, material.map, material.normalMap, material.roughnessMap);
    }

    // One cube and one capsule serve every instance in the scene.
    this.geometries = {
      box: new THREE.BoxGeometry(1, 1, 1),
      body: new THREE.CapsuleGeometry(TILE * 0.12, TILE * 0.3, 4, 10),
      head: new THREE.SphereGeometry(TILE * 0.105, 10, 8),
      ring: new THREE.TorusGeometry(TILE * 0.28, TILE * 0.035, 8, 20),
      food: new THREE.IcosahedronGeometry(TILE * 0.17, 0),
      trunk: new THREE.CylinderGeometry(TILE * 0.07, TILE * 0.1, TILE * 0.8, 6),
      canopy: new THREE.IcosahedronGeometry(TILE * 0.3, 0),
    };
    this._own(...Object.values(this.geometries));

    this.sharedMaterials = {
      food: new THREE.MeshStandardMaterial({ color: 0x3f8f42, roughness: 0.75 }),
      canopy: new THREE.MeshStandardMaterial({ color: 0x2c6b32, roughness: 0.9, flatShading: true }),
      ring: new THREE.MeshStandardMaterial({ color: 0xf0f6fc, roughness: 0.35, emissive: 0x1d3050 }),
      head: new THREE.MeshStandardMaterial({ color: 0xd9c1a3, roughness: 0.7 }),
    };
    this._own(...Object.values(this.sharedMaterials));

    this.scene.add(this._sky());
    this._light();
    this._ground();
    this._controls();
    this.update(snapshot);
    this._frameCamera();
    this._frame();
  }

  _own(...items) {
    for (const item of items) if (item) this.disposables.push(item);
  }

  // A dome rather than a background texture. `scene.background` stretches across the
  // *viewport*, which puts its horizon colour at the bottom of the screen instead of at
  // the actual horizon — the ground then met a mismatched sky and the plane's far edge
  // showed as a hard line. Grading by world direction instead means the horizon colour
  // lands exactly where the ground meets it, at any camera angle, and the fog (same
  // colour) dissolves one into the other.
  _sky() {
    const radius = this.radius * TILE * 8;
    const geometry = new THREE.SphereGeometry(radius, 32, 20);
    const zenith = new THREE.Color(ZENITH);
    const horizon = new THREE.Color(HORIZON);
    const colour = new THREE.Color();
    const position = geometry.attributes.position;
    const colours = new Float32Array(position.count * 3);
    for (let i = 0; i < position.count; i++) {
      const t = Math.max(0, Math.min(1, position.getY(i) / radius));
      colour.copy(horizon).lerp(zenith, Math.sqrt(t));
      colours[i * 3] = colour.r;
      colours[i * 3 + 1] = colour.g;
      colours[i * 3 + 2] = colour.b;
    }
    geometry.setAttribute("color", new THREE.BufferAttribute(colours, 3));

    const material = new THREE.MeshBasicMaterial({
      vertexColors: true,
      side: THREE.BackSide,
      fog: false,
      depthWrite: false,
    });
    this._own(geometry, material);
    const dome = new THREE.Mesh(geometry, material);
    dome.renderOrder = -1;
    return dome;
  }

  _size() {
    // A hidden or collapsed container reports 0, which would make the aspect ratio
    // NaN and blank the view. Fall back to the viewport instead.
    const width = this.container.clientWidth || window.innerWidth || 1;
    const height = this.container.clientHeight || window.innerHeight || 1;
    return { width, height };
  }

  _light() {
    this.scene.add(new THREE.HemisphereLight(0xbcd4ec, 0x4a3d2a, 1.6));
    const sun = new THREE.DirectionalLight(0xffe8c0, 3.0);
    const span = this.radius * TILE;
    sun.position.set(span * 0.7, span * 1.1, span * 0.5);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    sun.shadow.bias = -0.0006;
    sun.shadow.normalBias = 0.02;
    const extent = span + TILE * 4;
    Object.assign(sun.shadow.camera, {
      left: -extent, right: extent, top: extent, bottom: -extent,
      near: 1, far: span * 4,
    });
    sun.shadow.camera.updateProjectionMatrix();
    this.scene.add(sun);
    this.scene.add(sun.target);
  }

  _ground() {
    const span = (this.radius * 2 + 1) * TILE;
    // Generous subdivision: the displacement is what stops this reading as a flat card,
    // and at this size the vertex count is trivial next to the buildings.
    // Extends well past the focus square so its edge falls beyond the fog's far plane
    // and is never seen — the settlement sits in open country, not on a floating tile.
    const segments = Math.min(180, (this.radius * 2 + 1) * 5);
    // Far larger than the fog can see. The outer region is tapered flat, so the extra
    // area costs nothing in detail and guarantees the plane's own edge is never reached.
    this.groundHalf = span * 6;
    const geometry = new THREE.PlaneGeometry(this.groundHalf * 2, this.groundHalf * 2, segments, segments);
    geometry.rotateX(-Math.PI / 2);

    // Relief fades out with distance. Left un-tapered, the far *corners* of the square
    // plane ride up over the skyline as two hard triangles — the diagonal reaches 1.4x
    // further than the edges. Beyond `outer` the ground is flat and the fog takes it.
    // Everything the simulation can place sits inside `inner` for any radius
    // (radius*TILE < 0.7*(2*radius+1)*TILE), so no hut is ever on tapered ground.
    const inner = span * 0.7;
    const outer = span * 1.15;
    const position = geometry.attributes.position;
    for (let i = 0; i < position.count; i++) {
      const x = position.getX(i);
      const z = position.getZ(i);
      const t = Math.max(0, Math.min(1, (Math.hypot(x, z) - inner) / (outer - inner)));
      const falloff = 1 - t * t * (3 - 2 * t);
      position.setY(i, terrainHeight(x, z) * falloff);
    }
    position.needsUpdate = true;
    geometry.computeVertexNormals();

    const material = buildSurface("grass");
    // One texture tile per two world tiles. This has to be derived from the plane's own
    // size, not the focus square — the plane is much larger than the plot, and scaling
    // this to the plot stretched every blade across 48 metres.
    const repeat = (this.groundHalf * 2) / (TILE * 2);
    for (const map of [material.map, material.normalMap, material.roughnessMap]) {
      map.repeat.set(repeat, repeat);
    }
    this._own(geometry, material, material.map, material.normalMap, material.roughnessMap);

    const ground = new THREE.Mesh(geometry, material);
    ground.receiveShadow = true;
    this.scene.add(ground);
  }

  // Orbit + pan + zoom, hand-rolled: about seventy lines against another vendored file,
  // and it keeps the dependency surface to three.js alone.
  _controls() {
    const canvas = this.renderer.domElement;
    this.target = new THREE.Vector3(0, 0, 0);

    let dragging = null;
    let lastX = 0;
    let lastY = 0;

    const down = (event) => {
      dragging = event.button === 2 || event.shiftKey ? "pan" : "orbit";
      lastX = event.clientX;
      lastY = event.clientY;
      // Throws outright if the id has no active pointer — which happens with synthetic
      // events and with a pointer released between dispatch and handling. Capture is an
      // optimisation for dragging outside the canvas, never worth taking the view down.
      try {
        canvas.setPointerCapture(event.pointerId);
      } catch {
        /* dragging still works, it just stops at the canvas edge */
      }
    };
    const move = (event) => {
      if (!dragging) return;
      const dx = event.clientX - lastX;
      const dy = event.clientY - lastY;
      lastX = event.clientX;
      lastY = event.clientY;
      if (dragging === "orbit") {
        this.orbit.theta -= dx * 0.006;
        // Stop just shy of the poles: at exactly vertical the lookAt up-vector flips
        // and the scene spins on its own axis.
        this.orbit.phi = Math.max(0.12, Math.min(Math.PI / 2 - 0.04, this.orbit.phi - dy * 0.006));
      } else {
        const scale = this.orbit.distance * 0.0016;
        const forward = new THREE.Vector3(Math.sin(this.orbit.theta), 0, Math.cos(this.orbit.theta));
        const right = new THREE.Vector3(forward.z, 0, -forward.x);
        this.target.addScaledVector(right, -dx * scale);
        this.target.addScaledVector(forward, -dy * scale);
        this._clampTarget();
      }
    };
    const release = (event) => {
      dragging = null;
      try {
        if (event.pointerId !== undefined && canvas.hasPointerCapture(event.pointerId)) {
          canvas.releasePointerCapture(event.pointerId);
        }
      } catch {
        /* the capture is already gone, which is the state we wanted anyway */
      }
    };
    const wheel = (event) => {
      event.preventDefault();
      const min = TILE * 1.2;
      const max = this.radius * TILE * 4;
      const next = this.orbit.distance * (1 + event.deltaY * 0.0012);
      this.orbit.distance = Math.max(min, Math.min(max, next));
    };
    const menu = (event) => event.preventDefault();

    canvas.addEventListener("pointerdown", down);
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerup", release);
    // Without pointercancel the drag sticks on after a touch gesture is interrupted.
    canvas.addEventListener("pointercancel", release);
    canvas.addEventListener("pointerleave", release);
    canvas.addEventListener("wheel", wheel, { passive: false });
    canvas.addEventListener("contextmenu", menu);
    this._listeners = () => {
      canvas.removeEventListener("pointerdown", down);
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("pointerup", release);
      canvas.removeEventListener("pointercancel", release);
      canvas.removeEventListener("pointerleave", release);
      canvas.removeEventListener("wheel", wheel);
      canvas.removeEventListener("contextmenu", menu);
    };
  }

  _clampTarget() {
    // Panning past the edge of the plot loses the settlement off-screen with no way
    // back except reopening.
    const limit = (this.radius + 2) * TILE;
    this.target.x = Math.max(-limit, Math.min(limit, this.target.x));
    this.target.z = Math.max(-limit, Math.min(limit, this.target.z));
  }

  // Frame whatever is actually there, rather than assuming the plot is full. A clan of
  // three huts and a cluster of ninety should both fill the view on open.
  //
  // The fit is done by projecting the content's corners onto the camera's own right/up
  // axes. Fitting the raw width and depth instead is wrong for any camera that is not
  // overhead: a low camera foreshortens the depth axis, and the naive version put the
  // near row of huts off the bottom of the screen.
  _frameCamera() {
    const bounds = this.bounds;
    const theta = Math.PI * 0.22;
    const phi = Math.PI * 0.32;
    const fov = THREE.MathUtils.degToRad(this.camera.fov);
    const aspect = Math.max(0.4, this.camera.aspect);

    const minX = bounds ? bounds.minX : -this.radius * TILE;
    const maxX = bounds ? bounds.maxX : this.radius * TILE;
    const minZ = bounds ? bounds.minZ : -this.radius * TILE;
    const maxZ = bounds ? bounds.maxZ : this.radius * TILE;
    const centreX = (minX + maxX) / 2;
    const centreZ = (minZ + maxZ) / 2;
    const centreY = terrainHeight(centreX, centreZ) + HUT.height * 0.5;
    this.target.set(centreX, centreY, centreZ);

    const direction = new THREE.Vector3(
      Math.sin(phi) * Math.sin(theta),
      Math.cos(phi),
      Math.sin(phi) * Math.cos(theta)
    );
    const right = new THREE.Vector3().crossVectors(new THREE.Vector3(0, 1, 0), direction).normalize();
    const up = new THREE.Vector3().crossVectors(direction, right).normalize();

    // Vertical span allows for terrain relief under the huts and the roof ridge above.
    // Bounds come from hut centres, so pad by a hut's own footprint or the outermost
    // row hangs off the edge of the screen.
    const relief = 3.6;
    const pad = TILE;
    const corner = new THREE.Vector3();
    let spanRight = TILE;
    let spanUp = TILE;
    for (const x of [minX - pad, maxX + pad]) {
      for (const z of [minZ - pad, maxZ + pad]) {
        for (const y of [centreY - relief, centreY + relief]) {
          corner.set(x - centreX, y - centreY, z - centreZ);
          spanRight = Math.max(spanRight, Math.abs(corner.dot(right)));
          spanUp = Math.max(spanUp, Math.abs(corner.dot(up)));
        }
      }
    }

    const tanHalf = Math.tan(fov / 2);
    this.orbit = {
      theta,
      phi,
      distance: Math.max(spanUp / tanHalf, spanRight / (tanHalf * aspect)) * 1.18,
    };
    this.home = { ...this.orbit, target: this.target.clone() };

    // Fog has to be measured against where the ground actually *is*, not the plot size:
    // an earlier version put fog.near at 74 when the whole ground sat within 50 units of
    // the camera, so no fog was ever applied and the plane ended on a hard edge. The far
    // plane is pinned past the ground's own boundary so that boundary is always fully
    // dissolved before it can be seen.
    const eye = this.orbit.distance;
    this.scene.fog.near = eye + this.radius * TILE * 0.4;
    // Saturate at roughly twice the framed distance — well inside the ground's own
    // boundary, so the plane's square corner is fully dissolved long before it is
    // reached and never shows as a V against the sky.
    this.scene.fog.far = eye + this.radius * TILE * 2.4;
  }

  resetCamera() {
    if (!this.active || !this.home) return;
    this.orbit = { theta: this.home.theta, phi: this.home.phi, distance: this.home.distance };
    this.target.copy(this.home.target);
  }

  inFocus(item) {
    return (
      Math.abs(item.x - this.centre[0]) <= this.radius &&
      Math.abs(item.y - this.centre[1]) <= this.radius
    );
  }

  local(item) {
    // Simulation y maps to world z; centring keeps the camera maths simple.
    return [(item.x - this.centre[0]) * TILE, (item.y - this.centre[1]) * TILE];
  }

  update(snapshot) {
    if (!this.active || !snapshot) return;
    const buildings = snapshot.buildings.filter((b) => this.inFocus(b));
    const resources = snapshot.resources.filter((r) => this.inFocus(r) && r.amount > 0);
    const agents = snapshot.agents.filter((a) => this.inFocus(a));

    // Huts and trees only change when something is built or exhausted, which is rare
    // next to the tick rate. Rebuilding them every tick was throwing away and
    // reallocating 799 geometries a second.
    const key = `${buildings.map((b) => b.id).join(",")}|${resources.map((r) => r.id).join(",")}`;
    if (key !== this.staticKey) {
      this.staticKey = key;
      this._buildStatic(buildings, resources);
    }
    this._syncAgents(agents);

    this.bounds = this._bounds(buildings, agents);
    this.stats = { huts: buildings.length, agents: agents.length };
  }

  _bounds(buildings, agents) {
    const points = [...buildings, ...agents];
    if (points.length === 0) return null;
    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
    for (const point of points) {
      const [x, z] = this.local(point);
      minX = Math.min(minX, x); maxX = Math.max(maxX, x);
      minZ = Math.min(minZ, z); maxZ = Math.max(maxZ, z);
    }
    return { minX, maxX, minZ, maxZ };
  }

  _buildStatic(buildings, resources) {
    this._disposeStatic();
    const group = new THREE.Group();

    const matrix = new THREE.Matrix4();
    const hutMatrix = new THREE.Matrix4();
    const local = new THREE.Matrix4();
    const quaternion = new THREE.Quaternion();
    const euler = new THREE.Euler();
    const scale = new THREE.Vector3();
    const position = new THREE.Vector3();

    const instanced = (material, pieces, count, place) => {
      if (count === 0) return null;
      const mesh = new THREE.InstancedMesh(this.geometries.box, material, count * pieces.length);
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      mesh.frustumCulled = false;
      let index = 0;
      for (let i = 0; i < count; i++) {
        place(i, hutMatrix);
        for (const piece of pieces) {
          euler.set(piece.rotationX, 0, 0);
          quaternion.setFromEuler(euler);
          position.set(...piece.position);
          scale.set(...piece.scale);
          local.compose(position, quaternion, scale);
          matrix.multiplyMatrices(hutMatrix, local);
          mesh.setMatrixAt(index++, matrix);
        }
      }
      mesh.instanceMatrix.needsUpdate = true;
      group.add(mesh);
      return mesh;
    };

    const placeHut = (i, out) => {
      const building = buildings[i];
      const [x, z] = this.local(building);
      // A stable per-hut yaw so a cluster does not look stamped. Keep the multiply
      // inside 2^31 or the float loses the low bits that make it vary at all.
      const spin = (((building.id * 2654435761) % 2147483647) / 2147483647) * 0.6 - 0.3;
      euler.set(0, spin, 0);
      quaternion.setFromEuler(euler);
      position.set(x, terrainHeight(x, z), z);
      scale.set(1, 1, 1);
      out.compose(position, quaternion, scale);
    };

    instanced(this.materials.stone, HUT.stone, buildings.length, placeHut);
    instanced(this.materials.plaster, HUT.plaster, buildings.length, placeHut);
    instanced(this.materials.wood, HUT.wood, buildings.length, placeHut);
    instanced(this.materials.roof, HUT.roof, buildings.length, placeHut);

    const food = resources.filter((r) => r.kind === "FOOD");
    const wood = resources.filter((r) => r.kind !== "FOOD");

    const scatter = (list, geometry, material, lift, jitter) => {
      if (list.length === 0) return;
      const mesh = new THREE.InstancedMesh(geometry, material, list.length);
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      mesh.frustumCulled = false;
      for (let i = 0; i < list.length; i++) {
        const [x, z] = this.local(list[i]);
        const wobble = hash2(list[i].id, list[i].id * 3, 7);
        euler.set(0, wobble * Math.PI * 2, 0);
        quaternion.setFromEuler(euler);
        position.set(x, terrainHeight(x, z) + lift, z);
        const s = 1 + (wobble - 0.5) * jitter;
        scale.set(s, s, s);
        matrix.compose(position, quaternion, scale);
        mesh.setMatrixAt(i, matrix);
      }
      mesh.instanceMatrix.needsUpdate = true;
      group.add(mesh);
    };

    scatter(food, this.geometries.food, this.sharedMaterials.food, TILE * 0.17, 0.5);
    scatter(wood, this.geometries.trunk, this.materials.wood, TILE * 0.4, 0.35);
    scatter(wood, this.geometries.canopy, this.sharedMaterials.canopy, TILE * 0.95, 0.55);

    this.scene.add(group);
    this.built = group;
  }

  _clanMaterial(clanId) {
    const key = clanId === null || clanId === undefined ? -1 : clanId;
    let material = this.clanMaterials.get(key);
    if (!material) {
      material = new THREE.MeshStandardMaterial({
        color: new THREE.Color(key === -1 ? 0x8b949e : clanHue(key)),
        roughness: 0.55,
      });
      this.clanMaterials.set(key, material);
      this._own(material);
    }
    return material;
  }

  // Agents are instanced per clan rather than given a mesh each. One mesh per agent came
  // to two draw calls a head — 63 in a busy settlement, and growing with population.
  // Batched this way it is two per clan present, so the cost tracks the number of clans
  // in view (a handful) instead of the number of people.
  _syncAgents(agents) {
    const byClan = new Map();
    for (const agent of agents) {
      const key = agent.clan === null || agent.clan === undefined ? -1 : agent.clan;
      let list = byClan.get(key);
      if (!list) byClan.set(key, (list = []));
      list.push(agent);
    }

    for (const key of [...this.agentMeshes.keys()]) {
      if (!byClan.has(key)) this._disposeAgentGroup(key);
    }

    const { matrix, position, quaternion, scale, euler } = this.scratch;
    scale.set(1, 1, 1);

    for (const [key, list] of byClan) {
      let entry = this.agentMeshes.get(key);
      // An InstancedMesh has a fixed capacity, so a change in headcount means a new one.
      // Membership shifts far less often than position does.
      if (!entry || entry.count !== list.length) {
        if (entry) this._disposeAgentGroup(key);
        const body = new THREE.InstancedMesh(this.geometries.body, this._clanMaterial(key), list.length);
        const head = new THREE.InstancedMesh(this.geometries.head, this.sharedMaterials.head, list.length);
        for (const mesh of [body, head]) {
          mesh.castShadow = true;
          // Culling is computed from the source geometry, not the instances, so an
          // instanced batch can vanish while its members are plainly on screen.
          mesh.frustumCulled = false;
          this.scene.add(mesh);
        }
        entry = { body, head, count: list.length };
        this.agentMeshes.set(key, entry);
      }

      for (let i = 0; i < list.length; i++) {
        const agent = list[i];
        const [x, z] = this.local(agent);
        const ground = terrainHeight(x, z);

        let facing = 0;
        if (agent.target) {
          const [tx, tz] = this.local({ x: agent.target[0], y: agent.target[1] });
          if (tx !== x || tz !== z) facing = Math.atan2(tx - x, tz - z);
        }
        euler.set(0, facing, 0);
        quaternion.setFromEuler(euler);

        position.set(x, ground + TILE * 0.33, z);
        matrix.compose(position, quaternion, scale);
        entry.body.setMatrixAt(i, matrix);

        position.set(x, ground + TILE * 0.62, z);
        matrix.compose(position, quaternion, scale);
        entry.head.setMatrixAt(i, matrix);
      }
      entry.body.instanceMatrix.needsUpdate = true;
      entry.head.instanceMatrix.needsUpdate = true;
    }

    this._syncRings(agents.filter((agent) => agent.user));
  }

  // User-deployed agents wear a ring so they can be picked out of a crowd. One batch for
  // all of them regardless of clan.
  _syncRings(users) {
    if (this.rings && this.rings.count !== users.length) {
      this.scene.remove(this.rings);
      this.rings.dispose();
      this.rings = null;
    }
    if (users.length === 0) return;
    if (!this.rings) {
      this.rings = new THREE.InstancedMesh(this.geometries.ring, this.sharedMaterials.ring, users.length);
      this.rings.frustumCulled = false;
      this.scene.add(this.rings);
    }
    const { matrix, position, quaternion, scale, euler } = this.scratch;
    scale.set(1, 1, 1);
    euler.set(Math.PI / 2, 0, 0);
    quaternion.setFromEuler(euler);
    for (let i = 0; i < users.length; i++) {
      const [x, z] = this.local(users[i]);
      position.set(x, terrainHeight(x, z) + TILE * 0.04, z);
      matrix.compose(position, quaternion, scale);
      this.rings.setMatrixAt(i, matrix);
    }
    this.rings.instanceMatrix.needsUpdate = true;
  }

  _disposeAgentGroup(key) {
    const entry = this.agentMeshes.get(key);
    if (!entry) return;
    for (const mesh of [entry.body, entry.head]) {
      this.scene.remove(mesh);
      mesh.dispose();
    }
    this.agentMeshes.delete(key);
  }

  _disposeStatic() {
    if (!this.built) return;
    this.scene.remove(this.built);
    // Geometries and materials here are shared and outlive the group; only the
    // per-instance buffers belong to it.
    this.built.traverse((node) => {
      if (node.isInstancedMesh) node.dispose();
    });
    this.built = null;
  }

  _frame() {
    if (!this.active) return;
    this.frameHandle = requestAnimationFrame(() => this._frame());

    const { theta, phi, distance } = this.orbit;
    this.camera.position.set(
      this.target.x + distance * Math.sin(phi) * Math.sin(theta),
      this.target.y + distance * Math.cos(phi),
      this.target.z + distance * Math.sin(phi) * Math.cos(theta)
    );
    this.camera.lookAt(this.target);
    this.renderer.render(this.scene, this.camera);
  }

  resize() {
    if (!this.active) return;
    const { width, height } = this._size();
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height);
  }

  // Closing must actually free the gpu memory, not just hide the panel.
  close() {
    if (!this.active) return;
    this.active = false;
    cancelAnimationFrame(this.frameHandle);
    if (this._listeners) this._listeners();

    this._disposeStatic();
    for (const key of [...this.agentMeshes.keys()]) this._disposeAgentGroup(key);
    if (this.rings) {
      this.scene.remove(this.rings);
      this.rings.dispose();
      this.rings = null;
    }
    this.clanMaterials.clear();

    for (const item of this.disposables) {
      if (item && typeof item.dispose === "function") item.dispose();
    }
    this.disposables = [];

    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.renderer.domElement.remove();
    this.scene = null;
    this.renderer = null;
    this.camera = null;
    this.bounds = null;
    this.staticKey = null;
    if (this.onClose) this.onClose();
  }
}

export function clanHue(id) {
  // Same golden-angle spacing the 2d viewer uses, so a clan keeps its colour across views.
  const hue = ((id * 137.508) % 360) / 360;
  return new THREE.Color().setHSL(hue, 0.55, 0.55).getHex();
}

export function densestCluster(snapshot, radius) {
  // Where are the huts actually packed? Scores each hut by how many neighbours sit
  // inside a focus-sized square around it, so "focus the busiest place" needs no
  // simulation change to answer. Bucketed by radius so this stays linear-ish in the
  // number of huts rather than quadratic.
  const buildings = snapshot.buildings;
  if (buildings.length === 0) return null;

  const buckets = new Map();
  const keyOf = (x, y) => `${Math.floor(x / radius)},${Math.floor(y / radius)}`;
  for (const building of buildings) {
    const key = keyOf(building.x, building.y);
    let bucket = buckets.get(key);
    if (!bucket) buckets.set(key, (bucket = []));
    bucket.push(building);
  }

  let best = null;
  let bestCount = -1;
  for (const anchor of buildings) {
    const bx = Math.floor(anchor.x / radius);
    const by = Math.floor(anchor.y / radius);
    let count = 0;
    for (let gx = bx - 1; gx <= bx + 1; gx++) {
      for (let gy = by - 1; gy <= by + 1; gy++) {
        const bucket = buckets.get(`${gx},${gy}`);
        if (!bucket) continue;
        for (const other of bucket) {
          if (Math.abs(other.x - anchor.x) <= radius && Math.abs(other.y - anchor.y) <= radius) count++;
        }
      }
    }
    if (count > bestCount) {
      bestCount = count;
      best = anchor;
    }
  }
  return best ? { centre: [best.x, best.y], count: bestCount } : null;
}
