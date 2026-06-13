"""T4 — reference-guided placement (UV_TRANSFER_PLAN §3.T4).

Place each adaptive chart's freshly-SLIM-unwrapped UVs into the matching reference
chart's slot — NOT ``pack_islands`` (that would discard the design). Per chart:
  1. scale to match the reference chart's texel density, then clamp to fit its bbox slot;
  2. rotate: among 4 axis-aligned orientations of the principal-axis alignment, keep the
     one maximising IoU with the reference footprint (cheap 256² raster);
  3. translate to the reference slot's center.
Then resolve any residual raster collisions (shrink ≤15% → nudge), logging every move.

Pure numpy on a :class:`UVMap`; the only Blender-side fallback (``pack_islands`` for a
stubborn colliding pair) lives in ``pipeline``.
"""

from __future__ import annotations

import numpy as np

from transfer_uv_agent.reference import (
    RefChart, chart_area_3d, chart_uv_area, principal_axis, raster_mask,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


def _chart_loops(mesh: MeshGraph, face_ids) -> list[int]:
    return [li for fid in face_ids for li in mesh.faces[fid].loop_indices]


def _chart_tris_loops(mesh: MeshGraph, face_ids):
    for fid in face_ids:
        li = mesh.faces[fid].loop_indices
        for i in range(1, len(li) - 1):
            yield li[0], li[i], li[i + 1]


def _rot(theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s], [s, c]])


def _iou(mask_a: np.ndarray, mask_b: np.ndarray) -> float:
    inter = int((mask_a & mask_b).sum())
    union = int((mask_a | mask_b).sum())
    return (inter / union) if union else 0.0


def _alignment_angles(src_axis: np.ndarray, ref_axis: np.ndarray) -> list[float]:
    """Base angle rotating ``src_axis`` onto ``ref_axis`` plus the 4 axis-aligned
    flips/quarter-turns (T4.2). The principal axis sign is arbitrary, so the 90/180/270
    variants cover the reflections the IoU search then picks among."""
    base = np.arctan2(ref_axis[1], ref_axis[0]) - np.arctan2(src_axis[1], src_axis[0])
    return [base + k * (np.pi / 2.0) for k in range(4)]


def place_group_density_first(mesh: MeshGraph, face_ids, uvmap: UVMap, ref: RefChart, *,
                              footprint_res: int = 256) -> dict:
    """T4 (density-first) placement of one reference part's charts: rotate to align the
    principal axis to the reference part's (4-way, max-IoU) and translate the centroid to
    the reference slot center — **rotation + translation ONLY, never a per-part scale**.
    The charts keep the single global texel density set by ``average_islands_scale`` (so
    density variance stays ~0, the new hard gate). Returns the chosen rotation + the IoU
    with the reference part's footprint in the shared [0,1] frame."""
    loops = _chart_loops(mesh, face_ids)
    pts = uvmap.uv[loops].copy()
    if len(pts) == 0 or not np.all(np.isfinite(pts)):
        return {"rotation_deg": 0.0, "iou": 0.0}
    centroid = pts.mean(axis=0)
    pts0 = pts - centroid
    src_axis = principal_axis(pts)
    ref_axis = np.asarray(ref.principal, dtype=float)
    ref_center = np.asarray(ref.center)
    lp = {li: i for i, li in enumerate(loops)}

    best = None
    with np.errstate(all="ignore"):
        for theta in _alignment_angles(src_axis, ref_axis):
            placed = pts0 @ _rot(theta).T + ref_center  # rotate about centroid, no scale
            if not np.all(np.isfinite(placed)):
                continue
            tris = [[placed[lp[l0]], placed[lp[l1]], placed[lp[l2]]]
                    for l0, l1, l2 in _chart_tris_loops(mesh, face_ids)]
            mask = raster_mask(tris, bbox=(0.0, 0.0, 1.0, 1.0), resolution=footprint_res)
            iou = _iou(mask, ref.abs_footprint)
            if best is None or iou > best[0]:
                best = (iou, np.degrees(theta) % 360.0, placed)
    if best is None:
        uvmap.uv[loops] = ref_center
        return {"rotation_deg": 0.0, "iou": 0.0}
    iou, rot_deg, placed = best
    uvmap.uv[loops] = placed
    return {"rotation_deg": float(rot_deg), "iou": float(iou)}


