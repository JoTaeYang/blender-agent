"""Normal / visual cleanup for Decimation Optimize mode (Phase D4, decimation §6.4).

A freshly decimated triangle LOD shades *flat* -- every face uses its own
geometric normal -- so on a curved surface the shading visibly facets and the
per-face normal deviates a lot from the original smooth surface. Phase D4 reduces
those artifacts with Auto Smooth + Weighted Normal + normal transfer (plan §6.4,
§7). Inside Blender those are modifiers; this module is the Blender-free core that
(a) computes the *shading* normals Auto Smooth would produce and (b) measures the
improvement, so the effect is unit-testable offline.

**Auto Smooth / smoothing split.** A vertex is split into one shading normal per
*smoothing group* -- the fan of incident faces reachable through edges whose
dihedral angle is below ``auto_smooth_angle`` (sharp edges and boundaries break a
fan). Each group's shading normal is the (optionally area-weighted -- the
"Weighted Normal" modifier) average of its faces' normals. So a smooth dome
averages into a single rounded normal, while a cube corner keeps three separate
flat normals -- creases stay crisp, curves shade smoothly.

**Improvement metric.** ``evaluate_normal_cleanup`` samples low-poly faces, finds
the nearest high-poly triangle, and compares the high normal against both the
low-poly *flat* face normal (before) and the *smoothed shading* normal (after).
The mean angles' difference is the cleanup's gain (decimation plan §7 completion:
"normal deviation ... should improve").
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from retopo_agent.geometry.features import DEFAULT_FEATURE_ANGLE
from retopo_agent.geometry.shape_eval import (
    _folded_angle_deg,
    _nearest,
    _stride,
    _triangulate_arrays,
)
from uv_agent.geometry.mesh_graph import MeshGraph


class _DSU:
    """Tiny union-find over a fixed set of items (per-vertex face fans)."""

    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _vertex_incidence(mesh: MeshGraph):
    """``(vertex_id -> [face_id], vertex_id -> [Edge])`` adjacency."""
    vfaces: dict[int, list[int]] = {v.id: [] for v in mesh.vertices}
    for f in mesh.faces:
        for vid in dict.fromkeys(f.vertex_ids):  # de-dup, keep order
            vfaces[vid].append(f.id)
    vedges: dict[int, list] = {v.id: [] for v in mesh.vertices}
    for e in mesh.edges:
        a, b = e.vertex_ids
        vedges[a].append(e)
        vedges[b].append(e)
    return vfaces, vedges


def face_corner_normals(
    mesh: MeshGraph,
    *,
    auto_smooth_angle: float = DEFAULT_FEATURE_ANGLE,
    weighted: bool = True,
) -> dict[tuple[int, int], np.ndarray]:
    """Per-corner shading normals: ``(face_id, vertex_id) -> unit normal``.

    At each vertex, incident faces are grouped into smoothing fans (connected
    through edges with dihedral ``< auto_smooth_angle``); a corner gets its fan's
    averaged normal. ``weighted`` area-weights the average (Weighted Normal)."""
    vfaces, vedges = _vertex_incidence(mesh)
    face_normal = {f.id: np.asarray(f.normal, dtype=float) for f in mesh.faces}
    face_weight = {f.id: (max(f.area_3d, 1e-12) if weighted else 1.0) for f in mesh.faces}

    out: dict[tuple[int, int], np.ndarray] = {}
    for v in mesh.vertices:
        faces_here = vfaces[v.id]
        if not faces_here:
            continue
        dsu = _DSU(faces_here)
        for e in vedges[v.id]:
            if e.is_boundary or len(e.face_ids) != 2:
                continue  # boundary / non-manifold -> always a split
            if e.dihedral_angle >= auto_smooth_angle:
                continue  # sharp crease -> split the fan here
            dsu.union(e.face_ids[0], e.face_ids[1])

        group_acc: dict[int, np.ndarray] = {}
        for fid in faces_here:
            root = dsu.find(fid)
            group_acc.setdefault(root, np.zeros(3))
            group_acc[root] = group_acc[root] + face_weight[fid] * face_normal[fid]
        group_normal: dict[int, np.ndarray] = {}
        for root, acc in group_acc.items():
            length = float(np.linalg.norm(acc))
            group_normal[root] = acc / length if length > 1e-12 else face_normal[faces_here[0]]

        for fid in faces_here:
            out[(fid, v.id)] = group_normal[dsu.find(fid)]
    return out


def face_shading_normals(
    mesh: MeshGraph,
    *,
    auto_smooth_angle: float = DEFAULT_FEATURE_ANGLE,
    weighted: bool = True,
) -> np.ndarray:
    """Per-face shading normal ``(face_count, 3)``: the unit mean of a face's three
    corner (loop) normals. Equals the flat face normal where all corners belong to
    one-face groups (e.g. a crease), and the smooth normal on curved surfaces."""
    corners = face_corner_normals(mesh, auto_smooth_angle=auto_smooth_angle, weighted=weighted)
    out = np.zeros((mesh.face_count, 3), dtype=float)
    for i, f in enumerate(mesh.faces):
        acc = np.zeros(3)
        for vid in f.vertex_ids:
            acc = acc + corners.get((f.id, vid), np.asarray(f.normal, dtype=float))
        length = float(np.linalg.norm(acc))
        out[i] = acc / length if length > 1e-12 else np.asarray(f.normal, dtype=float)
    return out


@dataclass
class NormalCleanupReport:
    auto_smooth_angle: float
    weighted: bool
    sample_count: int
    normal_deviation_mean_deg_flat: float
    normal_deviation_mean_deg_smoothed: float

    @property
    def improvement_deg(self) -> float:
        """Drop in mean normal deviation (flat minus smoothed); > 0 means better."""
        return self.normal_deviation_mean_deg_flat - self.normal_deviation_mean_deg_smoothed

    @property
    def status(self) -> str:
        if self.improvement_deg > 0.01:
            return "improved"
        if self.improvement_deg < -0.01:
            return "regressed"
        return "unchanged"

    def to_dict(self) -> dict:
        return {
            "auto_smooth_angle_deg": self.auto_smooth_angle,
            "weighted": self.weighted,
            "sample_count": self.sample_count,
            "normal_deviation_mean_deg_flat": round(self.normal_deviation_mean_deg_flat, 3),
            "normal_deviation_mean_deg_smoothed": round(self.normal_deviation_mean_deg_smoothed, 3),
            "improvement_deg": round(self.improvement_deg, 3),
            "status": self.status,
        }


def evaluate_normal_cleanup(
    high: MeshGraph,
    low: MeshGraph,
    *,
    auto_smooth_angle: float = DEFAULT_FEATURE_ANGLE,
    weighted: bool = True,
    max_normal_samples: int = 2000,
) -> NormalCleanupReport:
    """Measure how much Auto-Smooth shading normals reduce the low-poly's normal
    deviation from ``high`` versus flat per-face shading (plan §7 Phase D4)."""
    A, B, C, tri_normals = _triangulate_arrays(high)
    if len(A) == 0 or low.face_count == 0:
        return NormalCleanupReport(auto_smooth_angle, weighted, 0, 0.0, 0.0)

    co = np.asarray([v.co for v in low.vertices], dtype=float)
    centroids = np.asarray([co[f.vertex_ids].mean(axis=0) for f in low.faces], dtype=float)
    flat_normals = np.asarray([f.normal for f in low.faces], dtype=float)
    smooth_normals = face_shading_normals(low, auto_smooth_angle=auto_smooth_angle, weighted=weighted)

    flat_devs: list[float] = []
    smooth_devs: list[float] = []
    for fi in _stride(low.face_count, max_normal_samples):
        _, idx = _nearest(centroids[fi], A, B, C)
        high_n = tri_normals[idx]
        flat_devs.append(_folded_angle_deg(flat_normals[fi], high_n))
        smooth_devs.append(_folded_angle_deg(smooth_normals[fi], high_n))

    return NormalCleanupReport(
        auto_smooth_angle=auto_smooth_angle,
        weighted=weighted,
        sample_count=len(flat_devs),
        normal_deviation_mean_deg_flat=float(np.mean(flat_devs)) if flat_devs else 0.0,
        normal_deviation_mean_deg_smoothed=float(np.mean(smooth_devs)) if smooth_devs else 0.0,
    )
