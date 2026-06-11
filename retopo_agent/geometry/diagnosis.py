"""Decimation pre-process topology diagnosis (Decimation plan DM2, §5).

ZBrush Decimation Master runs a *pre-process* pass that inspects the mesh for
risk factors and constraints before reducing it. This module is the pure-Python
equivalent: given a :class:`~uv_agent.geometry.mesh_graph.MeshGraph` it reports
the structural facts that decide how aggressively -- and with what policy -- the
mesh can be decimated, and recommends a component-handling policy for the retry
ladder (DM3 / DM5).

It runs on a ``MeshGraph``, so the same code diagnoses synthetic meshes (unit
tests, no Blender) and meshes extracted from Blender; the Blender adapter is
:mod:`retopo_agent.blender.diagnosis`.

Why this matters for the anchor case (plan §1): the Collapse modifier floors at
8008 faces / 25 components, 20 of them tiny detached shells. Diagnosing that
plateau result surfaces exactly the structure that blocks a lower target, and the
recommended ``component_budget`` policy is what the DM3 / DM5 retry would apply.
The output ``decimation_diagnosis.json`` mirrors the plan §5 example.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph

# Component-handling policies (plan §5 example / DM3 §6). ``component_budget``
# keeps the plan's spelling; it maps to DM3's ``--component-policy budget``.
POLICY_PRESERVE_ALL = "preserve_all"
POLICY_COMPONENT_BUDGET = "component_budget"
POLICY_LARGEST_ONLY = "largest_only"

# Defaults for the relative thresholds, all overridable by the caller.
DEFAULT_TINY_FACE_FRACTION = 0.01  # a component < 1% of total faces is "tiny"
DEFAULT_SMALL_TRI_FRACTION = 0.01  # a triangle < 1% of the median area is "very small"
DEFAULT_NEAR_DUP_FRACTION = 1e-5  # near-duplicate vertices within 1e-5 * bbox diagonal
DEFAULT_SHARP_ANGLE_DEG = 30.0  # dihedral >= 30 deg is a sharp-normal boundary


@dataclass
class DiagnosisReport:
    """Structured pre-process diagnosis of a mesh (plan §5).

    The first block of fields is the plan §5 ``decimation_diagnosis.json``
    contract; the rest are the additional §5 "분석 항목" risk factors, all cheap to
    compute on a MeshGraph.
    """

    # -- plan §5 output contract ------------------------------------------
    component_count: int
    largest_component_face_ratio: float
    boundary_edge_count: int
    non_manifold_edge_count: int
    tiny_component_count: int
    recommended_policy: str

    # -- size --------------------------------------------------------------
    vertex_count: int = 0
    edge_count: int = 0
    face_count: int = 0

    # -- components --------------------------------------------------------
    largest_component_face_count: int = 0
    tiny_component_face_ratio: float = 0.0  # fraction of faces in tiny components

    # -- degeneracy / duplicates ------------------------------------------
    degenerate_face_count: int = 0
    duplicate_vertex_count: int = 0
    near_duplicate_vertex_count: int = 0
    duplicate_face_count: int = 0
    very_small_triangle_count: int = 0

    # -- feature boundaries (preserve candidates) -------------------------
    material_boundary_edge_count: int = 0
    uv_seam_edge_count: int = 0
    sharp_edge_count: int = 0

    # -- area distribution -------------------------------------------------
    face_area: dict = field(default_factory=dict)

    # -- cleanup signal ----------------------------------------------------
    needs_cleanup: bool = False
    cleanup_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            # plan §5 contract first, in the documented order.
            "component_count": self.component_count,
            "largest_component_face_ratio": round(self.largest_component_face_ratio, 4),
            "boundary_edge_count": self.boundary_edge_count,
            "non_manifold_edge_count": self.non_manifold_edge_count,
            "tiny_component_count": self.tiny_component_count,
            "recommended_policy": self.recommended_policy,
            # extended diagnostics.
            "vertex_count": self.vertex_count,
            "edge_count": self.edge_count,
            "face_count": self.face_count,
            "largest_component_face_count": self.largest_component_face_count,
            "tiny_component_face_ratio": round(self.tiny_component_face_ratio, 4),
            "degenerate_face_count": self.degenerate_face_count,
            "duplicate_vertex_count": self.duplicate_vertex_count,
            "near_duplicate_vertex_count": self.near_duplicate_vertex_count,
            "duplicate_face_count": self.duplicate_face_count,
            "very_small_triangle_count": self.very_small_triangle_count,
            "material_boundary_edge_count": self.material_boundary_edge_count,
            "uv_seam_edge_count": self.uv_seam_edge_count,
            "sharp_edge_count": self.sharp_edge_count,
            "face_area": self.face_area,
            "needs_cleanup": self.needs_cleanup,
            "cleanup_reasons": self.cleanup_reasons,
        }


def recommend_component_policy(
    component_count: int,
    largest_component_face_ratio: float,
    tiny_component_count: int,
    *,
    tiny_min_count: int = 2,
) -> str:
    """Pick the component-handling policy the retry ladder should use (plan §5/§6).

    This is the function "diagnosis 결과가 retry policy 선택에 사용됨" refers to: a
    single mesh fact -> policy mapping, unit-tested in isolation and reused by both
    the report and (later) the DM5 retry loop.

    - one shell -> ``preserve_all`` (nothing to budget)
    - a dominant shell plus several tiny detached shells -> ``component_budget``
      (spread the face budget by importance; the anchor's 25-component / 20-tiny
      plateau lands here)
    - an overwhelmingly dominant shell with only negligible debris -> ``largest_only``
    """
    if component_count <= 1:
        return POLICY_PRESERVE_ALL
    if tiny_component_count >= tiny_min_count and largest_component_face_ratio >= 0.5:
        return POLICY_COMPONENT_BUDGET
    if largest_component_face_ratio >= 0.98:
        return POLICY_LARGEST_ONLY
    return POLICY_PRESERVE_ALL


def _component_face_counts(mesh: MeshGraph) -> Counter:
    """face counts per vertex-connected shell (root vertex -> face count).

    Union-find over vertices, unioning each face's vertices, so faces that share
    only a vertex (not an edge) still count as one shell -- the robust notion of a
    "detached component" for non-manifold inputs. Only shells that contain faces
    are returned (isolated vertices are ignored)."""
    parent = list(range(mesh.vertex_count))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path halving
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for f in mesh.faces:
        vids = f.vertex_ids
        first = vids[0]
        for vid in vids[1:]:
            union(first, vid)

    counts: Counter = Counter()
    for f in mesh.faces:
        counts[find(f.vertex_ids[0])] += 1
    return counts


def _area_distribution(areas: np.ndarray) -> dict:
    if areas.size == 0:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "median": 0.0, "p10": 0.0, "p90": 0.0}
    p10, median, p90 = (float(x) for x in np.percentile(areas, [10, 50, 90]))
    return {
        "min": round(float(areas.min()), 9),
        "max": round(float(areas.max()), 9),
        "mean": round(float(areas.mean()), 9),
        "median": round(median, 9),
        "p10": round(p10, 9),
        "p90": round(p90, 9),
    }


def _bbox_diagonal(coords: np.ndarray) -> float:
    if coords.size == 0:
        return 0.0
    return float(np.linalg.norm(coords.max(axis=0) - coords.min(axis=0)))


def diagnose_topology(
    mesh: MeshGraph,
    *,
    tiny_face_fraction: float = DEFAULT_TINY_FACE_FRACTION,
    small_tri_fraction: float = DEFAULT_SMALL_TRI_FRACTION,
    near_dup_fraction: float = DEFAULT_NEAR_DUP_FRACTION,
    sharp_angle_deg: float = DEFAULT_SHARP_ANGLE_DEG,
) -> DiagnosisReport:
    """Run the DM2 pre-process diagnosis on ``mesh`` (plan §5).

    All "small / tiny / near-duplicate" thresholds are *relative* so the same
    defaults work across mesh scales: a component is tiny below
    ``tiny_face_fraction`` of total faces, a triangle is very small below
    ``small_tri_fraction`` of the median face area, and vertices are near-duplicate
    within ``near_dup_fraction`` of the bounding-box diagonal.
    """
    face_count = mesh.face_count
    if face_count == 0:
        return DiagnosisReport(
            component_count=0,
            largest_component_face_ratio=0.0,
            boundary_edge_count=0,
            non_manifold_edge_count=0,
            tiny_component_count=0,
            recommended_policy=POLICY_PRESERVE_ALL,
            vertex_count=mesh.vertex_count,
            edge_count=mesh.edge_count,
            face_count=0,
            face_area=_area_distribution(np.array([])),
            cleanup_reasons=["empty mesh: no faces"],
            needs_cleanup=True,
        )

    # -- components --------------------------------------------------------
    comp_counts = _component_face_counts(mesh)
    component_count = len(comp_counts)
    largest_component_face_count = max(comp_counts.values())
    largest_component_face_ratio = largest_component_face_count / face_count
    tiny_threshold = max(1.0, tiny_face_fraction * face_count)
    tiny_components = [c for c in comp_counts.values() if c < tiny_threshold]
    tiny_component_count = len(tiny_components)
    tiny_component_face_ratio = sum(tiny_components) / face_count

    # -- edges -------------------------------------------------------------
    boundary_edge_count = sum(1 for e in mesh.edges if e.is_boundary)
    non_manifold_edge_count = sum(1 for e in mesh.edges if e.is_non_manifold)
    uv_seam_edge_count = sum(1 for e in mesh.edges if e.is_seam)
    sharp_edge_count = sum(
        1
        for e in mesh.edges
        if e.is_sharp or (len(e.face_ids) == 2 and e.dihedral_angle >= sharp_angle_deg)
    )
    material_boundary_edge_count = sum(
        1
        for e in mesh.edges
        if len(e.face_ids) == 2
        and mesh.faces[e.face_ids[0]].material_index != mesh.faces[e.face_ids[1]].material_index
    )

    # -- areas / degeneracy ------------------------------------------------
    areas = np.array([f.area_3d for f in mesh.faces], dtype=float)
    coords = np.array([v.co for v in mesh.vertices], dtype=float)
    bbox_diag = _bbox_diagonal(coords)
    area_scale = bbox_diag * bbox_diag
    degenerate_eps = 1e-10 * area_scale if area_scale > 0 else 1e-12
    degenerate_face_count = int(np.count_nonzero(areas <= degenerate_eps))

    nonzero = areas[areas > degenerate_eps]
    median_area = float(np.median(nonzero)) if nonzero.size else 0.0
    small_area = small_tri_fraction * median_area
    is_tri = np.array([len(f.vertex_ids) == 3 for f in mesh.faces])
    very_small_triangle_count = int(np.count_nonzero(is_tri & (areas < small_area))) if median_area > 0 else 0

    # -- duplicate vertices / faces ---------------------------------------
    duplicate_vertex_count = near_duplicate_vertex_count = 0
    if coords.size:
        unique_exact = len({tuple(c) for c in coords})
        duplicate_vertex_count = mesh.vertex_count - unique_exact
        if bbox_diag > 0:
            tol = near_dup_fraction * bbox_diag
            quantized = np.round(coords / tol).astype(np.int64)
            unique_near = len({tuple(r) for r in quantized})
        else:
            unique_near = unique_exact
        near_duplicate_vertex_count = mesh.vertex_count - unique_near

    face_keys = [tuple(sorted(f.vertex_ids)) for f in mesh.faces]
    duplicate_face_count = face_count - len(set(face_keys))

    # -- cleanup signal ----------------------------------------------------
    cleanup_reasons: list[str] = []
    if non_manifold_edge_count:
        cleanup_reasons.append(f"{non_manifold_edge_count} non-manifold edges")
    if degenerate_face_count:
        cleanup_reasons.append(f"{degenerate_face_count} degenerate faces")
    if duplicate_face_count:
        cleanup_reasons.append(f"{duplicate_face_count} duplicate faces")
    if duplicate_vertex_count:
        cleanup_reasons.append(f"{duplicate_vertex_count} duplicate vertices")
    needs_cleanup = bool(cleanup_reasons)

    recommended_policy = recommend_component_policy(
        component_count, largest_component_face_ratio, tiny_component_count
    )

    return DiagnosisReport(
        component_count=component_count,
        largest_component_face_ratio=largest_component_face_ratio,
        boundary_edge_count=boundary_edge_count,
        non_manifold_edge_count=non_manifold_edge_count,
        tiny_component_count=tiny_component_count,
        recommended_policy=recommended_policy,
        vertex_count=mesh.vertex_count,
        edge_count=mesh.edge_count,
        face_count=face_count,
        largest_component_face_count=largest_component_face_count,
        tiny_component_face_ratio=tiny_component_face_ratio,
        degenerate_face_count=degenerate_face_count,
        duplicate_vertex_count=duplicate_vertex_count,
        near_duplicate_vertex_count=near_duplicate_vertex_count,
        duplicate_face_count=duplicate_face_count,
        very_small_triangle_count=very_small_triangle_count,
        material_boundary_edge_count=material_boundary_edge_count,
        uv_seam_edge_count=uv_seam_edge_count,
        sharp_edge_count=sharp_edge_count,
        face_area=_area_distribution(areas),
        needs_cleanup=needs_cleanup,
        cleanup_reasons=cleanup_reasons,
    )