def place_group_islands(mesh: MeshGraph, islands: list, uvmap: UVMap, ref: RefChart, *,
                        footprint_res: int = 256, pad: float = 0.004,
                        fit_slot: bool = True) -> dict:
    """Round-3 T4: place ONE reference part's adaptive islands into its slot.

    After SLIM + pack the part's islands are scattered all over the tile; moving them as a
    rigid group (round 2) drags that scatter into the slot and the group extent explodes.
    Instead: the LARGEST island gets the rotation+IoU slot placement (it carries the part's
    look); every smaller island is then shelf-packed immediately around it — rows growing
    upward from the main island's top-right, wrapping at the slot width — so the whole
    part stays a compact block at its reference position. Density: never scaled here."""
    islands = sorted(islands, key=lambda fs: chart_uv_area(mesh, fs, uvmap), reverse=True)
    main = islands[0]
    info = place_group_density_first(mesh, main, uvmap, ref, footprint_res=footprint_res)
    lo, hi = chart_bbox(mesh, main, uvmap)
    wrap_w = max(2.0 * ref.half_extents[0], hi[0] - lo[0])
    cur_x, cur_y = hi[0] + pad, lo[1]
    row_h = 0.0
    for fs in islands[1:]:
        ls = _chart_loops(mesh, fs)
        blo, bhi = chart_bbox(mesh, fs, uvmap)
        w, h = bhi[0] - blo[0], bhi[1] - blo[1]
        if cur_x + w > lo[0] + wrap_w + pad and cur_x > hi[0] + pad:
            cur_x = lo[0]                      # new shelf row above everything placed so far
            cur_y = max(hi[1], cur_y + row_h) + pad
            row_h = 0.0
        _translate(uvmap, ls, np.array([cur_x - blo[0], cur_y - blo[1]]))
        cur_x += w + pad
        row_h = max(row_h, h)
    # Bounded slot fit: density matching makes block AREA ≈ slot area, but the block's
    # aspect can still overflow the slot (shape mismatch, IoU ~0.34). Scale the whole
    # block down to ≤ slot×slack — bounded at ``min_fit`` (NOT round 2's unbounded clamp;
    # the reference's own density variance is 0.515, so mild per-part scale stays well
    # under the 0.62 gate) — and re-center on the slot. The factor is returned for the log.
    if not fit_slot:
        # occupancy-grid placement (place_all_blocks) handles collisions itself; forcing
        # the block into the slot bbox here just destroys density/packing (fit hit its
        # 0.6 floor on aspect-mismatched parts → UV area ×0.36 → packing 0.25).
        info["slot_fit_scale"] = 1.0
        return info
    slack, min_fit = 1.0, 0.6
    all_loops = [li for fs in islands for li in _chart_loops(mesh, fs)]
    pts = uvmap.uv[all_loops]
    blo, bhi = pts.min(axis=0), pts.max(axis=0)
    bw, bh = max(bhi[0] - blo[0], 1e-9), max(bhi[1] - blo[1], 1e-9)
    fit = min(1.0, (2.0 * ref.half_extents[0] * slack) / bw,
              (2.0 * ref.half_extents[1] * slack) / bh)
    fit = max(fit, min_fit)
    center = (blo + bhi) / 2.0
    uvmap.uv[all_loops] = (pts - center) * fit + np.asarray(ref.center)
    # Edge blocks can stick out of the tile (our block is bigger than the artist's slot
    # near a border). Shift-only clamp back into [0,1] — translation, never a scale.
    pts = uvmap.uv[all_loops]
    blo, bhi = pts.min(axis=0), pts.max(axis=0)
    shift = np.zeros(2)
    for k in range(2):
        if blo[k] < 0.0:
            shift[k] = -blo[k]
        elif bhi[k] > 1.0:
            shift[k] = 1.0 - bhi[k]
    if np.any(shift):
        uvmap.uv[all_loops] = pts + shift
    info["slot_fit_scale"] = float(fit)
    return info


