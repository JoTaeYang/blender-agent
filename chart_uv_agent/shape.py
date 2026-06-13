"""Phase U1.6 — chart shape metrics (chart-UV plan §5b).

The chart *composition* (part-based, uniform density) matched the reference, but the
chart *shapes* did not: region-growing leaves jagged boundaries, thin tendrils, and
deep concavities — the direct cause of the packing holes. These metrics quantify that,
and (per "do not invent thresholds") the bars are calibrated by running the SAME code
on the reference's own 39 charts (see :func:`measure_charts`).

Pure / numpy on a :class:`MeshGraph`. Convexity is measured on a per-chart best-fit-plane
(PCA) projection of the 3D faces, so it works before any UV unwrap and is identical for
our charts and the reference's charts.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph


# ----------------------------------------------------------------- geometry utils

def _pca_project(coords: np.ndarray) -> np.ndarray:
    """Project (n,3) points onto their best-fit plane → (n,2)."""
    c = coords - coords.mean(axis=0)
    # Right singular vectors are the principal axes; take the top two.
    _, _, vt = np.linalg.svd(c, full_matrices=False)
    return c @ vt[:2].T


def _convex_hull_area(points: np.ndarray) -> float:
    """Area of the 2D convex hull (Andrew monotone chain + shoelace)."""
    pts = sorted({(round(float(x), 9), round(float(y), 9)) for x, y in points})
    if len(pts) < 3:
        return 0.0

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    area = 0.0
    n = len(hull)
    for i in range(n):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) * 0.5


# ----------------------------------------------------------------- chart shape metrics

def chart_convexity(mesh: MeshGraph, face_ids) -> float:
    """Filled chart area / convex-hull area in the chart's best-fit plane (chart-UV plan
    §5b). 1.0 = convex; deep pockets / tendrils drag it down. The direct packing-hole
    proxy. Returns 1.0 for a degenerate (≤2-face) chart."""
    if len(face_ids) < 2:
        return 1.0
    vids = sorted({v for f in face_ids for v in mesh.faces[f].vertex_ids})
    coords = np.array([mesh.vertices[v].co for v in vids], dtype=float)
    proj = _pca_project(coords)
    index = {v: i for i, v in enumerate(vids)}
    filled = 0.0
    for f in face_ids:
        loop = mesh.faces[f].vertex_ids
        for i in range(1, len(loop) - 1):
            a = proj[index[loop[0]]]
            b = proj[index[loop[i]]]
            c = proj[index[loop[i + 1]]]
            filled += abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) * 0.5
    hull = _convex_hull_area(proj)
    if hull <= 1e-12:
        return 1.0
    # Clamp at 1.0: filled > hull happens only when a CURVED chart folds over in the
    # planar projection (overlap), which is not a concavity/packing problem — a chart
    # cannot be "more than convex". Clamping makes the metric measure concavity cleanly.
    return float(min(filled / hull, 1.0))


def _boundary_edges_of_chart(mesh: MeshGraph, face_set: set[int], seams: set[int]):
    """Seam edges on this chart's border (both/one face in the chart)."""
    out = []
    for eid in seams:
        fids = mesh.edges[eid].face_ids
        if any(f in face_set for f in fids):
            out.append(eid)
    return out


def thin_faces(mesh: MeshGraph, face_set: set[int], seams: set[int]) -> set[int]:
    """Faces with ≥2 of their edges on the chart boundary — a chart is ≤2 faces wide
    there (a tendril/sliver finger)."""
    out: set[int] = set()
    for f in face_set:
        n = sum(1 for eid in mesh.faces[f].edge_ids if eid in seams)
        if n >= 2:
            out.add(f)
    return out


def tendril_chains(mesh: MeshGraph, face_set: set[int], seams: set[int], *,
                   min_len: int = 4) -> list[list[int]]:
    """Connected chains of thin faces longer than ``min_len`` — the tendrils to amputate
    (chart-UV plan §5b op 2 / shape gate). Returns the chains (lists of face ids)."""
    thin = thin_faces(mesh, face_set, seams)
    adjacency = mesh.face_adjacency()
    seen: set[int] = set()
    chains: list[list[int]] = []
    for f in thin:
        if f in seen:
            continue
        chain: list[int] = []
        q = deque([f])
        seen.add(f)
        while q:
            cur = q.popleft()
            chain.append(cur)
            for nb, _eid in adjacency[cur]:
                if nb in thin and nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        if len(chain) > min_len:
            chains.append(chain)
    return chains


