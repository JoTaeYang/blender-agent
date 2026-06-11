"""Shape-preservation evaluator (retopology plan §6.7, §10 Phase 3, Ticket 6).

Phase 3 answers, numerically, "how well does the low-poly keep the high-poly's
shape?" -- the spec's reminder that a result must be judged by more than reduced
polygon count. It is pure Python on :class:`~uv_agent.geometry.mesh_graph.MeshGraph`
(no Blender), so it is unit-testable offline; the Blender adapter
(:mod:`retopo_agent.blender.shape`) computes the same metrics with a BVH tree for
meshes too large to brute-force here.

Metrics (plan §6.7 / §15.4):

- ``surface_distance_mean`` / ``surface_distance_max`` -- distance from points
  sampled on the low-poly to the nearest point on the high-poly surface,
  reported both absolutely and as a ratio of the high-poly bounding-box diagonal
  (``surface_distance_ratio = distance / bounding_box_diagonal``, §15.6);
- ``normal_deviation_mean_deg`` -- mean angle between each low-poly face normal
  and the nearest high-poly face normal, folded into ``[0, 90]`` so a flipped
  winding does not masquerade as a 180-degree error;
- ``volume_error_ratio`` -- relative change in enclosed volume (informational).

Status bands (worst gating metric wins, §15.6):

    surface_distance_mean_ratio  <=0.01 accepted | <=0.03 retry | >0.03 failed
    surface_distance_max_ratio   <=0.05 accepted | <=0.10 retry | >0.10 failed
    normal_deviation_mean_deg    <=12   accepted | <=25   retry | >25   failed

``silhouette_error`` and ``curvature_preservation_score`` (§6.7) are deferred to a
later phase and not fabricated here; ``volume_error_ratio`` is reported but not
gated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from retopo_agent.geometry.decimate import bounding_box_diagonal
from uv_agent.geometry.mesh_graph import MeshGraph

# §15.6 shape thresholds (lower is better).
SURF_MEAN_ACCEPTED = 0.01
SURF_MEAN_RETRY = 0.03
SURF_MAX_ACCEPTED = 0.05
SURF_MAX_RETRY = 0.10
NORMAL_DEV_ACCEPTED = 12.0
NORMAL_DEV_RETRY = 25.0

_BAND_RANK = {"accepted": 0, "retry": 1, "failed": 2}


@dataclass(frozen=True)
class ShapeThresholds:
    """Accepted/retry cutoffs for the gating shape metrics (worst band wins).

    The defaults are the §15.6 quad-retopo thresholds. The decimation plan §6.5
    keeps the same surface-distance cutoffs but tolerates more normal deviation
    on a triangle LOD (``<= 20deg`` accepted vs ``<= 12deg``), so it is expressed
    as a separate instance rather than by duplicating the evaluator."""

    surf_mean_accepted: float = SURF_MEAN_ACCEPTED
    surf_mean_retry: float = SURF_MEAN_RETRY
    surf_max_accepted: float = SURF_MAX_ACCEPTED
    surf_max_retry: float = SURF_MAX_RETRY
    normal_dev_accepted: float = NORMAL_DEV_ACCEPTED
    normal_dev_retry: float = NORMAL_DEV_RETRY


DEFAULT_SHAPE_THRESHOLDS = ShapeThresholds()
# Decimation Optimize mode (decimation plan §6.5): triangle LOD tolerates twice
# the normal deviation of quad retopo before it leaves the accepted band.
DECIMATION_SHAPE_THRESHOLDS = ShapeThresholds(normal_dev_accepted=20.0, normal_dev_retry=40.0)


def _le_band(value: float, accepted: float, retry: float) -> str:
    if value <= accepted:
        return "accepted"
    if value <= retry:
        return "retry"
    return "failed"


def _worst(bands: list[str]) -> str:
    return max(bands, key=lambda b: _BAND_RANK[b]) if bands else "accepted"


# -- triangle geometry -----------------------------------------------------


def _triangulate_arrays(mesh: MeshGraph) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fan-triangulate every face. Returns ``(A, B, C, tri_normals)`` where each
    of A/B/C is ``(M, 3)`` triangle corners and ``tri_normals`` is ``(M, 3)``
    (the source face normal, repeated per triangle)."""
    a: list = []
    b: list = []
    c: list = []
    normals: list = []
    co = np.asarray([v.co for v in mesh.vertices], dtype=float)
    for face in mesh.faces:
        vids = face.vertex_ids
        p0 = co[vids[0]]
        for k in range(1, len(vids) - 1):
            a.append(p0)
            b.append(co[vids[k]])
            c.append(co[vids[k + 1]])
            normals.append(face.normal)
    if not a:
        empty = np.zeros((0, 3))
        return empty, empty, empty, empty
    return np.asarray(a), np.asarray(b), np.asarray(c), np.asarray(normals)


