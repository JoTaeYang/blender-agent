"""Synthetic high-poly meshes for retopology tests/demos (no Blender required).

These stand in for the §15.8 test models (sculpted rock, helmet, 100k sphere)
so the Phase 1 generator can be exercised end-to-end offline. Resolution is
parameterised, so the same builder serves a tiny unit-test mesh and a heavy
"100k-face" benchmark.
"""

from __future__ import annotations

import math

from uv_agent.geometry.mesh_graph import MeshGraph


def build_uv_sphere(
    segments: int = 24,
    rings: int = 16,
    radius: float = 1.0,
    object_id: str = "sphere_high",
) -> MeshGraph:
    """Closed lat-long ("UV") sphere: quad bands between two triangle-fan caps.

    ``rings`` is the number of latitude bands, ``segments`` the longitude count.
    Face count is ``segments * (rings - 2)`` quads ``+ 2 * segments`` cap tris.
    A clean closed manifold -- the spec's preferred test input (plan §15.8).
    """
    segments = max(3, int(segments))
    rings = max(2, int(rings))

    verts: list[tuple[float, float, float]] = [(0.0, 0.0, radius)]  # north pole = 0
    for r in range(1, rings):
        theta = math.pi * r / rings
        z = radius * math.cos(theta)
        rad = radius * math.sin(theta)
        for s in range(segments):
            phi = 2.0 * math.pi * s / segments
            verts.append((rad * math.cos(phi), rad * math.sin(phi), z))
    south = len(verts)
    verts.append((0.0, 0.0, -radius))  # south pole

    def ring_vid(r: int, s: int) -> int:  # r in 1..rings-1
        return 1 + (r - 1) * segments + (s % segments)

    faces: list[list[int]] = []
    for s in range(segments):  # north cap
        faces.append([0, ring_vid(1, s), ring_vid(1, s + 1)])
    for r in range(1, rings - 1):  # quad bands
        for s in range(segments):
            faces.append([ring_vid(r, s), ring_vid(r, s + 1), ring_vid(r + 1, s + 1), ring_vid(r + 1, s)])
    for s in range(segments):  # south cap
        faces.append([south, ring_vid(rings - 1, s + 1), ring_vid(rings - 1, s)])

    return MeshGraph.from_faces(object_id, verts, faces)


def build_subdivided_cube(divisions: int = 10, size: float = 1.0, object_id: str = "cube_high") -> MeshGraph:
    """Cube whose six faces are each subdivided into ``divisions x divisions``
    quads -- a hard-surface block for testing edge/silhouette preservation
    (plan §15.8 "high-poly cube bevel asset").

    Face count is ``6 * divisions**2``. Shared cube edges/corners are welded so
    the result is a single closed manifold.
    """
    n = max(1, int(divisions))
    s = size / 2.0
    coord_index: dict[tuple[int, int, int], int] = {}
    verts: list[tuple[float, float, float]] = []

    def vid(ix: int, iy: int, iz: int) -> int:
        key = (ix, iy, iz)
        idx = coord_index.get(key)
        if idx is None:
            idx = len(verts)
            coord_index[key] = idx
            verts.append((-s + size * ix / n, -s + size * iy / n, -s + size * iz / n))
        return idx

    faces: list[list[int]] = []

    def add_face(a: int, b: int, c: int, d: int) -> None:
        faces.append([a, b, c, d])

    for i in range(n):
        for j in range(n):
            # -Z and +Z
            add_face(vid(i, j, 0), vid(i, j + 1, 0), vid(i + 1, j + 1, 0), vid(i + 1, j, 0))
            add_face(vid(i, j, n), vid(i + 1, j, n), vid(i + 1, j + 1, n), vid(i, j + 1, n))
            # -Y and +Y
            add_face(vid(i, 0, j), vid(i + 1, 0, j), vid(i + 1, 0, j + 1), vid(i, 0, j + 1))
            add_face(vid(i, n, j), vid(i, n, j + 1), vid(i + 1, n, j + 1), vid(i + 1, n, j))
            # -X and +X
            add_face(vid(0, i, j), vid(0, i, j + 1), vid(0, i + 1, j + 1), vid(0, i + 1, j))
            add_face(vid(n, i, j), vid(n, i + 1, j), vid(n, i + 1, j + 1), vid(n, i, j + 1))

    return MeshGraph.from_faces(object_id, verts, faces)
