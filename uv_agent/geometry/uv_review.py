"""Read-only UV review metrics (MVP 1 plan §6, Session A).

Computes the MVP 1 review report from a :class:`MeshGraph` + :class:`UVMap`
*as they already are* — it never unwraps, relaxes, packs, or otherwise modifies
the UVs (plan §1 "MVP 1은 read-only review 단계다"). Islands are recovered from
**loop-UV discontinuity**, not seam flags, exactly as the risk note in plan §13
requires ("seam flag가 아니라 loop UV discontinuity 기준으로 island를 추출").

Pure Python (NumPy only); runs everywhere the engine runs, no Blender needed, so
the metric helpers are unit-tested offline. The Blender worker
(``worker/review_existing_uv.py``) calls :func:`compute_uv_review` after reading
the existing UV layer with :mod:`uv_agent.blender.uv_extract`.

The mandatory-90 rule is intentionally NOT computed here (plan §6).
"""

from __future__ import annotations

import numpy as np

from uv_agent.geometry.evaluation import (
    _point_in_triangle,
    _tri_signed_area_uv,
    _tris_from_face,
    island_distortion_summary,
    per_face_stretch,
    raster_overlap_diagnosis,
    uv_islands_from_uvmap,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

# Default tolerance for the [0,1] tile bounds check (matches the contract).
DEFAULT_BOUNDS_TOL = 1e-4


def uv_bounds(uvmap: UVMap, *, tol: float = DEFAULT_BOUNDS_TOL) -> dict:
    """Axis-aligned UV bounds + an ``in_0_1`` flag (plan §6 ``uv_bounds``)."""
    if len(uvmap.uv) == 0:
        return {"min": [0.0, 0.0], "max": [0.0, 0.0], "in_0_1": True}
    umin = float(uvmap.uv[:, 0].min())
    vmin = float(uvmap.uv[:, 1].min())
    umax = float(uvmap.uv[:, 0].max())
    vmax = float(uvmap.uv[:, 1].max())
    in_0_1 = (
        umin >= -tol and vmin >= -tol and umax <= 1.0 + tol and vmax <= 1.0 + tol
    )
    return {
        "min": [round(umin, 6), round(vmin, 6)],
        "max": [round(umax, 6), round(vmax, 6)],
        "in_0_1": bool(in_0_1),
    }


def packing_efficiency(mesh: MeshGraph, uvmap: UVMap) -> float:
    """Used UV area / global UV bounding-box area (plan §6 semantics).

    ``used UV area`` is the sum of absolute triangle UV areas; the bbox is the
    global axis-aligned UV extent. 1.0 = perfectly tight, lower = wasted space.
    """
    areauv_total, _flipped = _signed_uv_areas(mesh, uvmap)
    if len(uvmap.uv) == 0 or areauv_total <= 1e-12:
        return 0.0
    du = float(uvmap.uv[:, 0].max() - uvmap.uv[:, 0].min())
    dv = float(uvmap.uv[:, 1].max() - uvmap.uv[:, 1].min())
    bbox_area = max(du * dv, 1e-12)
    return float(min(1.0, areauv_total / bbox_area))


def _signed_uv_areas(mesh: MeshGraph, uvmap: UVMap) -> tuple[float, float]:
    """Return ``(total_abs_uv_area, flipped_uv_area)`` over all triangulated faces."""
    total = 0.0
    flipped = 0.0
    for f in mesh.faces:
        for l0, l1, l2 in _tris_from_face(f.loop_indices):
            s = _tri_signed_area_uv(uvmap.get(l0), uvmap.get(l1), uvmap.get(l2))
            total += abs(s)
            if s < 0:
                flipped += abs(s)
    return total, flipped


def _stretch_score(mesh: MeshGraph, uvmap: UVMap) -> float:
    """Area-weighted mean of the per-face area distortion (== evaluate's stretch)."""
    fstr = per_face_stretch(mesh, uvmap)
    num = 0.0
    den = 0.0
    for f in mesh.faces:
        a3 = float(f.area_3d)
        num += fstr[f.id] * a3
        den += a3
    return (num / den) if den > 1e-12 else 0.0


def _texel_density_variance(islands_summary: list[dict]) -> float:
    """Coefficient of variation of per-island texel density (auv / a3)."""
    densities = [
        row["area_uv"] / row["area_3d"]
        for row in islands_summary
        if row["area_3d"] > 1e-12
    ]
    if len(densities) < 2:
        return 0.0
    arr = np.asarray(densities, dtype=float)
    mean = float(arr.mean())
    if mean <= 1e-12:
        return 0.0
    return float(arr.std() / mean)


def compute_uv_review(
    mesh: MeshGraph,
    uvmap: UVMap,
    *,
    raster_resolution: int = 1024,
    bounds_tol: float = DEFAULT_BOUNDS_TOL,
) -> dict:
    """Compute the full MVP 1 review report from a mesh + its existing UV map.

    Returns a dict with three blocks the contract/summary consumes:

    - ``metrics``: the eight required numeric metrics (plan §6).
    - ``uv``: ``island_count`` / ``uv_bounds`` / negative / out-of-tile flags.
    - ``islands``: the per-island distortion summary (debug/heatmap aid).

    Islands come from loop-UV discontinuity, so this works on any existing UV
    layout (artist FBX, transferred, generated) without a seam set. Island
    recovery being imperfect never blocks the report — the bounds/overlap/checker
    numbers are always produced (plan §13).
    """
    islands = uv_islands_from_uvmap(mesh, uvmap)
    islands_summary = island_distortion_summary(mesh, uvmap, islands)

    # Per-face chart id so the raster diagnosis can attribute self vs cross overlap.
    face_chart = {fid: cid for cid, faces in enumerate(islands) for fid in faces}
    raster = raster_overlap_diagnosis(
        mesh, uvmap, face_chart, resolution=int(raster_resolution))

    areauv_total, flipped = _signed_uv_areas(mesh, uvmap)
    overlap_ratio = (flipped / areauv_total) if areauv_total > 1e-12 else 0.0

    worst = max((row["distortion"] for row in islands_summary), default=0.0)

    bounds = uv_bounds(uvmap, tol=bounds_tol)
    metrics = {
        "stretch_score": round(_stretch_score(mesh, uvmap), 6),
        "worst_island_distortion": round(float(worst), 6),
        "overlap_ratio": round(float(overlap_ratio), 6),
        "raster_overlap_ratio": raster["raster_overlap_ratio"],
        "self_overlap_ratio": raster["self_overlap_ratio"],
        "cross_overlap_ratio": raster["cross_overlap_ratio"],
        "texel_density_variance": round(_texel_density_variance(islands_summary), 6),
        "packing_efficiency": round(packing_efficiency(mesh, uvmap), 6),
    }
    uv_block = {
        "island_count": len([i for i in islands if i]),
        "uv_bounds": bounds,
        "has_negative_uv": bool(bounds["min"][0] < -bounds_tol or bounds["min"][1] < -bounds_tol),
        "has_out_of_tile_uv": not bounds["in_0_1"],
    }
    return {"metrics": metrics, "uv": uv_block, "islands": islands_summary}


# Distinct, readable island colors (cycled) — matches uv_agent.geometry.preview.
_PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]


