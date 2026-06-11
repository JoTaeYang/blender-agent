"""Blender feature analysis + hard-edge marking (retopology plan §6.1, §10 Phase 5).

Two jobs, both only inside Blender:

- :func:`mark_sharp_edges_by_angle` flags edges above a dihedral threshold (and
  open boundaries) as *sharp*, which is what QuadriFlow's ``use_preserve_sharp``
  reads -- so the remesh keeps panel lines / creases. This is the production way
  to "preserve hard edges" (plan §10 Phase 5).
- :func:`analyze_features_blender` summarizes a mesh's features (hard-edge ratio,
  curvature) directly from a ``bmesh``, sampling edges so it scales to
  multi-million-edge inputs without building a full Python mesh graph.
"""

from __future__ import annotations

import math

DEFAULT_FEATURE_ANGLE = 30.0


def mark_sharp_edges_by_angle(obj, angle_deg: float = DEFAULT_FEATURE_ANGLE) -> int:
    """Mark edges sharp where the dihedral angle >= ``angle_deg`` (open boundaries
    are always sharp). Returns the number marked. Enables QuadriFlow sharp
    preservation."""
    import bmesh

    mesh = obj.data
    threshold = math.radians(angle_deg)
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.edges.ensure_lookup_table()
        marked = 0
        for e in bm.edges:
            if len(e.link_faces) == 2:
                sharp = e.calc_face_angle(0.0) >= threshold
            else:
                sharp = True  # boundary / non-manifold -> feature
            e.smooth = not sharp
            if sharp:
                marked += 1
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()
    return marked


def analyze_features_blender(
    obj,
    angle_deg: float = DEFAULT_FEATURE_ANGLE,
    *,
    max_sample_edges: int = 300_000,
) -> dict:
    """Summarize hard-surface features of ``obj`` (plan §6.1).

    Iterating millions of edges in Python is slow, so edges are sampled by a
    fixed stride for the statistics; the reported ratios are estimates over that
    sample (``sampled_edge_count``), while ``edge_count`` is exact.
    """
    import bmesh

    threshold = math.radians(angle_deg)
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.edges.ensure_lookup_table()
        n = len(bm.edges)
        step = max(1, n // max_sample_edges)

        sampled = hard = boundary = 0
        sum_deg = 0.0
        max_deg = 0.0
        for i in range(0, n, step):
            e = bm.edges[i]
            sampled += 1
            if len(e.link_faces) == 2:
                deg = math.degrees(e.calc_face_angle(0.0))
                sum_deg += deg
                max_deg = max(max_deg, deg)
                if deg >= angle_deg:
                    hard += 1
            else:
                boundary += 1
                hard += 1
    finally:
        bm.free()

    return {
        "angle_threshold_deg": angle_deg,
        "edge_count": n,
        "sampled_edge_count": sampled,
        "hard_edge_count_sampled": hard,
        "hard_edge_ratio": round(hard / sampled, 4) if sampled else 0.0,
        "boundary_edge_count_sampled": boundary,
        "mean_dihedral_deg": round(sum_deg / max(sampled - boundary, 1), 3),
        "max_dihedral_deg": round(max_deg, 3),
    }
