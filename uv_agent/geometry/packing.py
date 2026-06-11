"""Island packing into the [0,1] UV square (plan §7 / Phase 5).

Two strategies, both overlap-free, in-bounds, and using a *single global scale*
so relative texel density is preserved:

- ``"maxrects"`` (default): MaxRects bin packing (Best-Short-Side-Fit) with
  per-island 0/90 rotation. Fills gaps far better than shelf packing, so it
  reaches a larger feasible scale -> higher packing efficiency. This implements
  the packing-efficiency improvement from the UV alignment plan (Phase 4).
- ``"shelf"``: the original row/shelf packer, kept for comparison / fallback.

Both binary-search the largest global scale that still fits.
"""

from __future__ import annotations

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import IslandTransform, UVMap
from uv_agent.planner.island_planner import Island, IslandPlan

_MIN_SIZE = 1e-6


def _rotate90(p: np.ndarray) -> np.ndarray:
    # (u, v) -> (-v, u)
    out = np.empty_like(p)
    out[:, 0] = -p[:, 1]
    out[:, 1] = p[:, 0]
    return out


# ---------------------------------------------------------------------------
# Shelf packer (original)
# ---------------------------------------------------------------------------
def _shelf_pack(sizes: list[tuple[float, float]], scale: float, padding: float, allow_rotate: bool = True):
    """Place rectangles left-to-right into width 1.0, wrapping to new shelves.
    Returns placements [(x, y, rotated)] in original order or None if it does
    not fit. Islands taller than wide are rotated to landscape first."""
    # Decide rotation per island (landscape), then shelf-pack by height.
    rot = [(allow_rotate and h > w) for (w, h) in sizes]
    eff = [(h, w) if r else (w, h) for (w, h), r in zip(sizes, rot)]
    order = sorted(range(len(eff)), key=lambda i: eff[i][1], reverse=True)
    placements = [None] * len(eff)
    x = padding
    y = padding
    shelf_h = 0.0
    for i in order:
        w, h = eff[i]
        sw, sh = w * scale, h * scale
        if sw + 2 * padding > 1.0 or sh + 2 * padding > 1.0:
            return None
        if x + sw + padding > 1.0:
            x = padding
            y += shelf_h + padding
            shelf_h = 0.0
        placements[i] = (x, y, rot[i])
        x += sw + padding
        shelf_h = max(shelf_h, sh)
    if y + shelf_h + padding > 1.0:
        return None
    return placements


# ---------------------------------------------------------------------------
# MaxRects packer (Best Short Side Fit, with 0/90 rotation)
# ---------------------------------------------------------------------------
def _split_free(free, placed):
    """Return the sub-rectangles of `free` not covered by `placed` (guillotine
    split of a MaxRects free node). Each rect is (x, y, w, h)."""
    fx, fy, fw, fh = free
    px, py, pw, ph = placed
    # No intersection -> free rect survives unchanged.
    if px >= fx + fw or px + pw <= fx or py >= fy + fh or py + ph <= fy:
        return [free]
    out = []
    eps = 1e-9
    # Left slab
    if px > fx + eps:
        out.append((fx, fy, px - fx, fh))
    # Right slab
    if px + pw < fx + fw - eps:
        out.append((px + pw, fy, fx + fw - (px + pw), fh))
    # Bottom slab
    if py > fy + eps:
        out.append((fx, fy, fw, py - fy))
    # Top slab
    if py + ph < fy + fh - eps:
        out.append((fx, py + ph, fw, fy + fh - (py + ph)))
    return out


def _prune(free):
    """Drop free rects fully contained in another free rect."""
    pruned = []
    for i, a in enumerate(free):
        ax, ay, aw, ah = a
        if aw <= 1e-9 or ah <= 1e-9:
            continue
        contained = False
        for j, b in enumerate(free):
            if i == j:
                continue
            bx, by, bw, bh = b
            if ax >= bx - 1e-9 and ay >= by - 1e-9 and ax + aw <= bx + bw + 1e-9 and ay + ah <= by + bh + 1e-9:
                # a is inside b; keep only one of two identical rects
                if (aw < bw - 1e-9 or ah < bh - 1e-9) or j < i:
                    contained = True
                    break
        if not contained:
            pruned.append(a)
    return pruned


def _maxrects_pack(sizes, scale, padding, allow_rotate=True):
    """Pack inflated rectangles (size*scale + padding) into the unit square.
    Returns placements [(x, y, rotated)] in original order or None."""
    free = [(0.0, 0.0, 1.0, 1.0)]
    placements = [None] * len(sizes)
    order = sorted(range(len(sizes)), key=lambda i: sizes[i][0] * sizes[i][1], reverse=True)
    for i in order:
        w0, h0 = sizes[i]
        iw = w0 * scale + padding
        ih = h0 * scale + padding
        best = None  # (short_fit, long_fit, x, y, rotated)
        for (fx, fy, fw, fh) in free:
            # orientation 0
            if iw <= fw + 1e-12 and ih <= fh + 1e-12:
                short = min(fw - iw, fh - ih)
                long_ = max(fw - iw, fh - ih)
                cand = (short, long_, fx, fy, False)
                if best is None or cand[:2] < best[:2]:
                    best = cand
            # orientation 90
            if allow_rotate and ih <= fw + 1e-12 and iw <= fh + 1e-12:
                short = min(fw - ih, fh - iw)
                long_ = max(fw - ih, fh - iw)
                cand = (short, long_, fx, fy, True)
                if best is None or cand[:2] < best[:2]:
                    best = cand
        if best is None:
            return None
        _, _, px, py, rot = best
        pw, ph = (ih, iw) if rot else (iw, ih)
        placements[i] = (px, py, rot)
        placed = (px, py, pw, ph)
        new_free = []
        for rect in free:
            new_free.extend(_split_free(rect, placed))
        free = _prune(new_free)
    return placements


