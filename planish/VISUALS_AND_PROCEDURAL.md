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