"""Quad-flow scoring + improvement (retopology plan §6.6, §6.8, §10 Phase 6).

Phase 6 makes the topology *flow* more naturally: more quads, more regular vertex
valences, squarer faces. It is pure Python on a
:class:`~uv_agent.geometry.mesh_graph.MeshGraph` so the score and the cleanup ops
are unit-testable; the Blender adapter (:mod:`retopo_agent.blender.quadflow`) does
the equivalent with native operators on large meshes.

``quad_flow_score`` -- the §6.6 ``edge_flow_score`` -- is a 0..1 blend of:

- **quad fraction** -- share of faces that are quads (tris/n-gons hurt flow);
- **valence regularity** -- how close interior vertices are to valence 4 (the
  ideal for a quad mesh; extraordinary vertices disrupt flow);
- **face squareness** -- how close quad corners are to 90 deg.

Improvement ops (plan §6.8 ``convert_tris_to_quads`` / ``relax_edge_flow``):

- :func:`tris_to_quads` greedily merges adjacent triangle pairs into quads,
  preferring near-coplanar, square merges and never merging across hard edges;
- :func:`relax_vertices` applies Taubin (lambda|mu) smoothing -- which relaxes
  edge flow without the shrinkage of plain Laplacian -- pinning feature and
  boundary vertices so shape is kept.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph

W_QUAD = 0.4
W_VALENCE = 0.3
W_FACE = 0.3
IDEAL_QUAD_VALENCE = 4


def _coords(mesh: MeshGraph) -> np.ndarray:
    return np.asarray([v.co for v in mesh.vertices], dtype=float)


def _vertex_neighbors(mesh: MeshGraph) -> list[list[int]]:
    nb: list[set[int]] = [set() for _ in range(mesh.vertex_count)]
    for e in mesh.edges:
        a, b = e.vertex_ids
        nb[a].add(b)
        nb[b].add(a)
    return [sorted(s) for s in nb]


def _boundary_vertices(mesh: MeshGraph) -> set[int]:
    bv: set[int] = set()
    for e in mesh.edges:
        if e.is_boundary or e.is_non_manifold:
            bv.update(e.vertex_ids)
    return bv


def vertex_valence(mesh: MeshGraph) -> np.ndarray:
    """Number of edges incident to each vertex."""
    val = np.zeros(mesh.vertex_count, dtype=int)
    for e in mesh.edges:
        a, b = e.vertex_ids
        val[a] += 1
        val[b] += 1
    return val


def _interior_angles_deg(co_loop: np.ndarray) -> list[float]:
    n = len(co_loop)
    angles = []
    for i in range(n):
        prev_v = co_loop[(i - 1) % n] - co_loop[i]
        next_v = co_loop[(i + 1) % n] - co_loop[i]
        ln = np.linalg.norm(prev_v) * np.linalg.norm(next_v)
        if ln < 1e-12:
            angles.append(90.0)
            continue
        cos = float(np.clip(np.dot(prev_v, next_v) / ln, -1.0, 1.0))
        angles.append(math.degrees(math.acos(cos)))
    return angles


def _quad_squareness(co_loop: np.ndarray) -> float:
    """1.0 for a rectangle, decreasing as corner angles deviate from 90 deg."""
    angles = _interior_angles_deg(co_loop)
    mean_dev = sum(abs(a - 90.0) for a in angles) / len(angles)
    return max(0.0, 1.0 - mean_dev / 90.0)


@dataclass
class QuadFlowReport:
    face_count: int
    quad_count: int
    triangle_count: int
    ngon_count: int
    quad_fraction: float
    valence_regularity: float
    face_squareness: float
    score: float
    valence_issue_count: int
    valence_histogram: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "face_count": self.face_count,
            "quad_count": self.quad_count,
            "triangle_count": self.triangle_count,
            "ngon_count": self.ngon_count,
            "quad_fraction": round(self.quad_fraction, 4),
            "valence_regularity": round(self.valence_regularity, 4),
            "face_squareness": round(self.face_squareness, 4),
            "edge_flow_score": round(self.score, 4),  # §6.6 name
            "quad_flow_score": round(self.score, 4),
            "valence_issue_count": self.valence_issue_count,
            "valence_histogram": self.valence_histogram,
        }


def quad_flow_score(mesh: MeshGraph) -> QuadFlowReport:
    """Score how natural the mesh's quad flow is (plan §6.6 ``edge_flow_score``)."""
    total = mesh.face_count
    if total == 0:
        return QuadFlowReport(0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0, {})

    co = _coords(mesh)
    quads = tris = ngons = 0
    squareness_sum = 0.0
    for f in mesh.faces:
        sides = len(f.vertex_ids)
        if sides == 3:
            tris += 1
        elif sides == 4:
            quads += 1
            squareness_sum += _quad_squareness(co[f.vertex_ids])
        else:
            ngons += 1
    quad_fraction = quads / total
    face_squareness = squareness_sum / quads if quads else 0.0

    valence = vertex_valence(mesh)
    boundary = _boundary_vertices(mesh)
    interior = [v for v in range(mesh.vertex_count) if v not in boundary and valence[v] > 0]
    if interior:
        regs = [max(0.0, 1.0 - abs(int(valence[v]) - IDEAL_QUAD_VALENCE) / IDEAL_QUAD_VALENCE) for v in interior]
        valence_regularity = float(np.mean(regs))
        valence_issue_count = sum(1 for v in interior if valence[v] != IDEAL_QUAD_VALENCE)
    else:
        valence_regularity = 0.0
        valence_issue_count = 0

    histogram: dict = {}
    for v in interior:
        histogram[int(valence[v])] = histogram.get(int(valence[v]), 0) + 1

    score = W_QUAD * quad_fraction + W_VALENCE * valence_regularity + W_FACE * face_squareness
    return QuadFlowReport(
        face_count=total,
        quad_count=quads,
        triangle_count=tris,
        ngon_count=ngons,
        quad_fraction=quad_fraction,
        valence_regularity=valence_regularity,
        face_squareness=face_squareness,
        score=score,
        valence_issue_count=valence_issue_count,
        valence_histogram=dict(sorted(histogram.items())),
    )


