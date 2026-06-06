"""UV quality evaluation (plan §7.5 / Phase 6).

Every metric is computed deterministically from the mesh + UV map so an agent
run can be scored and compared. Faces are fan-triangulated for area/angle math.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.packing import island_bbox
from uv_agent.geometry.solution import UVMap
from uv_agent.planner.island_planner import IslandPlan


@dataclass
class Evaluation:
    overlap_ratio: float
    stretch_score: float
    angle_distortion: float
    texel_density_variance: float
    packing_efficiency: float
    seam_visibility_score: float
    island_count: int
    small_island_ratio: float
    status: str  # "accepted" | "needs_repair"

    def to_dict(self) -> dict:
        return asdict(self)


def _tris_from_face(loop_indices: list[int]):
    """Fan-triangulate a polygon's loops -> list of (l0, li, li+1) triples."""
    for i in range(1, len(loop_indices) - 1):
        yield loop_indices[0], loop_indices[i], loop_indices[i + 1]


def _tri_area_3d(p0, p1, p2) -> float:
    return 0.5 * float(np.linalg.norm(np.cross(p1 - p0, p2 - p0)))


def _tri_signed_area_uv(a, b, c) -> float:
    return 0.5 * float((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))


def _corner_angles(p0, p1, p2) -> list[float]:
    pts = [np.asarray(p0, dtype=float), np.asarray(p1, dtype=float), np.asarray(p2, dtype=float)]
    angles = []
    for i in range(3):
        a = pts[i]
        b = pts[(i + 1) % 3]
        c = pts[(i + 2) % 3]
        v1 = b - a
        v2 = c - a
        n1 = np.linalg.norm(v1)
        n2 = np.linalg.norm(v2)
        if n1 < 1e-12 or n2 < 1e-12:
            angles.append(0.0)
            continue
        cosang = float(np.clip(np.dot(v1, v2) / (n1 * n2), -1.0, 1.0))
        angles.append(math.acos(cosang))
    return angles


def evaluate_uv_solution(
    mesh: MeshGraph,
    plan: IslandPlan,
    uvmap: UVMap,
    *,
    stretch_threshold: float = 0.25,
    view_dir: tuple[float, float, float] = (0.0, -1.0, 0.0),
    small_island_area: float = 0.01,
) -> Evaluation:
    area3d_total = 0.0
    areauv_total = 0.0  # absolute
    flipped_uv_area = 0.0
    tri_records: list[tuple[float, float]] = []  # (area3d, |area_uv|)
    angle_diff_sum = 0.0
    angle_weight = 0.0

    for f in mesh.faces:
        for l0, l1, l2 in _tris_from_face(f.loop_indices):
            p0 = mesh.vertex_co(mesh.loops[l0].vertex_id)
            p1 = mesh.vertex_co(mesh.loops[l1].vertex_id)
            p2 = mesh.vertex_co(mesh.loops[l2].vertex_id)
            a3 = _tri_area_3d(p0, p1, p2)
            uv0, uv1, uv2 = uvmap.get(l0), uvmap.get(l1), uvmap.get(l2)
            signed_uv = _tri_signed_area_uv(uv0, uv1, uv2)
            auv = abs(signed_uv)
            area3d_total += a3
            areauv_total += auv
            if signed_uv < 0:
                flipped_uv_area += auv
            tri_records.append((a3, auv))

            # angle distortion (weighted by 3D area)
            ang3 = _corner_angles(p0, p1, p2)
            anguv = _corner_angles(uv0, uv1, uv2)
            d = sum(abs(a - b) for a, b in zip(ang3, anguv)) / 3.0
            angle_diff_sum += d * a3
            angle_weight += a3

    # overlap_ratio: fraction of UV area that is folded/flipped.
    overlap_ratio = (flipped_uv_area / areauv_total) if areauv_total > 1e-12 else 0.0

    # stretch_score: area-weighted area distortion after global scale match.
    scale_sq = (area3d_total / areauv_total) if areauv_total > 1e-12 else 1.0
    stretch_acc = 0.0
    for a3, auv in tri_records:
        if a3 < 1e-12:
            continue
        ratio = (auv * scale_sq) / a3  # ~1 means undistorted
        ratio = max(ratio, 1e-9)
        stretch_acc += abs(math.log(ratio)) * a3
    stretch_score = (stretch_acc / area3d_total) if area3d_total > 1e-12 else 0.0

    angle_distortion = (angle_diff_sum / angle_weight / math.pi) if angle_weight > 1e-12 else 0.0

    # texel density variance across islands (coefficient of variation).
    densities = []
    island_areas_uv = []
    for isl in plan.islands:
        if not isl.face_ids:
            continue
        a3 = sum(mesh.faces[fid].area_3d for fid in isl.face_ids)
        auv = 0.0
        for fid in isl.face_ids:
            for l0, l1, l2 in _tris_from_face(mesh.faces[fid].loop_indices):
                auv += abs(_tri_signed_area_uv(uvmap.get(l0), uvmap.get(l1), uvmap.get(l2)))
        island_areas_uv.append(auv)
        if a3 > 1e-12:
            densities.append(auv / a3)
    if len(densities) >= 2:
        mean_d = float(np.mean(densities))
        texel_density_variance = float(np.std(densities) / mean_d) if mean_d > 1e-12 else 0.0
    else:
        texel_density_variance = 0.0

    # packing efficiency: used UV area / area of the global bounding box.
    boxes = [island_bbox(mesh, isl, uvmap) for isl in plan.islands if isl.face_ids]
    if boxes:
        umin = min(b[0] for b in boxes)
        vmin = min(b[1] for b in boxes)
        umax = max(b[2] for b in boxes)
        vmax = max(b[3] for b in boxes)
        bbox_area = max((umax - umin) * (vmax - vmin), 1e-12)
        packing_efficiency = float(min(1.0, areauv_total / bbox_area))
    else:
        packing_efficiency = 0.0

    # small island ratio.
    n_islands = len([i for i in plan.islands if i.face_ids])
    small = sum(1 for a in island_areas_uv if a < small_island_area)
    small_island_ratio = (small / n_islands) if n_islands else 0.0

    seam_visibility_score = _seam_visibility(mesh, plan, view_dir)

    accepted = (
        overlap_ratio <= max(plan.constraints.max_overlap_ratio, 1e-6)
        and stretch_score <= stretch_threshold
    )
    status = "accepted" if accepted else "needs_repair"

    return Evaluation(
        overlap_ratio=round(overlap_ratio, 6),
        stretch_score=round(stretch_score, 6),
        angle_distortion=round(angle_distortion, 6),
        texel_density_variance=round(texel_density_variance, 6),
        packing_efficiency=round(packing_efficiency, 6),
        seam_visibility_score=round(seam_visibility_score, 6),
        island_count=n_islands,
        small_island_ratio=round(small_island_ratio, 6),
        status=status,
    )


