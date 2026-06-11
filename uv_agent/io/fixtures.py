"""Synthetic meshes for tests and demos (no Blender required).

These let the whole engine be exercised end-to-end without a ``.blend`` file.
"""

from __future__ import annotations

import math

from uv_agent.geometry.mesh_graph import MeshGraph


def build_cube(object_id: str = "cube") -> MeshGraph:
    """Unit cube centered at origin. 8 verts, 12 edges, 6 quad faces.

    Every edge is shared by 2 perpendicular faces -> dihedral 90 deg, so a
    hard-edge island planner splits it into 6 islands (one per face)."""
    s = 0.5
    verts = [
        (-s, -s, -s),
        (s, -s, -s),
        (s, s, -s),
        (-s, s, -s),
        (-s, -s, s),
        (s, -s, s),
        (s, s, s),
        (-s, s, s),
    ]
    faces = [
        [0, 3, 2, 1],  # bottom (-z)
        [4, 5, 6, 7],  # top (+z)
        [0, 1, 5, 4],  # front (-y)
        [2, 3, 7, 6],  # back (+y)
        [1, 2, 6, 5],  # right (+x)
        [3, 0, 4, 7],  # left (-x)
    ]
    return MeshGraph.from_faces(object_id, verts, faces)


def build_grid_plane(nx: int = 4, ny: int = 4, size: float = 1.0, object_id: str = "plane") -> MeshGraph:
    """Flat subdivided plane in the XY plane. All faces coplanar (+Z normal),
    so dihedral is 0 everywhere -> a single island, and planar projection is
    exact (zero stretch / zero angle distortion)."""
    verts = []
    for j in range(ny + 1):
        for i in range(nx + 1):
            x = (i / nx - 0.5) * size
            y = (j / ny - 0.5) * size
            verts.append((x, y, 0.0))

    def vid(i: int, j: int) -> int:
        return j * (nx + 1) + i

    faces = []
    for j in range(ny):
        for i in range(nx):
            faces.append([vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)])
    return MeshGraph.from_faces(object_id, verts, faces)


def build_cylinder(segments: int = 12, rings: int = 3, radius: float = 0.5, height: float = 1.0,
                   object_id: str = "cylinder") -> MeshGraph:
    """Open cylinder side (no caps), good for cylindrical projection tests."""
    verts = []
    for r in range(rings + 1):
        z = (r / rings - 0.5) * height
        for s in range(segments):
            ang = 2 * math.pi * s / segments
            verts.append((radius * math.cos(ang), radius * math.sin(ang), z))

    def vid(s: int, r: int) -> int:
        return r * segments + (s % segments)

    faces = []
    for r in range(rings):
        for s in range(segments):
            faces.append([vid(s, r), vid(s + 1, r), vid(s + 1, r + 1), vid(s, r + 1)])
    return MeshGraph.from_faces(object_id, verts, faces)


def build_curved_strip(segments: int = 12, r_inner: float = 1.0, r_outer: float = 1.3,
                       total_angle: float = math.pi, object_id: str = "curved_strip") -> MeshGraph:
    """A flat annulus-sector ribbon, 1 quad wide, bent along an arc (in the XY
    plane). Planar projection keeps it curved; strip unwrap straightens it."""
    verts = []
    for i in range(segments + 1):
        t = total_angle * i / segments
        c, s = math.cos(t), math.sin(t)
        verts.append((r_inner * c, r_inner * s, 0.0))  # inner rung vertex
        verts.append((r_outer * c, r_outer * s, 0.0))  # outer rung vertex
    faces = []
    for i in range(segments):
        a_in, a_out = 2 * i, 2 * i + 1
        b_in, b_out = 2 * (i + 1), 2 * (i + 1) + 1
        faces.append([a_in, a_out, b_out, b_in])
    return MeshGraph.from_faces(object_id, verts, faces)


def build_curved_band(segments: int = 12, width_quads: int = 3, r0: float = 1.0,
                      dr: float = 0.15, total_angle: float = math.pi,
                      object_id: str = "curved_band") -> MeshGraph:
    """A curved band that is ``width_quads`` quads wide (concentric arcs).
    Planar projection keeps it bent; grid unwrap straightens it to a rectangle."""
    rings = width_quads + 1
    verts = []
    for i in range(segments + 1):
        t = total_angle * i / segments
        c, s = math.cos(t), math.sin(t)
        for w in range(rings):
            r = r0 + dr * w
            verts.append((r * c, r * s, 0.0))

    def vid(i, w):
        return i * rings + w

    faces = []
    for i in range(segments):
        for w in range(width_quads):
            faces.append([vid(i, w), vid(i, w + 1), vid(i + 1, w + 1), vid(i + 1, w)])
    return MeshGraph.from_faces(object_id, verts, faces)


def build_two_material_plane(nx: int = 4, ny: int = 4, object_id: str = "two_mat") -> MeshGraph:
    """Flat plane split into two material slots along the middle, to exercise
    material-boundary island splitting."""
    base = build_grid_plane(nx, ny, object_id=object_id)
    verts = [v.co for v in base.vertices]
    faces = [f.vertex_ids for f in base.faces]
    # Left half material 0, right half material 1.
    mats = []
    for fid, f in enumerate(base.faces):
        cx = sum(base.vertices[v].co[0] for v in f.vertex_ids) / len(f.vertex_ids)
        mats.append(0 if cx < 0 else 1)
    return MeshGraph.from_faces(object_id, verts, faces, material_indices=mats)