def place_all_blocks(mesh: MeshGraph, uvmap: UVMap, groups: list, ref_by_id: dict, *,
                     res: int = 512, pad_px: int = 2, max_attempts: int = 6) -> dict:
    """Round-3 final T4: occupancy-grid first-fit placement — zero overlap BY CONSTRUCTION.

    Why: the reference slots' bounding boxes themselves overlap (the artist packs by shape
    interlocking), so 'put each bbox block at its slot' can never be overlap-free, and
    separation/shrink loops just thrash (measured: raster 0.05–0.14 residual). Instead each
    part's block claims the FIRST FREE rectangle nearest its reference slot center on an
    occupancy grid: big parts (placed first) land on their slots, the rest nestle as close
    as the tile allows — semantic position becomes 'as close as possible', correctness is
    absolute. If some block finds no free spot, EVERYTHING is uniformly rescaled (density
    uniformity preserved) and placement restarts — bounded, logged attempts."""
    def _dilate(mask: np.ndarray, px: int) -> np.ndarray:
        out = mask.copy()
        for _ in range(px):
            d = out.copy()
            d[1:, :] |= out[:-1, :]
            d[:-1, :] |= out[1:, :]
            d[:, 1:] |= out[:, :-1]
            d[:, :-1] |= out[:, 1:]
            out = d
        return out

    order = sorted(groups, key=lambda g: ref_by_id[g[0]].uv_area, reverse=True)
    scale_total = 1.0
    for attempt in range(max_attempts):
        # form blocks at their slots (rotation+IoU for the main island, shelf for the rest)
        infos = {rid: place_group_islands(mesh, fss, uvmap, ref_by_id[rid], fit_slot=False)
                 for rid, fss in order}
        occ = np.zeros((res, res), dtype=bool)
        ok = True
        placed: list[list] = []
        for rid, fss in order:
            loops = [li for fs in fss for li in _chart_loops(mesh, fs)]
            pts = uvmap.uv[loops]
            lo, hi = pts.min(axis=0), pts.max(axis=0)
            # TRUE-SHAPE mask (not bbox): blob charts must interlock like the artist's
            # layout — the rect variant provably cannot tile the unit square (bbox-area
            # sum > 1 at reference density).
            lp = {li: i for i, li in enumerate(loops)}
            tris = []
            for fs in fss:
                for l0, l1, l2 in _chart_tris_loops(mesh, fs):
                    tris.append([pts[lp[l0]] - lo, pts[lp[l1]] - lo, pts[lp[l2]] - lo])
            w = int(np.ceil((hi[0] - lo[0]) * res)) + 1
            h = int(np.ceil((hi[1] - lo[1]) * res)) + 1
            if w >= res or h >= res:
                ok = False
                break
            M = max(w, h)
            mask = raster_mask(tris, bbox=(0.0, 0.0, M / res, M / res), resolution=M)
            mask = mask[:h, :w]
            mask = _dilate(mask, pad_px)
            cx = int(np.asarray(ref_by_id[rid].center)[0] * res) - w // 2
            cy = int(np.asarray(ref_by_id[rid].center)[1] * res) - h // 2
            best = None
            stride = 2
            for ring in range(0, res, stride):
                cands = ([(cx, cy)] if ring == 0 else
                         [(cx + dx, cy + dy) for dx in range(-ring, ring + 1, stride)
                          for dy in (-ring, ring)] +
                         [(cx + dx, cy + dy) for dy in range(-ring + stride, ring, stride)
                          for dx in (-ring, ring)])
                for x, y in cands:
                    if x < 0 or y < 0 or x + w > res or y + h > res:
                        continue
                    if not (occ[y:y + h, x:x + w] & mask).any():
                        best = (x, y)
                        break
                if best:
                    break
            if best is None:
                ok = False
                break
            x, y = best
            _translate(uvmap, loops, np.array([x / res, y / res]) - lo)
            occ[y:y + h, x:x + w] |= mask
            placed.append([loops, mask, x, y, w, h])
        if ok:
            # Gravity compaction: slide every block down/left to contact (artist layouts
            # are flush-packed). Shrinks the layout bbox — packing = UVarea/bbox — while
            # keeping the slot-anchored RELATIVE arrangement (blocks only translate,
            # ordering is preserved by the contact constraint).
            for _ in range(8):
                any_move = False
                for blk in sorted(placed, key=lambda b: (b[3], b[2])):
                    loops_b, mask_b, x, y, w, h = blk
                    occ[y:y + h, x:x + w] &= ~mask_b
                    nx, ny = x, y
                    moved = True
                    while moved:
                        moved = False
                        while ny > 0 and not (occ[ny - 1:ny - 1 + h, nx:nx + w] & mask_b).any():
                            ny -= 1
                            moved = True
                        while nx > 0 and not (occ[ny:ny + h, nx - 1:nx - 1 + w] & mask_b).any():
                            nx -= 1
                            moved = True
                    occ[ny:ny + h, nx:nx + w] |= mask_b
                    if (nx, ny) != (x, y):
                        _translate(uvmap, loops_b,
                                   np.array([(nx - x) / res, (ny - y) / res]))
                        blk[2], blk[3] = nx, ny
                        any_move = True
                if not any_move:
                    break
            return {"attempts": attempt + 1, "global_scale": round(scale_total, 4),
                    "iou": {rid: infos[rid]["iou"] for rid, _ in order}}
        # no fit → uniform global shrink (one factor for every chart: density stays uniform)
        all_loops = [li for _, fss in order for fs in fss for li in _chart_loops(mesh, fs)]
        pts = uvmap.uv[all_loops]
        c = (pts.min(axis=0) + pts.max(axis=0)) / 2.0
        uvmap.uv[all_loops] = (pts - c) * 0.93 + c
        scale_total *= 0.93
    raise RuntimeError(
        f"transfer T4: occupancy placement failed after {max_attempts} attempts "
        f"(cumulative scale {scale_total:.3f}) — refusing to ship overlapped/collapsed UVs")


