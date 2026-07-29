// Selective 3D focus view.
//
// The whole world stays on the cheap canvas. This opens on demand over a *local square*
// of it — a clan's centre, or a cluster of huts — and only that square gets meshes.
//
// Materials follow the Claude-of-Duty rules rather than its full GPU forge: zero art
// assets, everything generated at init, nothing allocated per frame. The pipeline is
// height-first — fbm value noise builds a height field, albedo and roughness are read
// off that height, and the normal map is a Sobel derivative of it. Three surfaces is
// enough to make a hut read as built rather than as boxes.
//
// It reads the same websocket snapshot the canvas does, so it is live for free, and it
// disposes every geometry, material, texture and the renderer itself on close.

import * as THREE from "/static/vendor/three.module.min.js";

const TILE = 2; // world metres per simulation tile
const TEXTURE_SIZE = 128;

// --- procedural materials ---------------------------------------------------

function hash2(x, y, seed) {
  let h = Math.sin(x * 127.1 + y * 311.7 + seed * 74.7) * 43758.5453;
  return h - Math.floor(h);
}

function valueNoise(x, y, seed) {
  const xi = Math.floor(x);
  const yi = Math.floor(y);
  const xf = x - xi;
  const yf = y - yi;
  // smoothstep keeps the lattice from showing as diamonds
  const u = xf * xf * (3 - 2 * xf);
  const v = yf * yf * (3 - 2 * yf);
  const a = hash2(xi, yi, seed);
  const b = hash2(xi + 1, yi, seed);
  const c = hash2(xi, yi + 1, seed);
  const d = hash2(xi + 1, yi + 1, seed);
  return (a * (1 - u) + b * u) * (1 - v) + (c * (1 - u) + d * u) * v;
}

function fbm(x, y, seed, octaves = 4) {
  let total = 0;
  let amplitude = 1;
  let frequency = 1;
  let norm = 0;
  for (let i = 0; i < octaves; i++) {
    total += valueNoise(x * frequency, y * frequency, seed + i) * amplitude;
    norm += amplitude;
    amplitude *= 0.5;
    frequency *= 2;
  }
  return total / norm;
}

// Each surface is a height function; albedo and roughness are read off the height, and
// the normal is derived from it. That ordering is the point — it is what makes plank
// gaps and shingle edges line up with their own shading instead of merely looking painted.
const SURFACES = {
  wood: {
    seed: 11,
    tint: [0.42, 0.28, 0.16],
    height(u, v, seed) {
      const plank = Math.floor(v * 6);
      const gap = Math.abs((v * 6) % 1 - 0.5) > 0.46 ? 0.35 : 1;
      const grain = fbm(u * 40 + plank * 17, v * 4, seed, 3);
      return gap * (0.6 + grain * 0.4);
    },
  },
  plaster: {
    seed: 23,
    tint: [0.78, 0.75, 0.68],
    height(u, v, seed) {
      const coarse = fbm(u * 6, v * 6, seed, 4);
      const pit = fbm(u * 30, v * 30, seed + 5, 2);
      return 0.75 + coarse * 0.2 - (pit > 0.82 ? 0.35 : 0);
    },
  },
  grass: {
    seed: 53,
    tint: [0.33, 0.42, 0.20],
    height(u, v, seed) {
      const clump = fbm(u * 10, v * 10, seed, 4);
      const blade = fbm(u * 70, v * 70, seed + 3, 2);
      const worn = fbm(u * 3, v * 3, seed + 9, 3);
      return 0.45 + clump * 0.32 + blade * 0.23 - (worn > 0.74 ? 0.2 : 0);
    },
  },
  roof: {
    seed: 37,
    tint: [0.34, 0.16, 0.13],
    height(u, v, seed) {
      const row = Math.floor(v * 10);
      const stagger = row % 2 === 0 ? 0 : 0.5;
      const withinRow = ((u * 8 + stagger) % 1);
      const edge = withinRow > 0.9 || (v * 10) % 1 > 0.88 ? 0.45 : 1;
      return edge * (0.7 + fbm(u * 20, v * 20, seed, 2) * 0.3);
    },
  },
};