def _merge_two_tris(tri_loop: list[int], shared: tuple[int, int], opposite: int) -> list[int] | None:
    """Build a quad by inserting ``opposite`` into ``tri_loop`` along its
    ``shared`` edge, preserving winding. Returns the 4-vertex loop or None."""
    a, b = shared
    n = len(tri_loop)
    for i in range(n):
        p, q = tri_loop[i], tri_loop[(i + 1) % n]
        if {p, q} == {a, b}:
            return tri_loop[: i + 1] + [opposite] + tri_loop[i + 1:]
    return None


def tris_to_quads(mesh: MeshGraph, *, max_angle: float = 40.0, object_id: str | None = None) -> MeshGraph:
    """Greedily merge adjacent triangle pairs into quads (plan §6.8).

    Candidates are interior edges shared by two triangles whose dihedral angle is
    <= ``max_angle`` (so hard creases are never merged across). Merges are taken
    best-quality first (near-coplanar + square), each triangle used at most once.
    """
    co = _coords(mesh)
    oid = object_id or f"{mesh.object_id}_QF"

    candidates = []  # (quality, f0, f1, quad_loop, material)
    for e in mesh.edges:
        if len(e.face_ids) != 2:
            continue
        f0, f1 = e.face_ids
        face0, face1 = mesh.faces[f0], mesh.faces[f1]
        if len(face0.vertex_ids) != 3 or len(face1.vertex_ids) != 3:
            continue
        if e.dihedral_angle > max_angle:
            continue
        a, b = e.vertex_ids
        opposite = next((v for v in face1.vertex_ids if v != a and v != b), None)
        if opposite is None:
            continue
        quad = _merge_two_tris(list(face0.vertex_ids), (a, b), opposite)
        if quad is None or len(set(quad)) != 4:
            continue
        planarity = 1.0 - e.dihedral_angle / 180.0
        quality = 0.5 * planarity + 0.5 * _quad_squareness(co[quad])
        candidates.append((quality, f0, f1, quad, face0.material_index))

    candidates.sort(key=lambda c: -c[0])
    used: set[int] = set()
    quad_faces: list[tuple[list[int], int]] = []
    for _, f0, f1, quad, mat in candidates:
        if f0 in used or f1 in used:
            continue
        used.add(f0)
        used.add(f1)
        quad_faces.append((quad, mat))

    new_faces: list[list[int]] = []
    new_materials: list[int] = []
    for f in mesh.faces:
        if f.id in used:
            continue
        new_faces.append(list(f.vertex_ids))
        new_materials.append(f.material_index)
    for quad, mat in quad_faces:
        new_faces.append(quad)
        new_materials.append(mat)

    return MeshGraph.from_faces(oid, [v.co for v in mesh.vertices], new_faces, material_indices=new_materials)


def relax_vertices(
    mesh: MeshGraph,
    *,
    iterations: int = 10,
    lam: float = 0.5,
    mu: float = -0.53,
    feature_mask=None,
    object_id: str | None = None,
) -> MeshGraph:
    """Taubin (lambda|mu) smoothing to relax edge flow without shrinking (plan §6.8).

    Feature vertices (``feature_mask``) and boundary/non-manifold vertices are
    pinned, so creases and silhouettes stay put while the interior regularizes.
    """
    co = _coords(mesh).copy()
    neighbors = _vertex_neighbors(mesh)
    pinned = _boundary_vertices(mesh)
    if feature_mask is not None:
        mask = np.asarray(feature_mask, dtype=bool)
        pinned |= {v for v in range(mesh.vertex_count) if mask[v]}

    movable = [v for v in range(mesh.vertex_count) if v not in pinned and neighbors[v]]

    def smooth_pass(positions: np.ndarray, factor: float) -> np.ndarray:
        out = positions.copy()
        for v in movable:
            avg = positions[neighbors[v]].mean(axis=0)
            out[v] = positions[v] + factor * (avg - positions[v])
        return out

    for _ in range(iterations):
        co = smooth_pass(co, lam)
        co = smooth_pass(co, mu)

    return MeshGraph.from_faces(
        object_id or f"{mesh.object_id}_RELAX",
        [tuple(c) for c in co],
        [list(f.vertex_ids) for f in mesh.faces],
        material_indices=[f.material_index for f in mesh.faces],
    )


def improve_quad_flow(
    mesh: MeshGraph,
    *,
    feature_mask=None,
    max_angle: float = 40.0,
    relax_iterations: int = 8,
    object_id: str | None = None,
) -> MeshGraph:
    """Convert triangles to quads, then relax edge flow (plan §10 Phase 6).

    ``tris_to_quads`` preserves the vertex set/indexing, so ``feature_mask`` stays
    valid for the subsequent relax.
    """
    quadded = tris_to_quads(mesh, max_angle=max_angle, object_id=object_id)
    return relax_vertices(
        quadded,
        iterations=relax_iterations,
        feature_mask=feature_mask,
        object_id=object_id or f"{mesh.object_id}_QF",
    )