def chart_bbox(mesh: MeshGraph, face_ids, uvmap: UVMap):
    pts = uvmap.uv[_chart_loops(mesh, face_ids)]
    return pts.min(axis=0), pts.max(axis=0)


def _translate(uvmap, loops, delta):
    uvmap.uv[loops] = uvmap.uv[loops] + delta


def _scale_about_center(mesh, face_ids, uvmap, factor):
    loops = _chart_loops(mesh, face_ids)
    pts = uvmap.uv[loops]
    c = pts.mean(axis=0)
    uvmap.uv[loops] = (pts - c) * factor + c


def normalize_global_density(mesh: MeshGraph, uvmap: UVMap, charts: list,
                             target_density: float) -> float:
    """Round-3 fix: scale ALL charts by ONE factor so the global texel density (UV area /
    3D area) equals the REFERENCE's. With matching density, each part's chart has ~the same
    UV area as its reference slot by construction, so slot placement fits without the
    separation/global-shrink cascade that collapsed the round-2 layout to 2.9% packing.
    Uniform scale ⇒ density variance untouched. Returns the factor applied."""
    uv_a = 0.0
    a3 = 0.0
    for _, fs in charts:
        uv_a += chart_uv_area(mesh, fs, uvmap)
        a3 += chart_area_3d(mesh, fs)
    if uv_a <= 1e-12 or a3 <= 1e-12 or target_density <= 0.0:
        return 1.0
    factor = float(np.sqrt(target_density / (uv_a / a3)))
    loops = [li for _, fs in charts for li in _chart_loops(mesh, fs)]
    pts = uvmap.uv[loops]
    center = (pts.min(axis=0) + pts.max(axis=0)) / 2.0
    uvmap.uv[loops] = (pts - center) * factor + center
    return factor