def boundary_smoothness(mesh: MeshGraph, seams: set[int], mandatory: set[int]) -> dict:
    """Per non-mandatory boundary segment, ratio of its edge count to the straight-line
    (Euclidean) distance between its endpoints, normalised by mean edge length — a
    staircase boundary has a high ratio, a straight one ≈ 1 (chart-UV plan §5b gate).
    Returns mean / p90 over segments."""
    segs = _boundary_segments(mesh, seams, mandatory)
    ratios: list[float] = []
    for seg in segs:
        if len(seg) < 2:
            continue
        verts = _segment_endpoints(mesh, seg)
        if verts is None:
            continue
        a, b = verts
        straight = float(np.linalg.norm(mesh.vertex_co(a) - mesh.vertex_co(b)))
        length = sum(float(np.linalg.norm(mesh.vertex_co(mesh.edges[e].vertex_ids[0])
                                          - mesh.vertex_co(mesh.edges[e].vertex_ids[1]))) for e in seg)
        if straight > 1e-9:
            ratios.append(length / straight)
    if not ratios:
        return {"mean": 1.0, "p90": 1.0, "segments": 0}
    arr = np.asarray(ratios)
    return {"mean": round(float(arr.mean()), 4), "p90": round(float(np.percentile(arr, 90)), 4),
            "segments": int(arr.size)}


def _vertex_seam_degree(mesh: MeshGraph, seams: set[int]) -> dict[int, int]:
    deg: dict[int, int] = {}
    for eid in seams:
        for v in mesh.edges[eid].vertex_ids:
            deg[v] = deg.get(v, 0) + 1
    return deg


def _boundary_segments(mesh: MeshGraph, seams: set[int], mandatory: set[int]) -> list[list[int]]:
    """Split the non-mandatory seam graph into maximal simple paths between junction
    vertices (seam-degree ≠ 2)."""
    free = seams - mandatory
    deg = _vertex_seam_degree(mesh, seams)  # junctions use the FULL seam degree
    vadj: dict[int, list[tuple[int, int]]] = {}
    for eid in free:
        a, b = mesh.edges[eid].vertex_ids
        vadj.setdefault(a, []).append((b, eid))
        vadj.setdefault(b, []).append((a, eid))
    visited: set[int] = set()
    segments: list[list[int]] = []

    def walk(start_v, start_e):
        seg = [start_e]
        visited.add(start_e)
        prev, cur = start_v, _other(mesh, start_e, start_v)
        while deg.get(cur, 0) == 2 and any(e not in visited for _, e in vadj.get(cur, [])):
            nxt = next(((w, e) for w, e in vadj[cur] if e not in visited), None)
            if nxt is None:
                break
            w, e = nxt
            seg.append(e)
            visited.add(e)
            prev, cur = cur, w
        return seg

    for eid in free:
        if eid in visited:
            continue
        a, _ = mesh.edges[eid].vertex_ids
        segments.append(walk(a, eid))
    return segments


def _other(mesh, eid, v):
    a, b = mesh.edges[eid].vertex_ids
    return b if a == v else a


def _segment_endpoints(mesh, seg_edges):
    """Endpoints of a path of edges (the two vertices touched once)."""
    count: dict[int, int] = {}
    for e in seg_edges:
        for v in mesh.edges[e].vertex_ids:
            count[v] = count.get(v, 0) + 1
    ends = [v for v, c in count.items() if c == 1]
    if len(ends) != 2:
        return None
    return ends[0], ends[1]


def measure_charts(mesh: MeshGraph, charts, seams: set[int], mandatory: set[int]) -> dict:
    """Run every shape metric over a chart set — used both to calibrate the bars on the
    reference's 39 charts and to gate our own charts (chart-UV plan §5b: same code)."""
    convex = [chart_convexity(mesh, fs) for fs in charts]
    tendrils = sum(len(tendril_chains(mesh, set(fs), seams)) for fs in charts)
    smooth = boundary_smoothness(mesh, seams, mandatory)
    arr = np.asarray(convex) if convex else np.array([1.0])
    return {
        "chart_count": len(charts),
        "convexity_mean": round(float(arr.mean()), 4),
        "convexity_p10": round(float(np.percentile(arr, 10)), 4),
        "convexity_min": round(float(arr.min()), 4),
        "boundary_smoothness_mean": smooth["mean"],
        "boundary_smoothness_p90": smooth["p90"],
        "tendril_count": int(tendrils),
    }
