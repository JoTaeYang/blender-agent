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


def per_face_stretch(mesh: MeshGraph, uvmap: UVMap) -> np.ndarray:
    """Per-face area-distortion (|log(uv_area*scale / 3d_area)|) after a global scale
    match (UV repair plan §3 Track 2 step 1). High values mark the faces a refinement
    seam should target. Returns an ``(n_faces,)`` array."""
    a3 = np.zeros(len(mesh.faces), dtype=float)
    auv = np.zeros(len(mesh.faces), dtype=float)
    for f in mesh.faces:
        s3 = s_uv = 0.0
        for l0, l1, l2 in _tris_from_face(f.loop_indices):
            p0 = mesh.vertex_co(mesh.loops[l0].vertex_id)
            p1 = mesh.vertex_co(mesh.loops[l1].vertex_id)
            p2 = mesh.vertex_co(mesh.loops[l2].vertex_id)
            s3 += _tri_area_3d(p0, p1, p2)
            s_uv += abs(_tri_signed_area_uv(uvmap.get(l0), uvmap.get(l1), uvmap.get(l2)))
        a3[f.id] = s3
        auv[f.id] = s_uv
    total3 = float(a3.sum())
    totaluv = float(auv.sum())
    scale_sq = (total3 / totaluv) if totaluv > 1e-12 else 1.0
    out = np.zeros(len(mesh.faces), dtype=float)
    for fid in range(len(mesh.faces)):
        if a3[fid] < 1e-12:
            continue
        ratio = max((auv[fid] * scale_sq) / a3[fid], 1e-9)
        out[fid] = abs(math.log(ratio))
    return out


def estimate_vt_count(mesh: MeshGraph, uvmap: UVMap, *, quantum: float = 1e-4) -> int:
    """Estimate the exported ``vt`` count: distinct (vertex, quantized-uv) corners.

    A vertex shared by faces with the same UV exports one ``vt``; a vertex split
    across a seam exports one per side. This mirrors what an OBJ writer emits, so
    ``vt / v`` measures seam proliferation against the reference's 1.13 (plan §5)."""
    seen: set[tuple[int, int, int]] = set()
    inv = 1.0 / quantum
    for loop in mesh.loops:
        u, v = uvmap.get(loop.index)
        seen.add((loop.vertex_id, int(round(u * inv)), int(round(v * inv))))
    return len(seen)


def uv_bounds_ok(uvmap: UVMap, *, lo: float = -1e-4, hi: float = 1.0 + 1e-4) -> bool:
    """All UVs within the [0,1] tile (plan §5 UDIM/0-1 bounds gate)."""
    if len(uvmap.uv) == 0:
        return True
    return bool(uvmap.uv.min() >= lo and uvmap.uv.max() <= hi)


def _point_in_triangle(tri, px, py):
    """Vectorised point-in-triangle (winding-agnostic) for pixel centres ``(px, py)``."""
    (x1, y1), (x2, y2), (x3, y3) = tri
    d1 = (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2)
    d2 = (px - x3) * (y2 - y3) - (x2 - x3) * (py - y3)
    d3 = (px - x1) * (y3 - y1) - (x3 - x1) * (py - y1)
    has_neg = (d1 < 0) | (d2 < 0) | (d3 < 0)
    has_pos = (d1 > 0) | (d2 > 0) | (d3 > 0)
    return ~(has_neg & has_pos)


def _erode(mask, iterations: int):
    """Binary erosion (4-neighbour). Removes ``iterations``-px-thick features — here the
    sub-pixel boundary-aliasing lines where tiling triangles share an edge, leaving only
    genuine interior-overlap blobs (the margin-px requirement)."""
    import numpy as np

    for _ in range(max(0, iterations)):
        m = mask.copy()
        m[1:, :] &= mask[:-1, :]
        m[:-1, :] &= mask[1:, :]
        m[:, 1:] &= mask[:, :-1]
        m[:, :-1] &= mask[:, 1:]
        mask = m
    return mask