def separate_charts(mesh: MeshGraph, uvmap: UVMap, charts: list, *, passes: int = 60,
                    pad: float = 0.004, max_disp: float = 0.15) -> int:
    """Push overlapping chart bboxes apart into neighbouring empty space (the user's
    'expand into empty space, do NOT clamp density' rule). Bbox-rectangle relaxation: each
    pass moves an overlapping pair apart along their min-overlap axis by half the overlap;
    everything stays uniformly scaled (density untouched). Keeps charts inside [0,1].

    Round-3 guard: each chart's CUMULATIVE displacement is capped at ``max_disp`` — a chart
    at its cap stops moving (its residual collision goes to ``resolve_overlaps``' logged
    local shrink instead). This kills the runaway ballooning that round 2's unbounded
    separation + global re-fit produced. Returns the number of passes that moved something."""
    loops = {cid: _chart_loops(mesh, fs) for cid, fs in charts}
    total_disp = {cid: 0.0 for cid, _ in charts}
    moved_passes = 0
    for _ in range(passes):
        boxes = {}
        for cid, ls in loops.items():
            p = uvmap.uv[ls]
            boxes[cid] = (p.min(axis=0), p.max(axis=0))
        moved = False
        ids = list(loops)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                (alo, ahi), (blo, bhi) = boxes[a], boxes[b]
                ox = min(ahi[0], bhi[0]) - max(alo[0], blo[0]) + pad
                oy = min(ahi[1], bhi[1]) - max(alo[1], blo[1]) + pad
                if ox <= 0 or oy <= 0:
                    continue                        # disjoint
                ca = (alo + ahi) / 2.0
                cb = (blo + bhi) / 2.0
                # Cap: a chart at max_disp is frozen; its half of the push is dropped
                # (NOT transferred — bounded movement beats guaranteed separation here;
                # the residual overlap is handled by the logged local shrink).
                room_a = max(0.0, max_disp - total_disp[a])
                room_b = max(0.0, max_disp - total_disp[b])
                if room_a <= 0.0 and room_b <= 0.0:
                    continue
                # Tile walls: a push may never drive a chart outside [0,1] (round 3 —
                # edge charts pushed out were why the layout outgrew the tile and the
                # global shrink collapsed everything). Clamp each move to the wall.
                if ox < oy:                          # separate along x
                    d = ox / 2.0
                    sign = 1.0 if ca[0] <= cb[0] else -1.0
                    da, db = min(d, room_a), min(d, room_b)
                    da = min(da, (alo[0] - 0.0) if sign > 0 else (1.0 - ahi[0]))
                    db = min(db, (1.0 - bhi[0]) if sign > 0 else (blo[0] - 0.0))
                    da, db = max(da, 0.0), max(db, 0.0)
                    if da > 0:
                        _translate(uvmap, loops[a], np.array([-sign * da, 0.0]))
                    if db > 0:
                        _translate(uvmap, loops[b], np.array([sign * db, 0.0]))
                else:                                # separate along y
                    d = oy / 2.0
                    sign = 1.0 if ca[1] <= cb[1] else -1.0
                    da, db = min(d, room_a), min(d, room_b)
                    da = min(da, (alo[1] - 0.0) if sign > 0 else (1.0 - ahi[1]))
                    db = min(db, (1.0 - bhi[1]) if sign > 0 else (blo[1] - 0.0))
                    da, db = max(da, 0.0), max(db, 0.0)
                    if da > 0:
                        _translate(uvmap, loops[a], np.array([0.0, -sign * da]))
                    if db > 0:
                        _translate(uvmap, loops[b], np.array([0.0, sign * db]))
                total_disp[a] += da
                total_disp[b] += db
                if da <= 0 and db <= 0:
                    continue
                moved = True
                boxes[a] = (uvmap.uv[loops[a]].min(axis=0), uvmap.uv[loops[a]].max(axis=0))
                boxes[b] = (uvmap.uv[loops[b]].min(axis=0), uvmap.uv[loops[b]].max(axis=0))
        if moved:
            moved_passes += 1
        else:
            break
    return moved_passes


