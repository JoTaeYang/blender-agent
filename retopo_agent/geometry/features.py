"""Feature detection for feature-aware retopology (retopology plan §6.1, §6.3, §10 Phase 5).

Phase 5 preserves the edges and regions that carry a model's shape: hard-surface
panel lines, silhouette/open boundaries, material seams, and high-curvature areas.
This module finds them on a :class:`~uv_agent.geometry.mesh_graph.MeshGraph` using
the per-edge dihedral angles the graph already carries, so it needs no Blender and
is unit-testable. The result drives:

- :func:`retopo_agent.geometry.decimate.feature_aware_decimate`, which keeps
  feature vertices and collapses flat regions (offline); and
- the Blender path, which marks the same hard edges sharp so QuadriFlow's
  ``use_preserve_sharp`` keeps them.

A vertex's *feature score* is the maximum dihedral angle over its incident edges
(boundary edges count as a hard 180 deg), so corners and creases score high and
flat interiors score ~0. ``feature_vertex_mask`` thresholds that score.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph

DEFAULT_FEATURE_ANGLE = 30.0
_BOUNDARY_SCORE = 180.0


def detect_hard_edges(mesh: MeshGraph, *, angle_threshold: float = DEFAULT_FEATURE_ANGLE) -> list[int]:
    """Edge ids whose dihedral angle is at least ``angle_threshold`` (or that are
    open boundaries -- silhouette edges)."""
    return [e.id for e in mesh.edges if e.is_boundary or e.dihedral_angle >= angle_threshold]


def material_boundary_edges(mesh: MeshGraph) -> list[int]:
    """Edge ids separating faces with different material indices (plan §6.3)."""
    out: list[int] = []
    for e in mesh.edges:
        if len(e.face_ids) == 2:
            m0 = mesh.faces[e.face_ids[0]].material_index
            m1 = mesh.faces[e.face_ids[1]].material_index
            if m0 != m1:
                out.append(e.id)
    return out


def vertex_feature_scores(mesh: MeshGraph) -> np.ndarray:
    """Per-vertex feature score = max incident dihedral angle (deg); boundary
    edges contribute a hard 180."""
    scores = np.zeros(mesh.vertex_count, dtype=float)
    for e in mesh.edges:
        a, b = e.vertex_ids
        ang = _BOUNDARY_SCORE if e.is_boundary else e.dihedral_angle
        if ang > scores[a]:
            scores[a] = ang
        if ang > scores[b]:
            scores[b] = ang
    return scores


def feature_vertex_mask(mesh: MeshGraph, *, angle_threshold: float = DEFAULT_FEATURE_ANGLE) -> np.ndarray:
    """Boolean mask (len == vertex_count): True where the vertex is on a feature
    (hard edge, boundary, or high curvature)."""
    return vertex_feature_scores(mesh) >= angle_threshold


@dataclass
class FeatureReport:
    vertex_count: int
    edge_count: int
    angle_threshold: float
    hard_edge_count: int
    boundary_edge_count: int
    material_boundary_edge_count: int
    feature_vertex_count: int
    flat_vertex_count: int
    curvature_mean_deg: float
    curvature_max_deg: float

    @property
    def feature_vertex_ratio(self) -> float:
        return self.feature_vertex_count / self.vertex_count if self.vertex_count else 0.0

    def to_dict(self) -> dict:
        return {
            "vertex_count": self.vertex_count,
            "edge_count": self.edge_count,
            "angle_threshold_deg": self.angle_threshold,
            "hard_edge_count": self.hard_edge_count,
            "boundary_edge_count": self.boundary_edge_count,
            "material_boundary_edge_count": self.material_boundary_edge_count,
            "feature_vertex_count": self.feature_vertex_count,
            "feature_vertex_ratio": round(self.feature_vertex_ratio, 4),
            "flat_vertex_count": self.flat_vertex_count,
            "curvature_mean_deg": round(self.curvature_mean_deg, 3),
            "curvature_max_deg": round(self.curvature_max_deg, 3),
        }


def analyze_features(mesh: MeshGraph, *, angle_threshold: float = DEFAULT_FEATURE_ANGLE) -> FeatureReport:
    """Summarize a mesh's shape-defining features (plan §6.1 mesh analysis)."""
    scores = vertex_feature_scores(mesh)
    mask = scores >= angle_threshold
    interior_dihedrals = [e.dihedral_angle for e in mesh.edges if not e.is_boundary]
    return FeatureReport(
        vertex_count=mesh.vertex_count,
        edge_count=mesh.edge_count,
        angle_threshold=angle_threshold,
        hard_edge_count=len(detect_hard_edges(mesh, angle_threshold=angle_threshold)),
        boundary_edge_count=sum(1 for e in mesh.edges if e.is_boundary),
        material_boundary_edge_count=len(material_boundary_edges(mesh)),
        feature_vertex_count=int(mask.sum()),
        flat_vertex_count=int((~mask).sum()),
        curvature_mean_deg=float(np.mean(interior_dihedrals)) if interior_dihedrals else 0.0,
        curvature_max_deg=float(np.max(interior_dihedrals)) if interior_dihedrals else 0.0,
    )


def plan_feature_preservation(
    mesh: MeshGraph,
    *,
    angle_threshold: float = DEFAULT_FEATURE_ANGLE,
    target_level: str | None = None,
) -> dict:
    """Build a feature-preservation plan (plan §6.3 schema): which regions to
    protect and why. Derived deterministically from :func:`analyze_features`."""
    report = analyze_features(mesh, angle_threshold=angle_threshold)
    preserve_regions: list[dict] = []
    if report.hard_edge_count - report.boundary_edge_count > 0:
        preserve_regions.append({
            "region_id": "hard_edges",
            "priority": "high",
            "reason": "hard-surface feature / panel line",
            "edge_count": report.hard_edge_count - report.boundary_edge_count,
        })
    if report.boundary_edge_count > 0:
        preserve_regions.append({
            "region_id": "open_boundary",
            "priority": "high",
            "reason": "silhouette / open boundary",
            "edge_count": report.boundary_edge_count,
        })
    if report.material_boundary_edge_count > 0:
        preserve_regions.append({
            "region_id": "material_boundaries",
            "priority": "medium",
            "reason": "material boundary",
            "edge_count": report.material_boundary_edge_count,
        })
    return {
        "target_level": target_level,
        "angle_threshold_deg": angle_threshold,
        "feature_vertex_count": report.feature_vertex_count,
        "preserve_regions": preserve_regions,
        "details_to_bake": [],  # small flat details -> normal bake (deferred)
    }
