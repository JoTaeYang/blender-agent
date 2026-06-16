"""Chart-UV test fixtures (chart-UV plan §4.3 / §10).

Watertight manifold MeshGraphs with the topology the segmentation must handle:
extremities (tubes/spikes), curved regions needing distortion splits, and a
humanoid-ish blob with limbs. Built by displacing a closed UV sphere — reusing
:func:`retopo_agent.io.fixtures.build_uv_sphere` so vertex/topology generation is
shared and the result is a clean closed manifold.
"""

from __future__ import annotations

import math

import numpy as np

from retopo_agent.io.fixtures import build_uv_sphere
from uv_agent.geometry.mesh_graph import MeshGraph


def _rebuild(sphere: MeshGraph, coords: np.ndarray, object_id: str) -> MeshGraph:
    faces = [list(f.vertex_ids) for f in sphere.faces]
    return MeshGraph.from_faces(object_id, [tuple(c) for c in coords], faces)


def build_displaced_sphere(segments: int = 24, rings: int = 16, *, amp: float = 0.18,
                           freq: float = 4.0, object_id: str = "displaced_sphere") -> MeshGraph:
    """A closed sphere with sinusoidal radial bumps — a smoothly-curved surface with no
    sharp creases, so the distortion-driven split loop (R1) is what must chart it."""
    sphere = build_uv_sphere(segments=segments, rings=rings)
    co = np.array([v.co for v in sphere.vertices], dtype=float)
    r = np.linalg.norm(co, axis=1, keepdims=True)
    unit = co / np.clip(r, 1e-9, None)
    bump = 1.0 + amp * np.sin(freq * unit[:, 0:1]) * np.cos(freq * unit[:, 1:2])
    return _rebuild(sphere, unit * r * bump, object_id)


def build_capsule_with_spikes(segments: int = 20, rings: int = 18, *, n_spikes: int = 4,
                              spike_len: float = 1.6, object_id: str = "capsule_spikes") -> MeshGraph:
    """An elongated capsule (z-stretched sphere) with a few radial spikes — explicit
    tube + extremity geometry for the tube/extremity detector and disk-cut tests."""
    sphere = build_uv_sphere(segments=segments, rings=rings)
    co = np.array([v.co for v in sphere.vertices], dtype=float)
    co[:, 2] *= 2.2  # elongate into a capsule
    # Push the n_spikes vertices nearest evenly-spaced equator directions outward.
    eq = np.array([[math.cos(2 * math.pi * k / n_spikes), math.sin(2 * math.pi * k / n_spikes), 0.0]
                   for k in range(n_spikes)])
    unit = co / np.clip(np.linalg.norm(co, axis=1, keepdims=True), 1e-9, None)
    for d in eq:
        k = int(np.argmax(unit @ d))
        co[k] = co[k] + d * spike_len
    return _rebuild(sphere, co, object_id)


def build_folded_planes(n: int = 6, *, object_id: str = "folded_planes") -> MeshGraph:
    """Two flat ``n×n`` quad grids meeting at a 90° fold (MINIMAL_DISTORTION_UV_PLAN §8
    Test 1). Grid A lies in the z=0 plane (0≤x,y≤1); grid B rises in +z from the shared
    top row (y=1, z=0), so the shared edge bends exactly 90°. The fold edges MUST become
    mandatory seams (R2); each flat half unwraps with zero distortion (R1 keeps it whole)."""
    coords: list[tuple[float, float, float]] = []
    idx: dict[tuple[str, int, int], int] = {}
    for i in range(n + 1):           # grid A: x = i/n, y = j/n, z = 0
        for j in range(n + 1):
            idx[("A", i, j)] = len(coords)
            coords.append((i / n, j / n, 0.0))
    for i in range(n + 1):           # grid B: x = i/n, y = 1, z = k/n (k=0 shares A's top row)
        for k in range(1, n + 1):
            idx[("B", i, k)] = len(coords)
            coords.append((i / n, 1.0, k / n))

    def vA(i, j):
        return idx[("A", i, j)]

    def vB(i, k):
        return idx[("A", i, n)] if k == 0 else idx[("B", i, k)]

    faces: list[list[int]] = []
    for i in range(n):
        for j in range(n):
            faces.append([vA(i, j), vA(i + 1, j), vA(i + 1, j + 1), vA(i, j + 1)])
    for i in range(n):
        for k in range(n):
            faces.append([vB(i, k), vB(i + 1, k), vB(i + 1, k + 1), vB(i, k + 1)])
    return MeshGraph.from_faces(object_id, coords, faces)


def build_humanoid_blob(segments: int = 20, rings: int = 20, *,
                        object_id: str = "humanoid_blob") -> MeshGraph:
    """A crude torso-with-limbs blob (elongated body + four directional bulges) — a
    multi-extremity shape for the split/merge and chart-count logic."""
    sphere = build_uv_sphere(segments=segments, rings=rings)
    co = np.array([v.co for v in sphere.vertices], dtype=float)
    co[:, 2] *= 1.8  # torso height
    unit = co / np.clip(np.linalg.norm(co, axis=1, keepdims=True), 1e-9, None)
    # Limb directions: two arms (±x, up), two legs (±x, down).
    limbs = [(0.9, 0.0, 0.6), (-0.9, 0.0, 0.6), (0.5, 0.0, -1.0), (-0.5, 0.0, -1.0)]
    for lx, ly, lz in limbs:
        d = np.array([lx, ly, lz]); d = d / np.linalg.norm(d)
        w = np.clip(unit @ d, 0.0, None) ** 6  # tight bulge toward each limb
        co += (d[None, :] * (0.8 * w)[:, None])
    return _rebuild(sphere, co, object_id)