def fit_all_into_unit(mesh: MeshGraph, uvmap: UVMap, charts: list, *, margin: float = 0.005) -> float:
    """Uniformly scale + translate ALL transferred charts so their combined bbox fits the
    [0,1] tile (the uv_bounds hard gate). A SINGLE global scale is applied → relative chart
    sizes are untouched, so the uniform texel density (variance ~0) is preserved, and the
    relative slot positions (correspondence) are preserved. Returns the scale applied."""
    loops = [li for _, fs in charts for li in _chart_loops(mesh, fs)]
    if not loops:
        return 1.0
    pts = uvmap.uv[loops]
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    if lo[0] >= 0.0 and lo[1] >= 0.0 and hi[0] <= 1.0 and hi[1] <= 1.0:
        return 1.0  # already inside [0,1]: do NOT touch the layout (round 3 — the
        #             reference-slot positions are the design; recentering would shift them)
    ext = np.maximum(hi - lo, 1e-9)
    avail = 1.0 - 2.0 * margin
    scale = float(min(avail / ext[0], avail / ext[1], 1.0))
    center = (lo + hi) / 2.0
    for _, fs in charts:
        ls = _chart_loops(mesh, fs)
        uvmap.uv[ls] = (uvmap.uv[ls] - center) * scale + np.array([0.5, 0.5])
    return scale


def resolve_overlaps(mesh: MeshGraph, uvmap: UVMap, charts: list, ref_for: dict, *,
                     resolution: int = 512, min_shrink: float = 0.5,
                     raster_max: float = 0.005, max_rounds: int = 24) -> dict:
    """Last-resort residual-collision fix AFTER separation (T4.4): for a still-colliding
    chart, locally shrink ONLY that chart (down to ``min_shrink``), recording the exact
    amount — the user's 'still colliding → local adjust that chart + record the amount'.
    This is the only place a chart's density may move, so it is logged and kept minimal.
    ``charts`` is ``[(chart_id, face_ids), ...]``."""
    from uv_agent.geometry.evaluation import raster_overlap_diagnosis

    face_chart = {fid: cid for cid, fs in charts for fid in fs}
    fs_for = dict(charts)
    adjustments: list[dict] = []

    def overlap():
        return raster_overlap_diagnosis(mesh, uvmap, face_chart, resolution=resolution)

    diag = overlap()
    rounds = 0
    shrunk: dict = {}
    while diag["raster_overlap_ratio"] > raster_max and diag["cross_charts"] and rounds < max_rounds:
        rounds += 1
        colliding = [c for c in diag["cross_charts"] if c in fs_for]
        if not colliding:
            break
        sizes = {cid: len(fs_for[cid]) for cid in colliding}
        victim = min(colliding, key=lambda c: sizes[c])
        if shrunk.get(victim, 1.0) <= min_shrink:
            colliding2 = [c for c in colliding if shrunk.get(c, 1.0) > min_shrink]
            if not colliding2:
                break
            victim = min(colliding2, key=lambda c: sizes.get(c, 1))
        _scale_about_center(mesh, fs_for[victim], uvmap, 0.9)
        shrunk[victim] = shrunk.get(victim, 1.0) * 0.9
        adjustments.append({"chart": int(victim), "op": "local_shrink", "factor": round(shrunk[victim], 4)})
        diag = overlap()

    return {"adjustments": adjustments,
            "raster_overlap_ratio": diag["raster_overlap_ratio"],
            "cross_charts": diag["cross_charts"],
            "needs_pack_fallback": diag["raster_overlap_ratio"] > raster_max}
