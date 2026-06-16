"""A6 — layout (AUTO_ARTIST_UV_PLAN §5.A6), reduced v1 after the first full-asset run.

**Lesson from `out/artist_full/t5850`:** forcing a part-grouped band/shelf BBOX layout as
the FINAL packer wrecked UV space (packing 0.24, half the tile empty, checker too large).
Per the user's correction, the band/grouping is now METADATA / debug only — the final
packing is owned by Blender's shape-aware CONCAVE packer (`pipeline.run_artist_uv`).

This module therefore provides, for the shipped pipeline:

- :func:`orient_long_islands` — rotate long islands (strips / cylinders / elongated) to a
  consistent vertical axis in UV; a CONCAVE re-pack (rotate off) then keeps that
  orientation. Orientation is the LOWEST priority (overlap > packing > checker > grouping
  > orientation), so the pipeline keeps it only when packing stays acceptable.
- :func:`layout_metadata` — the part grouping / band assignment + measured orientation /
  symmetry / density, REPORT-ONLY (validated as metadata before any layout is forced).

:func:`band_shelf_pack` (the old pure bbox band/shelf packer) is kept for DEBUG / study
and unit tests only — it is overlap-free and in-bounds by construction, but it is no
longer the shipped final layout. Pure numpy on a ``UVMap`` — unit-testable without Blender.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from artist_uv_agent.classification import PartClass
from artist_uv_agent.descriptors import PartDescriptor, quiet_fp
from artist_uv_agent.seams import SeamResult
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

# Part class → layout band (0 = top). Details/caps ride with their parent block, so the
# top band mainly holds standalone small parts (plan §5.A6 band table).
CLASS_BAND = {"detail": 0, "cap": 0, "blob": 1, "panel": 1, "unknown": 1, "shell": 1,
              "strip": 2, "cylinder": 2}
GAP = 0.01                                  # relative padding between boxes (raster margin)


@dataclass
class LayoutPlan:
    chart_xform: dict[int, dict]            # cid → {rotation_deg, scale, mirror}
    blocks: dict[int, list[int]]            # block(part) id → chart ids
    block_band: dict[int, int]
    metrics: dict = field(default_factory=dict)
    per_part_density: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"chart_xform": {int(k): v for k, v in self.chart_xform.items()},
                "blocks": {int(k): v for k, v in self.blocks.items()},
                "block_band": {int(k): v for k, v in self.block_band.items()},
                "metrics": self.metrics, "per_part_density": self.per_part_density}


# -- geometry helpers --------------------------------------------------------

def _chart_loops(mesh: MeshGraph, faces) -> list[int]:
    loops: list[int] = []
    for f in faces:
        loops.extend(mesh.faces[f].loop_indices)
    return loops


def _uv_points(uvmap: UVMap, loops) -> np.ndarray:
    return np.array([uvmap.get(li) for li in loops], dtype=float)


def _principal_angle(points: np.ndarray) -> float:
    """Angle (radians) of the island's long axis from +u; rotating by ``−angle`` makes the
    long axis vertical."""
    c = points.mean(axis=0)
    x = points - c
    if len(x) < 2:
        return 0.0
    cov = np.cov(x.T)
    w, v = np.linalg.eigh(cov)
    long_axis = v[:, int(np.argmax(w))]
    return float(np.arctan2(long_axis[1], long_axis[0]))


def _rotate(points: np.ndarray, angle: float, center: np.ndarray) -> np.ndarray:
    ca, sa = np.cos(angle), np.sin(angle)
    r = np.array([[ca, -sa], [sa, ca]])
    return (points - center) @ r.T + center


def _face_uv_area(mesh: MeshGraph, fid: int, uvmap: UVMap) -> float:
    li = mesh.faces[fid].loop_indices
    area = 0.0
    for i in range(1, len(li) - 1):
        a, b, c = uvmap.get(li[0]), uvmap.get(li[i]), uvmap.get(li[i + 1])
        area += abs(0.5 * ((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])))
    return area


def _shelf_pack(boxes: list[tuple[float, float]], gap: float, target_w: float | None = None):
    """Pack axis-aligned ``(w, h)`` boxes into rows; preserve input order (so symmetric
    mates / parent+detail stay adjacent). ``target_w`` caps the row width (default ~square
    aspect from the total area). Returns bottom-left positions (y-up) and the total
    ``(width, height)``. Overlap-free by construction."""
    if not boxes:
        return [], (0.0, 0.0)
    total_area = sum(w * h for w, h in boxes)
    target_w = max(max(w for w, _ in boxes),
                   target_w if target_w is not None else float(np.sqrt(total_area)) * 1.1)
    pos: list[tuple[float, float]] = []
    x = y = 0.0
    row_h = 0.0
    max_w = 0.0
    for w, h in boxes:
        if x > 0 and x + w > target_w:
            x = 0.0
            y += row_h + gap          # new row stacks upward
            row_h = 0.0
        pos.append((x, y))
        x += w + gap
        row_h = max(row_h, h)
        max_w = max(max_w, x - gap)
    total_h = y + row_h
    # Flip rows so the FIRST box sits at the top (highest y) — reading order top→down.
    pos = [(px, total_h - py - boxes[i][1]) for i, (px, py) in enumerate(pos)]
    return pos, (max_w, total_h)


# -- chart preparation -------------------------------------------------------

def _prepare_charts(mesh, uvmap, seam: SeamResult, descriptors, classes, weights,
                    *, orient: bool, target_density: float):
    """Per chart: density scale, orientation angle, mirror flag, and the resulting
    rotated+scaled bbox + local points (origin at bbox-min)."""
    from chart_uv_agent.segmentation import flood_charts

    charts = flood_charts(mesh, seam.seams)
    desc_by = {d.part_id: d for d in descriptors}
    class_by = {c.part_id: c for c in classes}
    # Symmetric mates are PAIRED (placed adjacently, _order_with_mates) and reported, but
    # NOT winding-mirrored in v1: a UV mirror inverts face winding, which the signed-area
    # flip gate (and tangent-space baking) correctly reject. Mirrored-orientation pairing
    # is a v2 refinement that needs proper winding handling (plan §5.A6).
    mirror_parts: set[int] = set()

    # Pass 1: per-chart UV/3D areas → the density ratio a_3d/a_uv. A degenerate SLIM island
    # (a_uv ≈ 0) would give an astronomical ratio and blow up the scale (NaN/overflow), so
    # ratios are CLAMPED to a window around the median — a degenerate sliver is packed at
    # ~median density rather than exploding the layout.
    raw = {}
    for cid, faces in enumerate(charts):
        loops = _chart_loops(mesh, faces)
        a_uv = sum(_face_uv_area(mesh, f, uvmap) for f in faces) or 1e-12
        a_3d = sum(mesh.faces[f].area_3d for f in faces) or 1e-12
        raw[cid] = {"loops": loops, "pts": _uv_points(uvmap, loops),
                    "a_uv": a_uv, "a_3d": a_3d, "ratio": a_3d / a_uv}
    ratios = np.array([raw[c]["ratio"] for c in raw]) if raw else np.array([1.0])
    med = float(np.median(ratios))
    lo, hi = med / 25.0, med * 25.0

    info: dict[int, dict] = {}
    for cid, faces in enumerate(charts):
        pid = seam.chart_to_part[cid]
        loops = raw[cid]["loops"]
        pts = raw[cid]["pts"]
        a_3d = raw[cid]["a_3d"]
        ratio = float(np.clip(raw[cid]["ratio"], lo, hi))
        scale = float(np.sqrt(target_density * weights.get(pid, 1.0) * ratio))
        role = class_by[pid].type
        # Orient every island to its principal axis (long axis vertical): consistent
        # reading orientation AND a tighter axis-aligned bbox → better shelf packing.
        angle = -_principal_angle(pts) + np.pi / 2 if orient else 0.0
        center = pts.mean(axis=0)
        rp = _rotate(pts, angle, center)
        if pid in mirror_parts:
            rp[:, 0] = 2 * center[0] - rp[:, 0]      # mirror u about the chart centre
        rp = (rp - center) * scale                    # density-normalise, origin at centroid
        mn = rp.min(axis=0)
        local = rp - mn
        bbox = local.max(axis=0)
        info[cid] = {"pid": pid, "loops": loops, "local": local,
                     "bbox": (float(bbox[0]), float(bbox[1])),
                     "rotation_deg": float(np.degrees(angle)), "scale": scale,
                     "mirror": pid in mirror_parts, "a_3d": a_3d, "role": role}
    return charts, info


def _resolve_blocks(seam: SeamResult, descriptors, classes, part_neighbors):
    """Group charts into BLOCKS (plan §5.A6 'group charts by part'); a detail/cap/shell
    part's charts ride with its parent (largest-area attached neighbour) so details sit
    near their parent. Returns ``block_part`` (part → block-owner part) and the per-block
    chart lists + band."""
    area_by = {d.part_id: d.area for d in descriptors}
    class_by = {c.part_id: c.type for c in classes}
    block_owner: dict[int, int] = {}
    for d in descriptors:
        pid = d.part_id
        owner = pid
        if class_by.get(pid) in ("detail", "cap", "shell"):
            nbrs = [n for n in part_neighbors.get(pid, set())
                    if class_by.get(n) not in ("detail", "cap", "shell")]
            if nbrs:
                owner = max(nbrs, key=lambda n: area_by.get(n, 0.0))
        block_owner[pid] = owner

    blocks: dict[int, list[int]] = {}
    for cid, pid in seam.chart_to_part.items():
        blocks.setdefault(block_owner[pid], []).append(cid)
    block_band = {owner: CLASS_BAND.get(class_by.get(owner, "unknown"), 1) for owner in blocks}
    return block_owner, blocks, block_band


# -- main entry --------------------------------------------------------------

@quiet_fp
def orient_long_islands(mesh: MeshGraph, uvmap: UVMap, charts, descriptors, classes,
                        seam: SeamResult, *, elong_min: float = 1.5) -> UVMap:
    """Rotate each LONG island (a ``strip``/``cylinder`` or a part with 3D elongation ≥
    ``elong_min``) about its UV centroid so its long axis is vertical — a consistent
    reading orientation. Pure: returns a NEW ``UVMap``; the islands may now overlap / leave
    ``[0,1]`` (a subsequent CONCAVE re-pack with rotation OFF repositions them and restores
    bounds while preserving the orientation). Short/round islands are left untouched."""
    desc_by = {d.part_id: d for d in descriptors}
    class_by = {c.part_id: c for c in classes}
    out = uvmap.copy()
    for cid, faces in enumerate(charts):
        pid = seam.chart_to_part[cid]
        d = desc_by.get(pid)
        role = class_by[pid].type if pid in class_by else "unknown"
        if not (role in ("strip", "cylinder") or (d is not None and d.elongation >= elong_min)):
            continue
        loops = _chart_loops(mesh, faces)
        pts = _uv_points(uvmap, loops)
        center = pts.mean(axis=0)
        angle = -_principal_angle(pts) + np.pi / 2
        rp = _rotate(pts, angle, center)
        for k, li in enumerate(loops):
            out.set(li, float(rp[k, 0]), float(rp[k, 1]))
    return out


@quiet_fp
def layout_metadata(mesh: MeshGraph, uvmap: UVMap, seam: SeamResult, descriptors, classes,
                    part_neighbors: dict[int, set[int]]) -> tuple[dict, dict]:
    """REPORT-ONLY layout metadata (no UVs are forced from it). Returns ``(metrics,
    per_part_density)``:

    - segmentation structure: part confidence, charts-per-part, symmetry pairs;
    - the INTENDED part grouping / band assignment (validated as metadata, NOT applied —
      the user's correction: grouping must be checked before it drives layout);
    - measured-from-final-UV: orientation consistency, strip alignment, per-part density.
    """
    from chart_uv_agent.segmentation import flood_charts

    charts = flood_charts(mesh, seam.seams)
    desc_by = {d.part_id: d for d in descriptors}
    class_by = {c.part_id: c for c in classes}
    block_owner, blocks, block_band = _resolve_blocks(seam, descriptors, classes, part_neighbors)

    long_angles, strip_angles = [], []
    part_uv: dict[int, float] = {}
    part_3d: dict[int, float] = {}
    for cid, faces in enumerate(charts):
        pid = seam.chart_to_part[cid]
        d = desc_by.get(pid)
        ang = _principal_angle(_uv_points(uvmap, _chart_loops(mesh, faces)))
        if d is not None and d.elongation > 1.5:
            long_angles.append(ang)
        if class_by[pid].type == "strip":
            strip_angles.append(np.degrees(ang))
        part_uv[pid] = part_uv.get(pid, 0.0) + sum(_face_uv_area(mesh, f, uvmap) for f in faces)
        part_3d[pid] = part_3d.get(pid, 0.0) + sum(mesh.faces[f].area_3d for f in faces)

    orient_consistency = float(np.mean([abs(np.sin(a)) for a in long_angles])) if long_angles else 1.0
    strip_alignment = (1.0 - min(1.0, float(np.std(strip_angles)) / 45.0)) if len(strip_angles) > 1 else 1.0
    per_part_density = {pid: (part_uv[pid] / part_3d[pid]) if part_3d[pid] > 1e-12 else 0.0
                        for pid in part_uv}

    mates = {tuple(sorted((d.part_id, d.symmetry_mate))) for d in descriptors if d.symmetry_mate >= 0}
    n_parts = len(set(seam.chart_to_part.values()))
    metrics = {
        "part_coverage": 1.0,
        "part_confidence_mean": round(float(np.mean([c.confidence for c in classes])), 4) if classes else 0.0,
        "charts_per_part": round(len(charts) / max(1, n_parts), 3),
        "symmetry_pair_count": len(mates),
        "orientation_consistency": round(orient_consistency, 4),
        "strip_alignment_score": round(strip_alignment, 4),
        "intended_grouping": {
            "layout_group_count": len(blocks),
            "block_band": {int(o): int(b) for o, b in block_band.items()},
            "part_to_block": {int(p): int(o) for p, o in block_owner.items()},
        },
        "note": "grouping is intended-structure metadata, NOT forced onto the final UVs "
                "(final layout = Blender CONCAVE pack); orientation_consistency is measured "
                "on the final UVs.",
    }
    return metrics, {int(k): round(v, 6) for k, v in per_part_density.items()}


@quiet_fp
def band_shelf_pack(mesh: MeshGraph, uvmap: UVMap, seam: SeamResult,
                    descriptors: list[PartDescriptor], classes: list[PartClass],
                    weights: dict[int, float], part_neighbors: dict[int, set[int]], *,
                    margin: float = 0.02, orient: bool = True,
                    target_density: float = 1.0) -> tuple[UVMap, LayoutPlan]:
    """DEBUG / STUDY ONLY (no longer the shipped final layout — see module docstring). The
    pure band→block→chart shelf packer: overlap-free and in-bounds by construction, but it
    wastes UV space on irregular organic charts (the reason it was demoted). Returns a NEW
    ``UVMap`` and the :class:`LayoutPlan` with per-chart transforms + the bbox-layout metrics."""
    charts, info = _prepare_charts(mesh, uvmap, seam, descriptors, classes, weights,
                                   orient=orient, target_density=target_density)
    block_owner, blocks, block_band = _resolve_blocks(seam, descriptors, classes, part_neighbors)

    # symmetric pairs adjacent: order blocks within a band so a block and its mate-owner
    # block sit next to each other.
    desc_by = {d.part_id: d for d in descriptors}

    # 1. pack charts within each block → block bbox + per-chart offset (within block).
    block_box: dict[int, tuple[float, float]] = {}
    chart_in_block: dict[int, tuple[float, float]] = {}
    for owner, cids in blocks.items():
        # tallest-first within a block packs the shelves tighter (chart order carries no
        # grammar meaning inside a block — symmetry/parent adjacency is a BLOCK-level rule).
        cids.sort(key=lambda c: -info[c]["bbox"][1])
        boxes = [info[c]["bbox"] for c in cids]
        pos, box = _shelf_pack(boxes, GAP)
        block_box[owner] = box
        for c, p in zip(cids, pos):
            chart_in_block[c] = p

    # 2. assign blocks to bands; order within band (symmetry-adjacent), pack blocks. All
    # bands share ONE target width so they read as aligned horizontal strips (top: details
    # /caps, middle: blob/panel, bottom: strips/cylinders) rather than ragged rows of
    # differing widths — the band grammar (plan §5.A6) over raw packing.
    bands: dict[int, list[int]] = {}
    for owner, band in block_band.items():
        bands.setdefault(band, []).append(owner)
    total_block_area = sum(w * h for w, h in block_box.values())
    band_target_w = float(np.sqrt(total_block_area)) * 1.6
    band_layout: dict[int, tuple[float, float]] = {}
    block_in_band: dict[int, tuple[float, float]] = {}
    for band, owners in bands.items():
        owners = _order_with_mates(owners, desc_by)
        boxes = [block_box[o] for o in owners]
        pos, box = _shelf_pack(boxes, GAP, target_w=band_target_w)
        band_layout[band] = box
        for o, p in zip(owners, pos):
            block_in_band[o] = p

    # 3. stack bands vertically (band 0 on top).
    total_w = max((w for w, _ in band_layout.values()), default=1.0)
    band_y: dict[int, float] = {}
    y = 0.0
    for band in sorted(band_layout, reverse=True):      # bottom band first (y from 0 up)
        band_y[band] = y
        y += band_layout[band][1] + GAP
    total_h = max(y - GAP, 1e-9)

    # 4. absolute chart positions, map [0,W]×[0,H] → [margin, 1-margin] (uniform scale).
    scale = (1.0 - 2 * margin) / max(total_w, total_h)
    new_uv = uvmap.copy()
    for cid, ci in info.items():
        owner = block_owner[ci["pid"]]
        bx, by = block_in_band[owner]
        cx, cy = chart_in_block[cid]
        abs_x = bx + cx
        abs_y = band_y[block_band[owner]] + by + cy
        for k, li in enumerate(ci["loops"]):
            lx, ly = ci["local"][k]
            u = margin + (abs_x + lx) * scale
            v = margin + (abs_y + ly) * scale
            new_uv.set(li, u, v)

    metrics, per_part_density = _artist_metrics(mesh, new_uv, charts, seam, descriptors,
                                                classes, info, blocks, weights)
    xform = {cid: {"rotation_deg": round(info[cid]["rotation_deg"], 2),
                   "scale": round(info[cid]["scale"] * scale, 6),
                   "mirror": info[cid]["mirror"]} for cid in info}
    plan = LayoutPlan(chart_xform=xform, blocks=blocks, block_band=block_band,
                      metrics=metrics, per_part_density=per_part_density)
    return new_uv, plan


def _order_with_mates(owners: list[int], desc_by) -> list[int]:
    """Order block owners so symmetric mates are adjacent (plan §5.A6 'pair symmetric
    parts side-by-side')."""
    ordered: list[int] = []
    placed: set[int] = set()
    for o in sorted(owners):
        if o in placed:
            continue
        ordered.append(o)
        placed.add(o)
        mate = desc_by[o].symmetry_mate if o in desc_by else -1
        if mate in owners and mate not in placed:
            ordered.append(mate)
            placed.add(mate)
    return ordered


# -- artist-style report metrics (plan §6) -----------------------------------

def _chart_centroid(mesh, uvmap, faces) -> np.ndarray:
    return _uv_points(uvmap, _chart_loops(mesh, faces)).mean(axis=0)


def _artist_metrics(mesh, uvmap, charts, seam, descriptors, classes, info, blocks, weights):
    class_by = {c.part_id: c for c in classes}
    desc_by = {d.part_id: d for d in descriptors}

    # per-part texel density (UV area / 3D area, weight-adjusted target should make ~equal)
    part_uv: dict[int, float] = {}
    part_3d: dict[int, float] = {}
    for cid, faces in enumerate(charts):
        pid = seam.chart_to_part[cid]
        part_uv[pid] = part_uv.get(pid, 0.0) + sum(_face_uv_area(mesh, f, uvmap) for f in faces)
        part_3d[pid] = part_3d.get(pid, 0.0) + sum(mesh.faces[f].area_3d for f in faces)
    per_part_density = {pid: (part_uv[pid] / part_3d[pid]) if part_3d[pid] > 1e-12 else 0.0
                        for pid in part_uv}

    # Measured from the FINAL UV (not the applied rotation): the long axis of each
    # elongated island in the packed layout. Orientation consistency = how vertical those
    # long axes are (|sin θ| → 1 at ±90°); strip alignment = low angular spread of strips.
    def _final_long_angle(faces):
        return _principal_angle(_uv_points(uvmap, _chart_loops(mesh, faces)))

    long_angles = [_final_long_angle(charts[c]) for c in info
                   if desc_by.get(seam.chart_to_part[c]) and desc_by[seam.chart_to_part[c]].elongation > 1.5]
    orient_consistency = float(np.mean([abs(np.sin(a)) for a in long_angles])) if long_angles else 1.0

    strip_angles = [np.degrees(_final_long_angle(charts[c])) for c in info if info[c]["role"] == "strip"]
    strip_alignment = (1.0 - min(1.0, float(np.std(strip_angles)) / 45.0)) if len(strip_angles) > 1 else 1.0

    # detail-near-parent: detail centroids close to their block centroid.
    detail_scores = []
    block_centroid: dict[int, np.ndarray] = {}
    for owner, cids in blocks.items():
        pts = np.vstack([_chart_centroid(mesh, uvmap, charts[c]) for c in cids])
        block_centroid[owner] = pts.mean(axis=0)
    for cid, faces in enumerate(charts):
        pid = seam.chart_to_part[cid]
        if class_by[pid].type in ("detail", "cap"):
            owner = next((o for o, cs in blocks.items() if cid in cs), pid)
            d = float(np.linalg.norm(_chart_centroid(mesh, uvmap, faces) - block_centroid[owner]))
            detail_scores.append(1.0 if d < 0.35 else max(0.0, 1.0 - (d - 0.35) / 0.35))
    detail_near_parent = float(np.mean(detail_scores)) if detail_scores else 1.0

    # symmetry pairs + paired scale error.
    mates = set()
    pair_errs = []
    for d in descriptors:
        if d.symmetry_mate >= 0:
            key = tuple(sorted((d.part_id, d.symmetry_mate)))
            if key in mates:
                continue
            mates.add(key)
            a, b = key
            ua, ub = part_uv.get(a, 0.0), part_uv.get(b, 0.0)
            if max(ua, ub) > 1e-9:
                pair_errs.append(abs(ua - ub) / max(ua, ub))
    paired_scale_error = float(np.mean(pair_errs)) if pair_errs else 0.0

    charts_per_part = float(len(charts) / max(1, len(set(seam.chart_to_part.values()))))
    part_conf = float(np.mean([c.confidence for c in classes])) if classes else 0.0
    readability = float(np.mean([orient_consistency, strip_alignment, detail_near_parent,
                                 1.0 - paired_scale_error]))

    metrics = {
        "part_coverage": 1.0,                       # segmentation partitions every face
        "part_confidence_mean": round(part_conf, 4),
        "charts_per_part": round(charts_per_part, 3),
        "symmetry_pair_count": len(mates),
        "paired_scale_error": round(paired_scale_error, 4),
        "layout_group_count": len(blocks),
        "strip_alignment_score": round(strip_alignment, 4),
        "detail_near_parent_score": round(detail_near_parent, 4),
        "orientation_consistency": round(orient_consistency, 4),
        "readability_score": round(readability, 4),
    }
    return metrics, {int(k): round(v, 6) for k, v in per_part_density.items()}
