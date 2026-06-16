"""Seam Decision Core — distortion-driven refinement helpers (RULE_BASED_UV_SEAM_CORE_PLAN §5.2).

The plan is explicit (§5, §12.5): do NOT rewrite the refinement loop. ``chart_uv_agent.pipeline``
already runs the correct distortion-driven, one-worst-island-per-round loop. This module
factors out the *pure decisions* that loop makes so they are reusable and unit-testable
without Blender, and so a reviewer-facing layer can call them directly:

    - which island is the worst (the refine target),
    - whether the global + worst-island distortion pass the bar (stop condition),
    - whether an added seam improved distortion enough to keep (the §5.2 accept rule).

Hard guard-rails the plan repeats (§5.2): one worst island per round; never split for
packing or convexity alone; revert a split whose distortion improvement is below
``min_improvement_ratio``; the threshold is the WORST-island distortion, not the global mean
(a reviewer sees the worst island in the checker, not the average).
"""

from __future__ import annotations

from dataclasses import dataclass

from artist_uv_agent.seam_policy import SeamPolicyConfig
from uv_agent.geometry.evaluation import island_distortion_summary, per_face_stretch
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


def island_distortion(mesh: MeshGraph, face_ids, face_stretch) -> float:
    """Area-weighted mean per-face stretch over one island — the checker distortion of that
    island. Identical metric to ``chart_uv_agent.pipeline._chart_distortion`` so the two never
    disagree about which island is worst."""
    area = sum(mesh.faces[f].area_3d for f in face_ids)
    if area <= 1e-12:
        return 0.0
    return sum(face_stretch[f] * mesh.faces[f].area_3d for f in face_ids) / area


def pick_worst_island(mesh: MeshGraph, uvmap: UVMap, islands):
    """Return ``(island_index, distortion)`` for the most-distorted island — the one and
    only island the loop is allowed to split this round (§5.2). ``islands`` is a list of
    face-id lists. Returns ``(-1, 0.0)`` when there are no faces."""
    fstr = per_face_stretch(mesh, uvmap)
    worst_idx, worst_val = -1, -1.0
    for cid, fs in enumerate(islands):
        if not fs:
            continue
        d = island_distortion(mesh, fs, fstr)
        if d > worst_val:
            worst_idx, worst_val = cid, d
    return worst_idx, max(0.0, worst_val)


@dataclass
class RefinementVerdict:
    """Whether the current layout passes the distortion bar, and if not which island to
    refine (RULE_BASED_UV_SEAM_CORE_PLAN §5.2 recommended judgement)."""

    passed: bool
    global_distortion: float
    worst_island_distortion: float
    worst_island_id: int
    global_threshold: float
    island_threshold: float

    def to_dict(self) -> dict:
        return {"passed": self.passed,
                "global_checker_distortion": round(self.global_distortion, 6),
                "worst_island_distortion": round(self.worst_island_distortion, 6),
                "worst_island_id": self.worst_island_id,
                "global_threshold": self.global_threshold,
                "island_threshold": self.island_threshold}


def evaluate_distortion(mesh: MeshGraph, uvmap: UVMap, islands, *,
                        global_threshold: float, island_threshold: float) -> RefinementVerdict:
    """The §5.2 stop test: pass iff GLOBAL checker distortion ≤ ``global_threshold`` AND the
    WORST single island ≤ ``island_threshold``. A layout whose global mean passes but whose
    worst island is badly stretched does NOT pass (the reviewer sees that island in the
    checkerboard). The worst island is the refine target when it fails."""
    fstr = per_face_stretch(mesh, uvmap)
    all_faces = [f.id for f in mesh.faces]
    glob = island_distortion(mesh, all_faces, fstr)
    worst_idx, worst_val = -1, 0.0
    for cid, fs in enumerate(islands):
        if not fs:
            continue
        d = island_distortion(mesh, fs, fstr)
        if d > worst_val:
            worst_idx, worst_val = cid, d
    passed = glob <= global_threshold and worst_val <= island_threshold
    return RefinementVerdict(passed, glob, worst_val, worst_idx,
                             global_threshold, island_threshold)


def improvement_ratio(before: float, after: float) -> float:
    """Relative distortion reduction ``(before - after) / before`` (0 when ``before`` is ~0).
    Negative means the split made the island WORSE."""
    if before <= 1e-12:
        return 0.0
    return (before - after) / before


def accept_split(before: float, after: float, *, min_improvement_ratio: float) -> bool:
    """The §5.2 accept rule: keep an added seam only when it cut the target island's
    distortion by at least ``min_improvement_ratio``; otherwise the caller must revert (a
    higher island count with little distortion gain is forbidden)."""
    return improvement_ratio(before, after) >= min_improvement_ratio


def refinement_plan(mesh: MeshGraph, uvmap: UVMap, islands, *,
                    config: SeamPolicyConfig | None = None,
                    global_threshold: float | None = None) -> dict:
    """One-call diagnosis a reviewer / the loop can read: the per-island distortion summary,
    the stop verdict, and the refine target. ``island_threshold`` defaults to the policy's
    ``distortion_threshold``; ``global_threshold`` defaults to the same value (override to
    match the active gate's ``stretch_max``). Pure — measures, never mutates."""
    cfg = config or SeamPolicyConfig()
    g_thr = cfg.distortion_threshold if global_threshold is None else global_threshold
    verdict = evaluate_distortion(mesh, uvmap, islands,
                                  global_threshold=g_thr,
                                  island_threshold=cfg.distortion_threshold)
    summary = island_distortion_summary(mesh, uvmap, islands)
    return {
        "verdict": verdict.to_dict(),
        "islands": summary,
        "refine_target": verdict.worst_island_id if not verdict.passed else -1,
        "at_island_cap": len(islands) >= cfg.max_islands,
    }