def _safe_div(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(den != 0.0, num / den, 0.0)
    return out


def closest_points_on_triangles(p: np.ndarray, A: np.ndarray, B: np.ndarray, C: np.ndarray) -> np.ndarray:
    """Closest point on each triangle to ``p`` (Ericson, vectorised over M tris).

    ``p`` is ``(3,)``; ``A``/``B``/``C`` are ``(M, 3)``. Returns ``(M, 3)``.
    """
    ab = B - A
    ac = C - A
    ap = p - A
    d1 = (ab * ap).sum(1)
    d2 = (ac * ap).sum(1)

    bp = p - B
    d3 = (ab * bp).sum(1)
    d4 = (ac * bp).sum(1)

    cp = p - C
    d5 = (ab * cp).sum(1)
    d6 = (ac * cp).sum(1)

    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2

    denom = _safe_div(np.ones_like(va), va + vb + vc)
    v = vb * denom
    w = vc * denom
    res = A + ab * v[:, None] + ac * w[:, None]  # default: face interior

    # Region masks (mutually exclusive); apply lowest-priority first.
    reg_bc = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
    w_bc = _safe_div(d4 - d3, (d4 - d3) + (d5 - d6))
    res = np.where(reg_bc[:, None], B + (C - B) * w_bc[:, None], res)

    reg_ac = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
    w_ac = _safe_div(d2, d2 - d6)
    res = np.where(reg_ac[:, None], A + ac * w_ac[:, None], res)

    reg_ab = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
    v_ab = _safe_div(d1, d1 - d3)
    res = np.where(reg_ab[:, None], A + ab * v_ab[:, None], res)

    reg_c = (d6 >= 0) & (d5 <= d6)
    res = np.where(reg_c[:, None], C, res)

    reg_b = (d3 >= 0) & (d4 <= d3)
    res = np.where(reg_b[:, None], B, res)

    reg_a = (d1 <= 0) & (d2 <= 0)
    res = np.where(reg_a[:, None], A, res)

    return res


def _nearest(p: np.ndarray, A, B, C) -> tuple[float, int]:
    cps = closest_points_on_triangles(p, A, B, C)
    d = np.linalg.norm(cps - p, axis=1)
    idx = int(np.argmin(d))
    return float(d[idx]), idx


def mesh_volume(A: np.ndarray, B: np.ndarray, C: np.ndarray) -> float:
    """Absolute enclosed volume via the divergence theorem over triangles
    (meaningful for closed meshes; an approximation otherwise)."""
    if len(A) == 0:
        return 0.0
    signed = (A * np.cross(B, C)).sum(1).sum() / 6.0
    return abs(float(signed))


def _stride(n: int, k: int) -> list[int]:
    if n <= k:
        return list(range(n))
    step = n / k
    return [int(i * step) for i in range(k)]


# -- report ----------------------------------------------------------------


@dataclass
class ShapeReport:
    bounding_box_diagonal: float
    sample_count: int
    surface_distance_mean: float
    surface_distance_max: float
    normal_deviation_mean_deg: float
    volume_error_ratio: float | None
    status: str
    reasons: list[str] = field(default_factory=list)

    @property
    def surface_distance_mean_ratio(self) -> float:
        return self.surface_distance_mean / self.bounding_box_diagonal if self.bounding_box_diagonal else 0.0

    @property
    def surface_distance_max_ratio(self) -> float:
        return self.surface_distance_max / self.bounding_box_diagonal if self.bounding_box_diagonal else 0.0

    def to_dict(self) -> dict:
        return {
            "bounding_box_diagonal": round(self.bounding_box_diagonal, 6),
            "sample_count": self.sample_count,
            "surface_distance_mean": round(self.surface_distance_mean, 6),
            "surface_distance_max": round(self.surface_distance_max, 6),
            "surface_distance_mean_ratio": round(self.surface_distance_mean_ratio, 5),
            "surface_distance_max_ratio": round(self.surface_distance_max_ratio, 5),
            "normal_deviation_mean_deg": round(self.normal_deviation_mean_deg, 3),
            "volume_error_ratio": (round(self.volume_error_ratio, 5) if self.volume_error_ratio is not None else None),
            "status": self.status,
            "reasons": self.reasons,
        }


def build_shape_report(
    *,
    bbox_diagonal: float,
    distances: list[float],
    normal_angles_deg: list[float],
    volume_error_ratio: float | None,
    thresholds: ShapeThresholds = DEFAULT_SHAPE_THRESHOLDS,
) -> ShapeReport:
    """Assemble a :class:`ShapeReport` from raw measurements and apply the §15.6
    bands. Shared by the pure and Blender evaluators so they classify identically.

    ``thresholds`` selects the gating cutoffs -- the §15.6 quad-retopo set by
    default, or :data:`DECIMATION_SHAPE_THRESHOLDS` for Decimation Optimize mode."""
    if not distances:
        return ShapeReport(bbox_diagonal, 0, 0.0, 0.0, 0.0, volume_error_ratio, "failed",
                           ["no samples: empty low-poly mesh"])

    mean_d = float(np.mean(distances))
    max_d = float(np.max(distances))
    normal_mean = float(np.mean(normal_angles_deg)) if normal_angles_deg else 0.0

    report = ShapeReport(
        bounding_box_diagonal=bbox_diagonal,
        sample_count=len(distances),
        surface_distance_mean=mean_d,
        surface_distance_max=max_d,
        normal_deviation_mean_deg=normal_mean,
        volume_error_ratio=volume_error_ratio,
        status="accepted",
    )

    bands: list[str] = []
    reasons: list[str] = []
    mean_band = _le_band(report.surface_distance_mean_ratio, thresholds.surf_mean_accepted, thresholds.surf_mean_retry)
    bands.append(mean_band)
    if mean_band != "accepted":
        reasons.append(f"surface_distance_mean_ratio {report.surface_distance_mean_ratio:.4f} -> {mean_band}")
    max_band = _le_band(report.surface_distance_max_ratio, thresholds.surf_max_accepted, thresholds.surf_max_retry)
    bands.append(max_band)
    if max_band != "accepted":
        reasons.append(f"surface_distance_max_ratio {report.surface_distance_max_ratio:.4f} -> {max_band}")
    if normal_angles_deg:
        nd_band = _le_band(normal_mean, thresholds.normal_dev_accepted, thresholds.normal_dev_retry)
        bands.append(nd_band)
        if nd_band != "accepted":
            reasons.append(f"normal_deviation_mean_deg {normal_mean:.2f} -> {nd_band}")

    report.status = _worst(bands)
    report.reasons = reasons or ["all shape metrics within accepted thresholds"]
    return report


def evaluate_shape_match(
    high: MeshGraph,
    low: MeshGraph,
    *,
    max_distance_samples: int = 4000,
    max_normal_samples: int = 2000,
    thresholds: ShapeThresholds = DEFAULT_SHAPE_THRESHOLDS,
) -> ShapeReport:
    """Evaluate how closely ``low`` matches ``high`` (plan §6.7).

    Brute-forces nearest-surface distance from sampled low-poly points (vertices
    + face centroids) against the high-poly's triangles -- fine for the modest
    meshes used in tests; the Blender adapter uses a BVH tree for large meshes.

    ``thresholds`` selects the gating cutoffs (default quad-retopo; pass
    :data:`DECIMATION_SHAPE_THRESHOLDS` for Decimation Optimize mode).
    """
    diag = bounding_box_diagonal(high)
    A, B, C, tri_normals = _triangulate_arrays(high)
    if len(A) == 0 or low.face_count == 0:
        return ShapeReport(diag, 0, 0.0, 0.0, 0.0, None, "failed", ["empty high or low mesh"])

    co = np.asarray([v.co for v in low.vertices], dtype=float)
    centroids = np.asarray([co[f.vertex_ids].mean(axis=0) for f in low.faces], dtype=float)
    low_face_normals = np.asarray([f.normal for f in low.faces], dtype=float)

    # Distance samples: low vertices + face centroids (deterministically strided).
    dist_pts = np.vstack([co, centroids]) if len(co) else centroids
    distances = [_nearest(dist_pts[i], A, B, C)[0] for i in _stride(len(dist_pts), max_distance_samples)]

    # Normal deviation: per sampled low face, compare to the nearest high tri.
    normal_angles: list[float] = []
    for fi in _stride(low.face_count, max_normal_samples):
        _, idx = _nearest(centroids[fi], A, B, C)
        normal_angles.append(_folded_angle_deg(low_face_normals[fi], tri_normals[idx]))

    vol_high = mesh_volume(A, B, C)
    la, lb, lc, _ = _triangulate_arrays(low)
    vol_low = mesh_volume(la, lb, lc)
    volume_error_ratio = abs(vol_low - vol_high) / vol_high if vol_high > 1e-12 else None

    return build_shape_report(
        bbox_diagonal=diag,
        distances=distances,
        normal_angles_deg=normal_angles,
        volume_error_ratio=volume_error_ratio,
        thresholds=thresholds,
    )


def _folded_angle_deg(n1, n2) -> float:
    a = np.asarray(n1, dtype=float)
    b = np.asarray(n2, dtype=float)
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    cos = float(np.clip(np.dot(a, b) / (na * nb), -1.0, 1.0))
    ang = math.degrees(math.acos(cos))
    return min(ang, 180.0 - ang)  # ignore winding-induced flips
