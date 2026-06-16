"""Debug overlays (AUTO_ARTIST_UV_PLAN §7 / §12). Reviewing semantic segmentation is
guesswork without them, so they are mandatory deliverables.

Pure helpers (no Blender):

- :func:`parts_uv_svg`        — the UV layout coloured by part, as a self-contained SVG.
- :func:`rasterize_parts`     — an RGBA numpy raster of the UV coloured by part; the
                                worker saves it as ``*_uv_colored_by_part.png`` via the
                                Blender image API (the same path P6 uses for stitches).
- :func:`part_debug_rows`     — the per-part debug table (id, type, faces, area, conf,
                                parent, mate, chart ids, repair history).

The 3D ``part_debug_front/side`` renders are produced in the worker (a per-part material
+ the fixed-camera render path); this module owns the UV-space and tabular overlays.
"""

from __future__ import annotations

import colorsys

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


def part_color(pid: int) -> tuple[float, float, float]:
    """A deterministic, well-separated RGB for a part id (golden-ratio hue hopping)."""
    h = (pid * 0.61803398875) % 1.0
    s = 0.55 + 0.25 * ((pid * 7) % 3) / 2.0
    v = 0.95 - 0.25 * ((pid * 5) % 2)
    return colorsys.hsv_to_rgb(h, s, v)


def _chart_tris(mesh: MeshGraph, faces, uvmap: UVMap):
    for f in faces:
        li = mesh.faces[f].loop_indices
        for i in range(1, len(li) - 1):
            yield (uvmap.get(li[0]), uvmap.get(li[i]), uvmap.get(li[i + 1]))


def parts_uv_svg(mesh: MeshGraph, uvmap: UVMap, chart_to_part: dict[int, int],
                 charts, *, size: int = 1024) -> str:
    """Return an SVG string: every UV island filled with its part colour, seams implied by
    the gaps between islands. ``charts`` is the flooded chart face-lists (index = chart id)."""
    body = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 1 1">',
            '<rect width="1" height="1" fill="#111"/>']
    for cid, faces in enumerate(charts):
        r, g, b = part_color(chart_to_part.get(cid, cid))
        col = f'#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}'
        for (a, bb, c) in _chart_tris(mesh, faces, uvmap):
            # SVG y grows downward; flip v so the layout reads the same as the UV editor.
            pts = " ".join(f"{p[0]:.5f},{1.0 - p[1]:.5f}" for p in (a, bb, c))
            body.append(f'<polygon points="{pts}" fill="{col}" stroke="none"/>')
    body.append("</svg>")
    return "\n".join(body)


def _point_in_tri(px, py, a, b, c) -> bool:
    d1 = (px - b[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (py - b[1])
    d2 = (px - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (py - c[1])
    d3 = (px - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (py - a[1])
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


def rasterize_parts(mesh: MeshGraph, uvmap: UVMap, chart_to_part: dict[int, int],
                    charts, *, resolution: int = 512) -> np.ndarray:
    """RGBA float raster (``resolution²×4``, rows bottom→top to match Blender image data)
    of the UV coloured by part. The worker saves it via ``bpy.data.images``. Scan-converts
    each island triangle; cheap O(tris · bbox) rasteriser (no Blender / PIL)."""
    img = np.zeros((resolution, resolution, 4), dtype=np.float64)
    img[:, :, 3] = 1.0
    img[:, :, :3] = 0.07
    for cid, faces in enumerate(charts):
        r, g, b = part_color(chart_to_part.get(cid, cid))
        for (a, bb, c) in _chart_tris(mesh, faces, uvmap):
            xs = [a[0], bb[0], c[0]]
            ys = [a[1], bb[1], c[1]]
            x0 = max(0, int(np.floor(min(xs) * resolution)))
            x1 = min(resolution - 1, int(np.ceil(max(xs) * resolution)))
            y0 = max(0, int(np.floor(min(ys) * resolution)))
            y1 = min(resolution - 1, int(np.ceil(max(ys) * resolution)))
            for py in range(y0, y1 + 1):
                fy = (py + 0.5) / resolution
                for px in range(x0, x1 + 1):
                    fx = (px + 0.5) / resolution
                    if _point_in_tri(fx, fy, a, bb, c):
                        img[py, px, :3] = (r, g, b)
    return img


def part_debug_rows(parts, descriptors, classes, seam_result) -> list[dict]:
    """Per-part debug table (plan §7 ``artist_parts.json`` content): id, type, faces, area,
    confidence, parent (symmetry mate), chart ids, and repair history touching the part."""
    desc_by = {d.part_id: d for d in descriptors}
    class_by = {c.part_id: c for c in classes}
    rows = []
    for p in parts:
        d = desc_by.get(p.part_id)
        c = class_by.get(p.part_id)
        rows.append({
            "part_id": p.part_id,
            "type": c.type if c else "unknown",
            "type_confidence": round(c.confidence, 3) if c else 0.0,
            "type_reason": c.reason if c else "",
            "face_count": p.face_count,
            "area": round(d.area, 6) if d else 0.0,
            "area_frac": round(d.area_frac, 4) if d else 0.0,
            "segmentation_confidence": round(p.confidence, 3),
            "symmetry_mate": d.symmetry_mate if d else -1,
            "neighbors": sorted(p.neighbors),
            "chart_ids": seam_result.part_charts.get(p.part_id, []),
        })
    return rows