def raster_overlap_diagnosis(mesh: MeshGraph, uvmap: UVMap, face_chart=None, *,
                             resolution: int = 1024, margin_px: int = 1) -> dict:
    """True (raster) UV overlap, with per-chart attribution (chart-UV plan correctness
    round). Rasterise every UV triangle onto a ``resolution``² grid by **pixel-centre**
    sampling — so sub-pixel boundary touches between adjacent triangles never register
    (the 1px-margin requirement) and only genuinely overlapping interiors share a pixel.

    A multi-occupied pixel is attributed ``cross`` (two DIFFERENT charts invade) or
    ``self`` (one chart self-intersects / folds), since the two need different repairs.
    Returns the ratios + the offending chart sets. ``raster_overlap_ratio`` =
    multi-occupied / occupied pixels (the gated number)."""
    R = int(resolution)
    count = np.zeros((R, R), dtype=np.int32)
    chart_of = np.full((R, R), -1, dtype=np.int32)
    cross = np.zeros((R, R), dtype=bool)
    self_charts: set[int] = set()
    cross_charts: set[int] = set()

    for f in mesh.faces:
        cid = int(face_chart.get(f.id, 0)) if face_chart is not None else 0
        for l0, l1, l2 in _tris_from_face(f.loop_indices):
            tri = np.array([uvmap.get(l0), uvmap.get(l1), uvmap.get(l2)], dtype=float) * R
            minx = max(0, int(np.floor(tri[:, 0].min())))
            maxx = min(R - 1, int(np.ceil(tri[:, 0].max())))
            miny = max(0, int(np.floor(tri[:, 1].min())))
            maxy = min(R - 1, int(np.ceil(tri[:, 1].max())))
            if maxx < minx or maxy < miny:
                continue
            xs = np.arange(minx, maxx + 1) + 0.5
            ys = np.arange(miny, maxy + 1) + 0.5
            px, py = np.meshgrid(xs, ys)
            mask = _point_in_triangle(tri, px, py)
            if not mask.any():
                continue
            sc = count[miny:maxy + 1, minx:maxx + 1]
            scid = chart_of[miny:maxy + 1, minx:maxx + 1]
            sx = cross[miny:maxy + 1, minx:maxx + 1]
            already = mask & (sc >= 1)
            diff = already & (scid != cid)
            if diff.any():
                cross_charts.add(cid)
                cross_charts.update(int(v) for v in np.unique(scid[diff]) if v >= 0)
            same = already & (scid == cid)
            if same.any():
                self_charts.add(cid)
            sx |= diff
            sc += mask
            scid[mask] = cid

    occupied = int((count >= 1).sum())
    # Erode by margin_px so 1px boundary-aliasing lines don't count as overlap; only
    # genuine interior-overlap blobs survive.
    multi_mask = _erode(count >= 2, margin_px)
    cross_mask = _erode(cross & (count >= 2), margin_px)
    multi = int(multi_mask.sum())
    cross_px = int((cross_mask & multi_mask).sum())
    self_px = int((multi_mask & ~cross_mask).sum())
    ratio = (multi / occupied) if occupied else 0.0
    return {
        "raster_overlap_ratio": round(ratio, 6),
        "self_overlap_ratio": round((self_px / occupied) if occupied else 0.0, 6),
        "cross_overlap_ratio": round((cross_px / occupied) if occupied else 0.0, 6),
        "occupied_px": occupied, "multi_px": multi,
        "self_px": self_px, "cross_px": cross_px,
        "self_charts": sorted(self_charts), "cross_charts": sorted(cross_charts),
        "resolution": R,
    }


def raster_overlap_ratio(mesh: MeshGraph, uvmap: UVMap, *, resolution: int = 1024) -> float:
    """The gated number: multi-occupied / occupied pixels (chart-UV correctness round)."""
    return raster_overlap_diagnosis(mesh, uvmap, None, resolution=resolution)["raster_overlap_ratio"]


