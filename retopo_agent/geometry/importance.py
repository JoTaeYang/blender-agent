"""Importance map for feature-aware decimation (Decimation plan DM4, §7).

The core of ZBrush-style decimation is *not* reducing every region by the same
ratio: hard edges, silhouette, curvature, seams and material borders carry the
model's shape and must be kept dense, while flat areas can collapse hard (plan
§7). This module turns a :class:`~uv_agent.geometry.mesh_graph.MeshGraph` into a
continuous **importance map** -- a value in ``[0, 1]`` per vertex, edge and face
-- combining the plan §7 importance sources:

- curvature (graded dihedral angle), and the binary hard-edge threshold,
- open boundary and non-manifold boundary,
- material boundary and UV seam,
- sharp-normal boundary,
- face-area percentile (small faces = fine detail = important),
- an optional user vertex weight group.

Each source contributes a per-element value in ``[0, 1]``; they are combined by a
weighted **soft-OR** (the max of the weighted contributions), so importance means
"the strongest reason to keep this element" and a feature lit by any source
saturates to ~1 while a flat interior stays ~0. Edges carry the feature signals,
vertices take the max over their incident edges (plus area / user weight), and
faces take the max over their vertices.

It runs on a ``MeshGraph`` so it is unit-tested offline; the Blender adapter is
:mod:`retopo_agent.blender.importance`. Short term the map drives the Decimate
Collapse vertex group (feature regions decimated less); mid term it becomes the
importance penalty in the DM6 custom QEM collapse.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph

DEFAULT_ANGLE_THRESHOLD = 30.0  # dihedral >= this is a hard edge
DEFAULT_CURVATURE_FULL_DEG = 60.0  # dihedral at which graded curvature saturates to 1

# Importance source keys (plan §7 "importance source").
SRC_CURVATURE = "curvature"
SRC_HARD_EDGE = "hard_edge"
SRC_BOUNDARY = "boundary"
SRC_NON_MANIFOLD = "non_manifold"
SRC_MATERIAL_BOUNDARY = "material_boundary"
SRC_UV_SEAM = "uv_seam"
SRC_SHARP_NORMAL = "sharp_normal"
SRC_FACE_AREA = "face_area"
SRC_USER_GROUP = "user_group"

# Per-source weights: binary structural features dominate; material/seam slightly
# lower; face-area is a soft tie-breaker. All in [0, 1] so the soft-OR stays in range.
DEFAULT_WEIGHTS: dict[str, float] = {
    SRC_CURVATURE: 1.0,
    SRC_HARD_EDGE: 1.0,
    SRC_BOUNDARY: 1.0,
    SRC_NON_MANIFOLD: 1.0,
    SRC_MATERIAL_BOUNDARY: 0.9,
    SRC_UV_SEAM: 0.9,
    SRC_SHARP_NORMAL: 1.0,
    SRC_FACE_AREA: 0.6,
    SRC_USER_GROUP: 1.0,
}
ALL_SOURCES: tuple[str, ...] = tuple(DEFAULT_WEIGHTS.keys())


@dataclass
class ImportanceMap:
    """Per-element importance in ``[0, 1]`` plus which sources actually fired."""

    vertex_importance: np.ndarray  # shape (vertex_count,)
    edge_importance: np.ndarray  # shape (edge_count,)
    face_importance: np.ndarray  # shape (face_count,)
    sources: dict[str, bool]  # source -> enabled AND contributed somewhere
    weights: dict[str, float]

    @staticmethod
    def _stats(arr: np.ndarray) -> dict:
        if arr.size == 0:
            return {"min": 0.0, "mean": 0.0, "max": 0.0}
        return {
            "min": round(float(arr.min()), 4),
            "mean": round(float(arr.mean()), 4),
            "max": round(float(arr.max()), 4),
        }

    @property
    def importance_stats(self) -> dict:
        return self._stats(self.vertex_importance)

    def feature_vertex_mask(self, threshold: float = 0.5) -> np.ndarray:
        """Boolean per-vertex mask of "important" vertices (importance >= threshold),
        compatible with :func:`retopo_agent.geometry.decimate.feature_aware_decimate`."""
        return self.vertex_importance >= threshold

    def to_dict(self) -> dict:
        return {
            "importance_stats": self.importance_stats,  # plan §7 contract (vertices)
            "edge_importance_stats": self._stats(self.edge_importance),
            "face_importance_stats": self._stats(self.face_importance),
            "sources": self.sources,
            "weights": self.weights,
            "vertex_count": int(self.vertex_importance.size),
            "edge_count": int(self.edge_importance.size),
            "face_count": int(self.face_importance.size),
        }


def compute_importance_map(
    mesh: MeshGraph,
    *,
    angle_threshold: float = DEFAULT_ANGLE_THRESHOLD,
    curvature_full_deg: float = DEFAULT_CURVATURE_FULL_DEG,
    sharp_angle: float | None = None,
    user_vertex_weights=None,
    weights: dict[str, float] | None = None,
    enabled_sources=None,
) -> ImportanceMap:
    """Compute the per-vertex/edge/face importance map of ``mesh`` (plan §7).

    ``angle_threshold`` is the hard-edge dihedral; ``curvature_full_deg`` is the
    dihedral at which the *graded* curvature term saturates to 1. ``sharp_angle``
    defaults to ``angle_threshold``. ``user_vertex_weights`` is an optional
    per-vertex array (clamped to ``[0, 1]``) folded in as the ``user_group`` source.
    ``weights`` overrides individual source weights; ``enabled_sources`` restricts
    which sources contribute (defaults to all).

    Combination is a weighted soft-OR (max), so importance stays in ``[0, 1]`` and
    expresses the strongest feature signal at each element.
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    enabled = set(ALL_SOURCES if enabled_sources is None else enabled_sources)
    if sharp_angle is None:
        sharp_angle = angle_threshold

    n_v, n_e, n_f = mesh.vertex_count, mesh.edge_count, mesh.face_count
    vert_imp = np.zeros(n_v, dtype=float)
    edge_imp = np.zeros(n_e, dtype=float)
    face_imp = np.zeros(n_f, dtype=float)
    hit = {k: False for k in ALL_SOURCES}

    if n_f == 0 or n_v == 0:
        return ImportanceMap(vert_imp, edge_imp, face_imp, {k: False for k in ALL_SOURCES}, w)

    # -- edge-based sources -> edge importance, propagated to endpoint vertices --
    for e in mesh.edges:
        two_faced = len(e.face_ids) == 2
        best = 0.0
        if SRC_CURVATURE in enabled and two_faced and curvature_full_deg > 0:
            c = min(1.0, e.dihedral_angle / curvature_full_deg) * w[SRC_CURVATURE]
            if c > 0:
                hit[SRC_CURVATURE] = True
            best = max(best, c)
        if SRC_HARD_EDGE in enabled and two_faced and e.dihedral_angle >= angle_threshold:
            best = max(best, w[SRC_HARD_EDGE])
            hit[SRC_HARD_EDGE] = True
        if SRC_BOUNDARY in enabled and e.is_boundary:
            best = max(best, w[SRC_BOUNDARY])
            hit[SRC_BOUNDARY] = True
        if SRC_NON_MANIFOLD in enabled and e.is_non_manifold:
            best = max(best, w[SRC_NON_MANIFOLD])
            hit[SRC_NON_MANIFOLD] = True
        if SRC_MATERIAL_BOUNDARY in enabled and two_faced and (
            mesh.faces[e.face_ids[0]].material_index != mesh.faces[e.face_ids[1]].material_index
        ):
            best = max(best, w[SRC_MATERIAL_BOUNDARY])
            hit[SRC_MATERIAL_BOUNDARY] = True
        if SRC_UV_SEAM in enabled and e.is_seam:
            best = max(best, w[SRC_UV_SEAM])
            hit[SRC_UV_SEAM] = True
        if SRC_SHARP_NORMAL in enabled and (
            e.is_sharp or (two_faced and e.dihedral_angle >= sharp_angle)
        ):
            best = max(best, w[SRC_SHARP_NORMAL])
            hit[SRC_SHARP_NORMAL] = True

        best = min(1.0, best)
        edge_imp[e.id] = best
        a, b = e.vertex_ids
        if best > vert_imp[a]:
            vert_imp[a] = best
        if best > vert_imp[b]:
            vert_imp[b] = best

    # -- face-area percentile -> small faces are fine detail (plan §7) -----------
    if SRC_FACE_AREA in enabled:
        areas = np.array([f.area_3d for f in mesh.faces], dtype=float)
        positive = areas[areas > 0]
        median = float(np.median(positive)) if positive.size else 0.0
        if median > 0:
            area_contrib = np.clip((median - areas) / median, 0.0, 1.0) * w[SRC_FACE_AREA]
            for f in mesh.faces:
                ac = float(area_contrib[f.id])
                if ac <= 0:
                    continue
                hit[SRC_FACE_AREA] = True
                ac = min(1.0, ac)
                for vid in f.vertex_ids:
                    if ac > vert_imp[vid]:
                        vert_imp[vid] = ac

    # -- optional user vertex group ---------------------------------------------
    if SRC_USER_GROUP in enabled and user_vertex_weights is not None:
        uw = np.clip(np.asarray(user_vertex_weights, dtype=float), 0.0, 1.0) * w[SRC_USER_GROUP]
        m = min(uw.size, n_v)
        vert_imp[:m] = np.maximum(vert_imp[:m], np.minimum(1.0, uw[:m]))
        if np.any(uw[:m] > 0):
            hit[SRC_USER_GROUP] = True

    np.clip(vert_imp, 0.0, 1.0, out=vert_imp)

    # -- face importance = max vertex importance over the face's vertices --------
    for f in mesh.faces:
        face_imp[f.id] = max(vert_imp[vid] for vid in f.vertex_ids)

    sources = {k: (k in enabled and hit[k]) for k in ALL_SOURCES}
    return ImportanceMap(vert_imp, edge_imp, face_imp, sources, w)


def importance_to_vertex_weights(importance, strength: float = 1.0) -> np.ndarray:
    """Map importance to Decimate-Collapse vertex-group weights (plan §7 short term).

    ``strength`` (``preserve_features_strength``) sharpens or softens protection:
    ``strength > 1`` pushes weights up (protect more aggressively), ``strength < 1``
    relaxes them. Implemented as ``importance ** (1/strength)`` on ``[0, 1]`` (a
    monotone gamma curve, fixed points at 0 and 1). ``strength <= 0`` -> no
    protection. The result stays in ``[0, 1]``.
    """
    imp = np.clip(np.asarray(importance, dtype=float), 0.0, 1.0)
    if strength <= 0:
        return np.zeros_like(imp)
    if strength == 1.0:
        return imp
    return np.clip(np.power(imp, 1.0 / strength), 0.0, 1.0)
