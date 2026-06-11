"""Feature-preservation off/on comparison for Decimation Optimize mode (Phase D3).

Phase D3's deliverable is the ability to *compare* a decimation that ignores
features against one that protects them, **at the same target face count**
(decimation plan §7 "compare feature preservation off/on results"). This module
is the offline, Blender-free core of that comparison, built entirely from pieces
that already exist:

- the "off" variant is plain :func:`~retopo_agent.geometry.decimate.decimate_to_target`;
- the "on" variant is :func:`~retopo_agent.geometry.decimate.feature_aware_decimate_to_target`,
  which keeps the feature vertices found by
  :func:`~retopo_agent.geometry.features.feature_vertex_mask` while collapsing flat
  regions more aggressively (plan §6 "preserve density in important areas, decimate
  flat areas"); and
- both variants are scored with the Phase 3 shape evaluator under the decimation
  bands (:data:`~retopo_agent.geometry.shape_eval.DECIMATION_SHAPE_THRESHOLDS`).

Preserving the hard edges / silhouette keeps the worst-case surface deviation
down, so the headline number is ``surface_distance_max_ratio_improvement`` (off
minus on); a positive value means feature preservation helped. The Blender worker
produces the same comparison on real assets via the BVH evaluator.
"""

from __future__ import annotations

from dataclasses import dataclass

from retopo_agent.geometry.decimate import (
    decimate_to_target,
    feature_aware_decimate_to_target,
)
from retopo_agent.geometry.features import DEFAULT_FEATURE_ANGLE, feature_vertex_mask
from retopo_agent.geometry.shape_eval import (
    DECIMATION_SHAPE_THRESHOLDS,
    ShapeReport,
    ShapeThresholds,
    evaluate_shape_match,
)
from uv_agent.geometry.mesh_graph import MeshGraph

FEATURE_OFF = "feature_preserve_off"
FEATURE_ON = "feature_preserve_on"


@dataclass
class DecimationVariant:
    """One side of the comparison: a decimated mesh and its shape report."""

    label: str  # FEATURE_OFF | FEATURE_ON
    method: str
    target_face_count: int
    actual_face_count: int
    shape: ShapeReport

    @property
    def target_error_ratio(self) -> float:
        if self.target_face_count <= 0:
            return 0.0
        return abs(self.actual_face_count - self.target_face_count) / self.target_face_count

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "method": self.method,
            "target_face_count": self.target_face_count,
            "actual_face_count": self.actual_face_count,
            "target_error_ratio": round(self.target_error_ratio, 4),
            "shape_status": self.shape.status,
            "surface_distance_mean_ratio": round(self.shape.surface_distance_mean_ratio, 5),
            "surface_distance_max_ratio": round(self.shape.surface_distance_max_ratio, 5),
            "normal_deviation_mean_deg": round(self.shape.normal_deviation_mean_deg, 3),
        }


@dataclass
class FeaturePreservationComparison:
    target_face_count: int
    feature_angle: float
    feature_vertex_count: int
    off: DecimationVariant
    on: DecimationVariant

    @property
    def surface_distance_max_ratio_improvement(self) -> float:
        """How much feature preservation cut the worst-case surface deviation
        (off minus on); positive means preservation helped."""
        return self.off.shape.surface_distance_max_ratio - self.on.shape.surface_distance_max_ratio

    @property
    def surface_distance_mean_ratio_improvement(self) -> float:
        return self.off.shape.surface_distance_mean_ratio - self.on.shape.surface_distance_mean_ratio

    @property
    def preserves_shape_better(self) -> bool:
        return self.surface_distance_max_ratio_improvement >= 0.0

    def to_dict(self) -> dict:
        return {
            "comparison": "feature_preservation",
            "target_face_count": self.target_face_count,
            "feature_angle_deg": self.feature_angle,
            "feature_vertex_count": self.feature_vertex_count,
            "off": self.off.to_dict(),
            "on": self.on.to_dict(),
            "surface_distance_max_ratio_improvement": round(self.surface_distance_max_ratio_improvement, 5),
            "surface_distance_mean_ratio_improvement": round(self.surface_distance_mean_ratio_improvement, 5),
            "preserves_shape_better": self.preserves_shape_better,
        }


def compare_feature_preservation(
    high: MeshGraph,
    target_face_count: int,
    *,
    feature_angle: float = DEFAULT_FEATURE_ANGLE,
    thresholds: ShapeThresholds = DECIMATION_SHAPE_THRESHOLDS,
) -> FeaturePreservationComparison:
    """Decimate ``high`` to ``target_face_count`` twice -- without and with feature
    preservation -- and score both for shape fidelity (plan §7 Phase D3).

    The feature mask comes from the dihedral-angle scores
    (:func:`feature_vertex_mask`): vertices on hard edges / boundaries / high
    curvature are kept exactly in the "on" variant. Returns both variants and the
    improvement in worst-case surface deviation."""
    mask = feature_vertex_mask(high, angle_threshold=feature_angle)

    off = decimate_to_target(high, target_face_count)
    on = feature_aware_decimate_to_target(high, target_face_count, mask)

    off_shape = evaluate_shape_match(high, off.low_mesh, thresholds=thresholds)
    on_shape = evaluate_shape_match(high, on.low_mesh, thresholds=thresholds)

    return FeaturePreservationComparison(
        target_face_count=target_face_count,
        feature_angle=feature_angle,
        feature_vertex_count=int(mask.sum()),
        off=DecimationVariant(FEATURE_OFF, off.method, target_face_count, off.actual_face_count, off_shape),
        on=DecimationVariant(FEATURE_ON, on.method, target_face_count, on.actual_face_count, on_shape),
    )