function buildSurface(name) {
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

      // albedo: the tint darkened where the surface sits lower
      const shade = 0.55 + h * 0.45;
      albedo[i] = Math.min(255, spec.tint[0] * shade * 255);
      albedo[i + 1] = Math.min(255, spec.tint[1] * shade * 255);
      albedo[i + 2] = Math.min(255, spec.tint[2] * shade * 255);
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
      let nx = dx * strength;
      let ny = dy * strength;
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

    const width = this.container.clientWidth;
    const height = this.container.clientHeight;

    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(width, height);
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    // Without tone mapping the sun clips to white and everything below it reads muddy,
    // which is what made the first render look like a night scene.
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.35;
    this.container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0b1016);
    this.scene.fog = new THREE.Fog(0x0b1016, radius * TILE * 1.5, radius * TILE * 4);

    this.camera = new THREE.PerspectiveCamera(50, width / height, 0.1, 500);

    // Materials and shared geometry are built once here, never per frame.
    this.materials = {
      wood: buildSurface("wood"),
      plaster: buildSurface("plaster"),
      roof: buildSurface("roof"),
    };
    for (const material of Object.values(this.materials)) {
      this.disposables.push(material, material.map, material.normalMap, material.roughnessMap);
    }

    this._light();
    this._ground();
    this._controls();
    this.update(snapshot);
    this._frame();
  }

  _light() {
    this.scene.add(new THREE.HemisphereLight(0xbcd4ec, 0x4a3d2a, 1.6));
    const sun = new THREE.DirectionalLight(0xffe8c0, 3.0);
    sun.position.set(18, 26, 12);
    sun.castShadow = true;
    sun.shadow.mapSize.set(1024, 1024);
    const span = this.radius * TILE + 6;
    Object.assign(sun.shadow.camera, {
      left: -span, right: span, top: span, bottom: -span, near: 1, far: 90,
    });
    sun.shadow.camera.updateProjectionMatrix();
    this.scene.add(sun);
    this.scene.add(sun.target);
  }

  _ground() {
    const span = (this.radius * 2 + 1) * TILE;
    const geometry = new THREE.PlaneGeometry(span, span, 1, 1);
    geometry.rotateX(-Math.PI / 2);
    const material = buildSurface("grass");
    // One texture tile per two world tiles, so blades stay blade-sized rather than
    // being stretched across the whole plot.
    const repeat = (this.radius * 2 + 1) / 2;
    for (const map of [material.map, material.normalMap, material.roughnessMap]) {
      map.repeat.set(repeat, repeat);
    }
    this.disposables.push(material, material.map, material.normalMap, material.roughnessMap);
    const ground = new THREE.Mesh(geometry, material);
    ground.receiveShadow = true;
    this.scene.add(ground);
    this.disposables.push(geometry, material);
  }

  // Orbit + pan + zoom, hand-rolled: about sixty lines against another vendored file,
  // and it keeps the dependency surface to three.js alone.
  _controls() {
    const canvas = this.renderer.domElement;
    this.orbit = { theta: Math.PI * 0.25, phi: Math.PI * 0.32, distance: this.radius * TILE * 2.2 };
    this.target = new THREE.Vector3(0, 0, 0);

    let dragging = null;
    let lastX = 0;
    let lastY = 0;

    const down = (event) => {
      dragging = event.button === 2 || event.shiftKey ? "pan" : "orbit";
      lastX = event.clientX;
      lastY = event.clientY;
      canvas.setPointerCapture(event.pointerId);
    };
    const move = (event) => {
      if (!dragging) return;
      const dx = event.clientX - lastX;
      const dy = event.clientY - lastY;
      lastX = event.clientX;
      lastY = event.clientY;
      if (dragging === "orbit") {
        this.orbit.theta -= dx * 0.006;
        this.orbit.phi = Math.max(0.08, Math.min(Math.PI / 2 - 0.05, this.orbit.phi - dy * 0.006));
      } else {
        const scale = this.orbit.distance * 0.0016;
        const forward = new THREE.Vector3(Math.sin(this.orbit.theta), 0, Math.cos(this.orbit.theta));
        const right = new THREE.Vector3(forward.z, 0, -forward.x);
        this.target.addScaledVector(right, -dx * scale);
        this.target.addScaledVector(forward, -dy * scale);
      }
    };
    const up = (event) => {
      dragging = null;
      if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    };
    const wheel = (event) => {
      event.preventDefault();
      const min = TILE * 1.5;
      const max = this.radius * TILE * 5;
      this.orbit.distance = Math.max(min, Math.min(max, this.orbit.distance * (1 + event.deltaY * 0.0012)));
    };
    const menu = (event) => event.preventDefault();

    canvas.addEventListener("pointerdown", down);
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerup", up);
    canvas.addEventListener("wheel", wheel, { passive: false });
    canvas.addEventListener("contextmenu", menu);
    this._listeners = () => {
      canvas.removeEventListener("pointerdown", down);
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("pointerup", up);
      canvas.removeEventListener("wheel", wheel);
      canvas.removeEventListener("contextmenu", menu);
    };
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
    if (!this.active) return;
    this._rebuild(snapshot);
    this.stats = {
      huts: snapshot.buildings.filter((b) => this.inFocus(b)).length,
      agents: snapshot.agents.filter((a) => this.inFocus(a)).length,
    };
  }

  _clearGroup(name) {
    const existing = this[name];
    if (!existing) return;
    this.scene.remove(existing);
    existing.traverse((node) => {
      if (node.isMesh && node.geometry) node.geometry.dispose();
    });
  }

  _rebuild(snapshot) {
    this._clearGroup("built");
    const group = new THREE.Group();

    for (const building of snapshot.buildings) {
      if (!this.inFocus(building)) continue;
      const [x, z] = this.local(building);
      group.add(this._hut(x, z, building.id));
    }
    for (const node of snapshot.resources) {
      if (!this.inFocus(node) || node.amount <= 0) continue;
      const [x, z] = this.local(node);
      group.add(this._resource(x, z, node));
    }
    for (const agent of snapshot.agents) {
      if (!this.inFocus(agent)) continue;
      const [x, z] = this.local(agent);
      group.add(this._agent(x, z, agent));
    }

    this.scene.add(group);
    this.built = group;
  }

  // A modular hut: four walls with real thickness, a doorway gap in one of them, and a
  // pitched roof. Simple enough to read at a glance, structured enough to grow into a
  // real kit later.
  _hut(x, z, seedId) {
    const hut = new THREE.Group();
    const w = TILE * 0.82;
    const h = TILE * 0.75;
    const t = TILE * 0.1;
    const wall = this.materials.plaster;
    const wood = this.materials.wood;

    const piece = (sx, sy, sz, px, py, pz, material) => {
      const geometry = new THREE.BoxGeometry(sx, sy, sz);
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.set(px, py, pz);
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      return mesh;
    };

    hut.add(piece(w, h, t, 0, h / 2, -w / 2, wall)); // back
    hut.add(piece(t, h, w, -w / 2, h / 2, 0, wall)); // left
    hut.add(piece(t, h, w, w / 2, h / 2, 0, wall));  // right

    // front wall split around a doorway
    const door = w * 0.34;
    const jamb = (w - door) / 2;
    hut.add(piece(jamb, h, t, -(door + jamb) / 2, h / 2, w / 2, wall));
    hut.add(piece(jamb, h, t, (door + jamb) / 2, h / 2, w / 2, wall));
    hut.add(piece(door, h * 0.22, t, 0, h - h * 0.11, w / 2, wood)); // lintel

    // pitched roof: two slabs leaning together
    const slope = TILE * 0.62;
    for (const side of [-1, 1]) {
      const geometry = new THREE.BoxGeometry(w * 1.16, t * 1.4, slope * 1.25);
      const mesh = new THREE.Mesh(geometry, this.materials.roof);
      mesh.position.set(0, h + slope * 0.28, side * slope * 0.42);
      mesh.rotation.x = side * 0.62;
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      hut.add(mesh);
    }

    // a rotation per hut so a cluster does not look stamped
    hut.rotation.y = ((seedId * 2654435761) % 1000) / 1000 * 0.5 - 0.25;
    hut.position.set(x, 0, z);
    return hut;
  }

  _resource(x, z, node) {
    const isFood = node.kind === "FOOD";
    const geometry = isFood
      ? new THREE.IcosahedronGeometry(TILE * 0.16, 0)
      : new THREE.CylinderGeometry(TILE * 0.07, TILE * 0.09, TILE * 0.7, 6);
    const material = isFood
      ? new THREE.MeshStandardMaterial({ color: 0x2f7f3a, roughness: 0.8 })
      : this.materials.wood;
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.set(x, isFood ? TILE * 0.16 : TILE * 0.35, z);
    mesh.castShadow = true;
    if (isFood) this.disposables.push(material);
    return mesh;
  }

  _agent(x, z, agent) {
    const group = new THREE.Group();
    const colour = new THREE.Color(agent.clan === null ? 0x8b949e : clanHue(agent.clan));
    const material = new THREE.MeshStandardMaterial({ color: colour, roughness: 0.6 });
    const body = new THREE.Mesh(new THREE.CapsuleGeometry(TILE * 0.13, TILE * 0.34, 4, 8), material);
    body.position.y = TILE * 0.35;
    body.castShadow = true;
    group.add(body);

    if (agent.user) {
      const ringGeometry = new THREE.TorusGeometry(TILE * 0.26, TILE * 0.03, 6, 16);
      const ringMaterial = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.4 });
      const ring = new THREE.Mesh(ringGeometry, ringMaterial);
      ring.rotation.x = Math.PI / 2;
      ring.position.y = TILE * 0.02;
      group.add(ring);
      this.disposables.push(ringGeometry, ringMaterial);
    }

    this.disposables.push(material);
    group.position.set(x, 0, z);
    return group;
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
    const width = this.container.clientWidth;
    const height = this.container.clientHeight;
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

    this._clearGroup("built");
    for (const item of this.disposables) {
      if (item && typeof item.dispose === "function") item.dispose();
    }
    this.disposables = [];

    this.scene.traverse((node) => {
      if (node.isMesh) {
        node.geometry?.dispose();
        if (Array.isArray(node.material)) node.material.forEach((m) => m.dispose());
        else node.material?.dispose();
      }
    });

    this.renderer.dispose();
    this.renderer.domElement.remove();
    this.scene = null;
    this.renderer = null;
    this.built = null;
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
  // simulation change to answer.
  let best = null;
  let bestCount = -1;
  for (const anchor of snapshot.buildings) {
    let count = 0;
    for (const other of snapshot.buildings) {
      if (Math.abs(other.x - anchor.x) <= radius && Math.abs(other.y - anchor.y) <= radius) count++;
    }
    if (count > bestCount) {
      bestCount = count;
      best = anchor;
    }
  }
  return best ? { centre: [best.x, best.y], count: bestCount } : null;
}