def uv_layout_svg(
    mesh: MeshGraph,
    uvmap: UVMap,
    islands: list[list[int]] | None = None,
    *,
    size: int = 512,
    title: str | None = None,
) -> str:
    """Render the existing UV layout to an SVG (plan §7 optional ``uv_layout.svg``).

    Pure Python so it always succeeds, unlike Blender's ``uv.export_layout``
    operator which is context-sensitive (plan §13). The worker uses this both as
    the optional SVG artifact and as a guaranteed fallback when the PNG export
    fails. Islands (loop-UV charts) are colored distinctly; the [0,1] tile is drawn
    with a 0.25 grid. UVs outside [0,1] are clamped into view, not hidden.
    """
    if islands is None:
        islands = uv_islands_from_uvmap(mesh, uvmap)
    pad = 16
    inner = size

    def sx(u: float) -> float:
        return pad + max(-0.5, min(1.5, u)) * inner

    def sy(v: float) -> float:
        return pad + (1.0 - max(-0.5, min(1.5, v))) * inner

    w = size + 2 * pad
    h = size + 2 * pad + (24 if title else 0)
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="#1e1e1e"/>',
        f'<rect x="{pad}" y="{pad}" width="{inner}" height="{inner}" '
        f'fill="#2b2b2b" stroke="#555" stroke-width="1"/>',
    ]
    for t in (0.25, 0.5, 0.75):
        parts.append(f'<line x1="{sx(t):.1f}" y1="{pad}" x2="{sx(t):.1f}" y2="{pad + inner}" stroke="#3a3a3a"/>')
        parts.append(f'<line x1="{pad}" y1="{sy(t):.1f}" x2="{pad + inner}" y2="{sy(t):.1f}" stroke="#3a3a3a"/>')

    for idx, faces in enumerate(islands):
        color = _PALETTE[idx % len(_PALETTE)]
        for fid in faces:
            loops = mesh.faces[fid].loop_indices
            pts = " ".join(f"{sx(uvmap.get(li)[0]):.2f},{sy(uvmap.get(li)[1]):.2f}" for li in loops)
            parts.append(
                f'<polygon points="{pts}" fill="{color}" fill-opacity="0.55" '
                f'stroke="{color}" stroke-width="0.75"/>'
            )

    if title:
        safe = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts.append(
            f'<text x="{pad}" y="{h - 8}" fill="#ddd" font-family="monospace" font-size="13">{safe}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


_PALETTE_RGB = [_hex_rgb(c) for c in _PALETTE]


def rasterize_uv_layout(
    mesh: MeshGraph,
    uvmap: UVMap,
    islands: list[list[int]] | None = None,
    *,
    size: int = 1024,
    alpha: float = 0.62,
):
    """Rasterize the existing UV layout to an ``(size, size, 4)`` uint8 RGBA array.

    NumPy-only (no Blender, no GPU), so it produces ``uv_layout.png`` headlessly —
    Blender's ``uv.export_layout`` PNG mode needs GPU drawing unavailable under
    ``--background`` (plan §7, §13). Islands (loop-UV charts) are filled with
    distinct colors over the [0,1] tile; the 0.25 grid is drawn for scale.
    """
    import numpy as np

    if islands is None:
        islands = uv_islands_from_uvmap(mesh, uvmap)
    S = int(size)
    margin = max(2, int(round(S * 0.04)))
    inner = S - 2 * margin

    canvas = np.empty((S, S, 4), dtype=np.uint8)
    canvas[:] = (30, 31, 36, 255)  # background
    canvas[margin:S - margin, margin:S - margin] = (43, 43, 43, 255)  # [0,1] tile
    for t in (0.25, 0.5, 0.75):  # grid lines
        gx = margin + int(round(t * inner))
        gy = margin + int(round((1.0 - t) * inner))
        canvas[margin:S - margin, gx] = (58, 58, 58, 255)
        canvas[gy, margin:S - margin] = (58, 58, 58, 255)

    def to_px(uv):
        return margin + uv[0] * inner, margin + (1.0 - uv[1]) * inner

    for cid, faces in enumerate(islands):
        color = np.array(_PALETTE_RGB[cid % len(_PALETTE_RGB)], dtype=np.float32)
        for fid in faces:
            for l0, l1, l2 in _tris_from_face(mesh.faces[fid].loop_indices):
                tri = [to_px(uvmap.get(l0)), to_px(uvmap.get(l1)), to_px(uvmap.get(l2))]
                xs = [p[0] for p in tri]
                ys = [p[1] for p in tri]
                minx = max(0, int(np.floor(min(xs))))
                maxx = min(S - 1, int(np.ceil(max(xs))))
                miny = max(0, int(np.floor(min(ys))))
                maxy = min(S - 1, int(np.ceil(max(ys))))
                if maxx < minx or maxy < miny:
                    continue
                xr = np.arange(minx, maxx + 1) + 0.5
                yr = np.arange(miny, maxy + 1) + 0.5
                pgx, pgy = np.meshgrid(xr, yr)
                mask = _point_in_triangle(tri, pgx, pgy)
                if not mask.any():
                    continue
                region = canvas[miny:maxy + 1, minx:maxx + 1, :3].astype(np.float32)
                region[mask] = region[mask] * (1.0 - alpha) + color * alpha
                canvas[miny:maxy + 1, minx:maxx + 1, :3] = region.astype(np.uint8)
    return canvas


def write_uv_layout_png(
    mesh: MeshGraph,
    uvmap: UVMap,
    path: str,
    islands: list[list[int]] | None = None,
    *,
    size: int = 1024,
) -> str:
    """Rasterize the UV layout and write it as ``path`` (PNG). Returns ``path``."""
    from uv_agent.io.png import write_png

    return write_png(path, rasterize_uv_layout(mesh, uvmap, islands, size=size))