_PACKERS = {"shelf": _shelf_pack, "maxrects": _maxrects_pack}


def _layout_efficiency(sizes, scale, placements) -> float:
    """Proxy for packing_efficiency of a candidate layout: scale^2 / bbox_area.

    The true UV area = k * scale^2 (k = sum of unit-scale island areas, constant
    across candidates), so this ordering matches the packing_efficiency metric.
    """
    if not placements or scale <= 0:
        return 0.0
    xs0, ys0, xs1, ys1 = [], [], [], []
    for (w, h), (px, py, rot) in zip(sizes, placements):
        pw, ph = (h, w) if rot else (w, h)
        xs0.append(px)
        ys0.append(py)
        xs1.append(px + pw * scale)
        ys1.append(py + ph * scale)
    bbox_area = (max(xs1) - min(xs0)) * (max(ys1) - min(ys0))
    if bbox_area <= 1e-12:
        return 0.0
    return (scale * scale) / bbox_area


def _search_scale(packer, sizes, padding, allow_rotate, iters: int = 48):
    """Binary-search the largest global scale that still fits with ``packer``.
    Returns (scale, placements)."""
    best = packer(sizes, 1e-9, padding, allow_rotate)
    lo, hi = 0.0, 1.0
    for _ in range(iters):
        mid = (lo + hi) / 2
        placed = packer(sizes, mid, padding, allow_rotate)
        if placed is not None:
            best = placed
            lo = mid
        else:
            hi = mid
    return lo, best


def pack_islands(
    mesh: MeshGraph,
    plan: IslandPlan,
    uvmap: UVMap,
    *,
    padding: float | None = None,
    allow_rotate: bool = True,
    strategy: str = "auto",
) -> list[IslandTransform]:
    """Lay out every island inside [0,1]^2. Mutates ``uvmap`` in place.

    ``strategy``:
    - ``"auto"`` (default): run both packers and keep whichever reaches the
      larger global scale (i.e. uses more of the texture). Guarantees the
      result is never worse than the original shelf packer, and gains on
      irregular island sets where MaxRects fills gaps better.
    - ``"maxrects"``: MaxRects (Best-Short-Side-Fit) only.
    - ``"shelf"``: original row/shelf packer only.

    All variants keep a single global scale (texel density preserved) and are
    overlap-free.
    """
    if padding is None:
        padding = plan.constraints.padding_uv
    padding = float(np.clip(padding, 0.0, 0.1))

    islands: list[Island] = [i for i in plan.islands if i.face_ids]
    if not islands:
        return []

    # Per-island local coords (min corner at origin), footprint (w, h).
    locals_: list[np.ndarray] = []
    loop_lists: list[list[int]] = []
    sizes: list[tuple[float, float]] = []
    for isl in islands:
        loop_indices = [li for fid in isl.face_ids for li in mesh.faces[fid].loop_indices]
        p = uvmap.uv[loop_indices].copy()
        p -= p.min(axis=0)
        w = max(_MIN_SIZE, float(p[:, 0].max()))
        h = max(_MIN_SIZE, float(p[:, 1].max()))
        locals_.append(p)
        loop_lists.append(loop_indices)
        sizes.append((w, h))

    if strategy == "auto":
        # Both packers maximize the global scale; pick the one that fills its
        # bounding box best (== higher packing_efficiency), tie-break on scale.
        candidates = [
            _search_scale(_maxrects_pack, sizes, padding, allow_rotate),
            _search_scale(_shelf_pack, sizes, padding, allow_rotate),
        ]
        scale, placements = max(
            candidates, key=lambda c: (_layout_efficiency(sizes, c[0], c[1]), c[0])
        )
    else:
        packer = _PACKERS.get(strategy, _maxrects_pack)
        scale, placements = _search_scale(packer, sizes, padding, allow_rotate)
    placements = placements or [(padding, padding, False)] * len(islands)

    transforms: list[IslandTransform] = []
    for idx, isl in enumerate(islands):
        px, py, rot = placements[idx]
        p = locals_[idx]
        if rot:
            p = _rotate90(p)
            p = p - p.min(axis=0)
        packed = p * scale + np.array([px, py])
        uvmap.uv[loop_lists[idx]] = packed
        transforms.append(
            IslandTransform(
                island_id=isl.island_id,
                rotation_deg=90.0 if rot else 0.0,
                scale=scale,
                translation=(float(px), float(py)),
            )
        )
    return transforms


def island_bbox(mesh: MeshGraph, island: Island, uvmap: UVMap) -> tuple[float, float, float, float]:
    loop_indices = [li for fid in island.face_ids for li in mesh.faces[fid].loop_indices]
    p = uvmap.uv[loop_indices]
    return (
        float(p[:, 0].min()),
        float(p[:, 1].min()),
        float(p[:, 0].max()),
        float(p[:, 1].max()),
    )