def per_island_metrics(mesh: MeshGraph, face_ids: list[int], uvmap: UVMap) -> dict:
    """Overlap (fold) ratio and area-stretch for a single island, so the agent
    can decide which island to repair."""
    area3d_total = 0.0
    areauv_total = 0.0
    flipped = 0.0
    recs: list[tuple[float, float]] = []
    for fid in face_ids:
        for l0, l1, l2 in _tris_from_face(mesh.faces[fid].loop_indices):
            p0 = mesh.vertex_co(mesh.loops[l0].vertex_id)
            p1 = mesh.vertex_co(mesh.loops[l1].vertex_id)
            p2 = mesh.vertex_co(mesh.loops[l2].vertex_id)
            a3 = _tri_area_3d(p0, p1, p2)
            s = _tri_signed_area_uv(uvmap.get(l0), uvmap.get(l1), uvmap.get(l2))
            area3d_total += a3
            areauv_total += abs(s)
            if s < 0:
                flipped += abs(s)
            recs.append((a3, abs(s)))
    overlap = (flipped / areauv_total) if areauv_total > 1e-12 else 0.0
    scale_sq = (area3d_total / areauv_total) if areauv_total > 1e-12 else 1.0
    acc = 0.0
    for a3, auv in recs:
        if a3 < 1e-12:
            continue
        ratio = max((auv * scale_sq) / a3, 1e-9)
        acc += abs(math.log(ratio)) * a3
    stretch = (acc / area3d_total) if area3d_total > 1e-12 else 0.0
    return {"overlap_ratio": round(overlap, 6), "stretch_score": round(stretch, 6)}


def _seam_visibility(mesh: MeshGraph, plan: IslandPlan, view_dir) -> float:
    """Proxy for plan §7.5 seam_visibility_score.

    Approximates how exposed the seams are to the camera: the length-weighted
    fraction of seam edges whose adjacent faces face toward ``view_dir``.
    A real implementation would use the project camera + material masks.
    """
    view = np.asarray(view_dir, dtype=float)
    view /= np.linalg.norm(view) or 1.0
    total = 0.0
    visible = 0.0
    for eid in plan.seam_edge_ids:
        e = mesh.edges[eid]
        a = mesh.vertex_co(e.vertex_ids[0])
        b = mesh.vertex_co(e.vertex_ids[1])
        length = float(np.linalg.norm(b - a))
        if length < 1e-12:
            continue
        total += length
        facing = 0.0
        for fid in e.face_ids:
            n = np.asarray(mesh.faces[fid].normal, dtype=float)
            facing = max(facing, float(np.dot(n, -view)))  # normal pointing at camera
        if facing > 0:
            visible += length * facing
    return (visible / total) if total > 1e-12 else 0.0
