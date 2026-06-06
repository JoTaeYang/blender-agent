"""Initial UV coordinate generation per island (plan §7.3 / Phase 4).

MVP approach: deterministic projection unwraps (planar + cylindrical). These
produce the *initial* UVs that relaxation and packing then refine. Blender's
native unwrap result can also be imported as an initial solution via the
adapter; the rest of the pipeline does not care where the seed UVs came from.
"""

from __future__ import annotations

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


def island_vertex_ids(mesh: MeshGraph, face_ids: list[int]) -> list[int]:
    seen: dict[int, None] = {}
    for fid in face_ids:
        for vid in mesh.faces[fid].vertex_ids:
            seen.setdefault(vid, None)
    return list(seen.keys())


def _area_weighted_normal(mesh: MeshGraph, face_ids: list[int]) -> np.ndarray:
    n = np.zeros(3)
    for fid in face_ids:
        f = mesh.faces[fid]
        n += np.asarray(f.normal) * f.area_3d
    norm = np.linalg.norm(n)
    if norm < 1e-9:
        # Degenerate (normals cancel): fall back to the largest face's normal.
        biggest = max(face_ids, key=lambda fid: mesh.faces[fid].area_3d)
        return np.asarray(mesh.faces[biggest].normal, dtype=float)
    return n / norm


def _basis_from_normal(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    up = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(up, normal))) > 0.95:
        up = np.array([0.0, 1.0, 0.0])
    tangent = np.cross(up, normal)
    tangent /= np.linalg.norm(tangent)
    bitangent = np.cross(normal, tangent)
    return tangent, bitangent


def project_island_planar(mesh: MeshGraph, face_ids: list[int], uvmap: UVMap) -> None:
    """Orthographic projection onto the island's average plane.

    Loops sharing a mesh vertex receive identical UVs (the projection is a pure
    function of 3D position), so the island stays connected in UV space.
    """
    if not face_ids:
        return
    normal = _area_weighted_normal(mesh, face_ids)
    tangent, bitangent = _basis_from_normal(normal)
    vids = island_vertex_ids(mesh, face_ids)
    origin = np.mean([mesh.vertex_co(v) for v in vids], axis=0)
    for fid in face_ids:
        for loop_index in mesh.faces[fid].loop_indices:
            p = mesh.vertex_co(mesh.loops[loop_index].vertex_id) - origin
            uvmap.set(loop_index, float(np.dot(p, tangent)), float(np.dot(p, bitangent)))


def project_island_cylindrical(
    mesh: MeshGraph, face_ids: list[int], uvmap: UVMap, axis: str = "z"
) -> None:
    """Cylindrical projection around ``axis`` (good for tubes / bolt rings)."""
    if not face_ids:
        return
    axis_idx = {"x": 0, "y": 1, "z": 2}[axis]
    a, b = [i for i in range(3) if i != axis_idx]
    vids = island_vertex_ids(mesh, face_ids)
    center = np.mean([mesh.vertex_co(v) for v in vids], axis=0)
    # Estimate radius so u uses real arc length (radius * angle). Using arc
    # length and raw height preserves the tube's aspect ratio -> low stretch.
    radius = float(np.mean([np.hypot(mesh.vertex_co(v)[a] - center[a],
                                     mesh.vertex_co(v)[b] - center[b]) for v in vids]))
    radius = radius or 1.0
    for fid in face_ids:
        loop_indices = mesh.faces[fid].loop_indices
        angles = []
        vs = []
        for loop_index in loop_indices:
            co = mesh.vertex_co(mesh.loops[loop_index].vertex_id)
            angles.append(float(np.arctan2(co[b] - center[b], co[a] - center[a])))  # -pi..pi
            vs.append(float(co[axis_idx]))
        # Seam unwrap: faces straddling the +/-pi wrap span the whole circle
        # backward (reads as a fold). Shift low angles by +2pi so each face stays
        # contiguous; this introduces the expected vertical UV seam.
        if max(angles) - min(angles) > np.pi:
            angles = [ang + 2 * np.pi if ang < 0 else ang for ang in angles]
        for loop_index, ang, v in zip(loop_indices, angles, vs):
            uvmap.set(loop_index, ang * radius, v)


def project_island(mesh: MeshGraph, face_ids: list[int], uvmap: UVMap, projection: str) -> None:
    if projection == "cylindrical":
        project_island_cylindrical(mesh, face_ids, uvmap)
    else:
        project_island_planar(mesh, face_ids, uvmap)
