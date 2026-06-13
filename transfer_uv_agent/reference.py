"""T1 — reference chart extraction (UV_TRANSFER_PLAN §3.T1).

Recover the reference asset's UV islands from its artist ``vt`` data and, per chart,
record the geometry the placement step (T4) needs: face set, UV bbox (center +
half-extents), principal UV axis (PCA), mean texel density (UV area / 3D area), and a
small raster footprint mask for the IoU rotation search. Pure Python / numpy on a
:class:`MeshGraph` + :class:`UVMap` — no Blender (the BVH lives in ``pipeline``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from uv_agent.geometry.evaluation import uv_islands_from_uvmap
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


def _face_uv_points(mesh: MeshGraph, face_ids, uvmap: UVMap) -> np.ndarray:
    """All per-loop UV coordinates of ``face_ids`` as an ``(N, 2)`` array."""
    pts = []
    for fid in face_ids:
        for li in mesh.faces[fid].loop_indices:
            pts.append(uvmap.get(li))
    return np.asarray(pts, dtype=float) if pts else np.zeros((0, 2), dtype=float)


def _tris(loop_indices):
    for i in range(1, len(loop_indices) - 1):
        yield loop_indices[0], loop_indices[i], loop_indices[i + 1]


def chart_uv_area(mesh: MeshGraph, face_ids, uvmap: UVMap) -> float:
    """Absolute UV area of a chart (sum of |signed-tri-area| over its faces)."""
    a = 0.0
    for fid in face_ids:
        for l0, l1, l2 in _tris(mesh.faces[fid].loop_indices):
            p, q, r = uvmap.get(l0), uvmap.get(l1), uvmap.get(l2)
            a += abs(0.5 * ((q[0] - p[0]) * (r[1] - p[1]) - (r[0] - p[0]) * (q[1] - p[1])))
    return a


def chart_area_3d(mesh: MeshGraph, face_ids) -> float:
    return float(sum(mesh.faces[fid].area_3d for fid in face_ids))


def principal_axis(points: np.ndarray) -> np.ndarray:
    """Unit principal (max-variance) axis of a 2D point cloud via PCA. Sign is arbitrary
    (the placement step tries all 4 axis-aligned orientations)."""
    if len(points) < 2:
        return np.array([1.0, 0.0])
    c = points - points.mean(axis=0)
    cov = c.T @ c
    vals, vecs = np.linalg.eigh(cov)
    axis = vecs[:, int(np.argmax(vals))]
    n = float(np.linalg.norm(axis))
    return axis / n if n > 1e-12 else np.array([1.0, 0.0])


def raster_mask(tris_uv, *, bbox, resolution: int) -> np.ndarray:
    """Rasterise a list of UV triangles into a ``resolution``² boolean mask, normalised
    so ``bbox`` (min_u, min_v, max_u, max_v) maps to the full grid. Used for the IoU
    rotation search (T4.2) — both the reference footprint and each candidate placement
    are rasterised in the SAME normalised frame so the overlap is comparable."""
    from uv_agent.geometry.evaluation import _point_in_triangle

    R = int(resolution)
    mask = np.zeros((R, R), dtype=bool)
    minu, minv, maxu, maxv = bbox
    du = max(maxu - minu, 1e-9)
    dv = max(maxv - minv, 1e-9)
    for tri in tris_uv:
        t = np.asarray(tri, dtype=float)
        nx = (t[:, 0] - minu) / du * R
        ny = (t[:, 1] - minv) / dv * R
        ntri = np.column_stack([nx, ny])
        minx = max(0, int(np.floor(nx.min())))
        maxx = min(R - 1, int(np.ceil(nx.max())))
        miny = max(0, int(np.floor(ny.min())))
        maxy = min(R - 1, int(np.ceil(ny.max())))
        if maxx < minx or maxy < miny:
            continue
        xs = np.arange(minx, maxx + 1) + 0.5
        ys = np.arange(miny, maxy + 1) + 0.5
        px, py = np.meshgrid(xs, ys)
        hit = _point_in_triangle(ntri, px, py)
        if hit.any():
            mask[miny:maxy + 1, minx:maxx + 1] |= hit
    return mask


@dataclass
class RefChart:
    """One reference UV island and the placement metadata derived from it (T1.3)."""

    chart_id: int
    face_ids: list[int]
    uv_min: tuple[float, float]
    uv_max: tuple[float, float]
    center: tuple[float, float]
    half_extents: tuple[float, float]
    principal: tuple[float, float]
    texel_density: float          # UV area / 3D area
    uv_area: float
    area_3d: float
    footprint: np.ndarray = field(repr=False, default=None)  # raster mask in own bbox frame
    abs_footprint: np.ndarray = field(repr=False, default=None)  # raster mask in the [0,1] tile

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.uv_min[0], self.uv_min[1], self.uv_max[0], self.uv_max[1])

    def to_dict(self) -> dict:
        return {"chart_id": self.chart_id, "faces": len(self.face_ids),
                "center": [round(c, 5) for c in self.center],
                "half_extents": [round(c, 5) for c in self.half_extents],
                "principal": [round(c, 4) for c in self.principal],
                "texel_density": round(self.texel_density, 6),
                "uv_area": round(self.uv_area, 6)}


def extract_reference_charts(mesh: MeshGraph, uvmap: UVMap, *, tol: float = 1e-5,
                             footprint_res: int = 256) -> list[RefChart]:
    """Extract the reference's UV islands (T1). Each island → a :class:`RefChart` with
    bbox/center/principal-axis/texel-density + a raster footprint for IoU matching."""
    islands = uv_islands_from_uvmap(mesh, uvmap, tol=tol)
    charts: list[RefChart] = []
    for cid, face_ids in enumerate(islands):
        pts = _face_uv_points(mesh, face_ids, uvmap)
        if len(pts) == 0:
            continue
        umin, vmin = pts.min(axis=0)
        umax, vmax = pts.max(axis=0)
        center = ((umin + umax) / 2.0, (vmin + vmax) / 2.0)
        half = ((umax - umin) / 2.0, (vmax - vmin) / 2.0)
        area_uv = chart_uv_area(mesh, face_ids, uvmap)
        area3 = chart_area_3d(mesh, face_ids)
        density = (area_uv / area3) if area3 > 1e-12 else 0.0
        tris = [[uvmap.get(l0), uvmap.get(l1), uvmap.get(l2)]
                for fid in face_ids for l0, l1, l2 in _tris(mesh.faces[fid].loop_indices)]
        fp = raster_mask(tris, bbox=(umin, vmin, umax, vmax), resolution=footprint_res)
        abs_fp = raster_mask(tris, bbox=(0.0, 0.0, 1.0, 1.0), resolution=footprint_res)
        charts.append(RefChart(
            chart_id=cid, face_ids=list(face_ids),
            uv_min=(float(umin), float(vmin)), uv_max=(float(umax), float(vmax)),
            center=(float(center[0]), float(center[1])),
            half_extents=(float(half[0]), float(half[1])),
            principal=tuple(float(x) for x in principal_axis(pts)),
            texel_density=float(density), uv_area=float(area_uv), area_3d=float(area3),
            footprint=fp, abs_footprint=abs_fp,
        ))
    return charts