def relative_small_island_ratio(mesh: MeshGraph, plan, uvmap: UVMap, *, frac: float = 0.2) -> float:
    """Fraction of islands whose UV area is below ``frac`` × the MEDIAN island area — a
    chart-count-invariant confetti measure (chart-UV plan §5b): it flags genuine size
    *disparity* (slivers far smaller than their siblings), not "many charts". The
    absolute-0.01 ``small_island_ratio`` rises just because there are more charts, which
    fights the convexity gate; this does not."""
    areas = []
    for isl in plan.islands:
        if not isl.face_ids:
            continue
        a = 0.0
        for fid in isl.face_ids:
            for l0, l1, l2 in _tris_from_face(mesh.faces[fid].loop_indices):
                a += abs(_tri_signed_area_uv(uvmap.get(l0), uvmap.get(l1), uvmap.get(l2)))
        areas.append(a)
    if not areas:
        return 0.0
    med = float(np.median(areas))
    if med <= 1e-12:
        return 0.0
    small = sum(1 for a in areas if a < frac * med)
    return small / len(areas)


def uv_islands_from_uvmap(mesh: MeshGraph, uvmap: UVMap, *, tol: float = 1e-5) -> list[list[int]]:
    """Recover UV islands from a UVMap alone (chart-UV plan U0): two adjacent faces are
    in the same island iff their shared edge carries the SAME UV on both sides. Lets us
    measure island_count / packing on Smart-UV and reference layouts where we have UVs
    but no seam set. Returns a list of face-id lists."""
    from collections import deque

    # Per (face, vertex) UV via loops.
    fv_uv: dict[tuple[int, int], tuple[float, float]] = {}
    for loop in mesh.loops:
        fv_uv[(loop.face_id, loop.vertex_id)] = uvmap.get(loop.index)

    adjacency = mesh.face_adjacency()

    def welded(fa: int, fb: int, edge_id: int) -> bool:
        a, b = mesh.edges[edge_id].vertex_ids
        for vid in (a, b):
            ua = fv_uv.get((fa, vid))
            ub = fv_uv.get((fb, vid))
            if ua is None or ub is None:
                return False
            if abs(ua[0] - ub[0]) > tol or abs(ua[1] - ub[1]) > tol:
                return False
        return True

    seen: set[int] = set()
    islands: list[list[int]] = []
    for f in mesh.faces:
        if f.id in seen:
            continue
        comp: list[int] = []
        q = deque([f.id])
        seen.add(f.id)
        while q:
            cur = q.popleft()
            comp.append(cur)
            for nb, eid in adjacency[cur]:
                if nb not in seen and welded(cur, nb, eid):
                    seen.add(nb)
                    q.append(nb)
        islands.append(comp)
    return islands


def boundary_straightness_score(mesh: MeshGraph, seam_edge_ids) -> dict:
    """Jaggedness of the chart boundaries (chart-UV plan U0 / U1.5, report-only).

    At each interior seam vertex (degree-2 in the seam graph) measure the turning
    angle between its two seam edges; a straight boundary turns ~0°, a staircase turns
    a lot. Returns mean/p90 turning angle (degrees) and a 0..1 ``straightness`` score
    (1 = perfectly straight). Junction vertices (seam degree ≠ 2) are skipped."""
    seam = set(seam_edge_ids)
    incident: dict[int, list[int]] = {}
    for eid in seam:
        a, b = mesh.edges[eid].vertex_ids
        incident.setdefault(a, []).append(eid)
        incident.setdefault(b, []).append(eid)

    turns: list[float] = []
    for vid, eids in incident.items():
        if len(eids) != 2:
            continue
        dirs = []
        for eid in eids:
            a, b = mesh.edges[eid].vertex_ids
            other = b if a == vid else a
            d = mesh.vertex_co(other) - mesh.vertex_co(vid)
            n = np.linalg.norm(d)
            if n > 1e-12:
                dirs.append(d / n)
        if len(dirs) != 2:
            continue
        # Angle between the incoming and outgoing edge directions (0 = straight line).
        cos = float(np.clip(-np.dot(dirs[0], dirs[1]), -1.0, 1.0))
        turns.append(math.degrees(math.acos(cos)))
    if not turns:
        return {"mean_turn_deg": 0.0, "p90_turn_deg": 0.0, "straightness": 1.0, "samples": 0}
    arr = np.asarray(turns)
    mean_turn = float(arr.mean())
    return {
        "mean_turn_deg": round(mean_turn, 3),
        "p90_turn_deg": round(float(np.percentile(arr, 90)), 3),
        "straightness": round(max(0.0, 1.0 - mean_turn / 90.0), 4),
        "samples": int(arr.size),
    }


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
