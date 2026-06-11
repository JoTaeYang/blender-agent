"""Topology level presets + LOD comparison types (retopology plan §6.2, §10 Phase 4).

Phase 4 lets a user pick a topology *level* instead of a raw face count, batch
several levels from one high-poly, and compare them.

Presets map to a fraction of the source face count (plan §6.2 table: a 100k input
yields 50k / 20k / 10k):

    high_retopo -> 0.5   (prioritise shape preservation)
    mid_retopo  -> 0.2   (general game / render asset)
    low_retopo  -> 0.1   (real-time / mobile / LOD)
    custom      -> user-supplied absolute target

This module is pure (no Blender, no numpy): preset resolution, the per-LOD result
record, and the comparison container. Both the offline pipeline
(:mod:`retopo_agent.pipeline`) and the Blender batch
(:mod:`retopo_agent.blender.batch`) build the *same* ``LodEntry`` / ``LodComparison``
so their comparison JSON is identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Preset -> fraction of source face count (plan §6.2).
TOPOLOGY_PRESETS: dict[str, float] = {
    "high_retopo": 0.5,
    "mid_retopo": 0.2,
    "low_retopo": 0.1,
}
CUSTOM_LEVEL = "custom"
MIN_TARGET_FACES = 50


def resolve_target_face_count(source_face_count: int, level: str, *, custom_target: int | None = None) -> int:
    """Resolve a topology ``level`` to an absolute target face count.

    Preset levels scale the source count; ``custom`` uses ``custom_target``.
    """
    if level == CUSTOM_LEVEL:
        if custom_target is None:
            raise ValueError("custom topology level requires a custom_target")
        return max(MIN_TARGET_FACES, int(custom_target))
    if level not in TOPOLOGY_PRESETS:
        raise ValueError(f"unknown topology level: {level!r} (known: {sorted(TOPOLOGY_PRESETS)} + custom)")
    return max(MIN_TARGET_FACES, round(source_face_count * TOPOLOGY_PRESETS[level]))


@dataclass(frozen=True)
class LodPlan:
    level: str
    target_face_count: int

    def object_suffix(self) -> str:
        return f"LOW_{self.target_face_count}"


def plan_topology_levels(
    source_face_count: int,
    *,
    levels: list[str] | None = None,
    targets: list[int] | None = None,
) -> list[LodPlan]:
    """Build a de-duplicated, descending-by-detail list of LOD plans.

    ``levels`` are preset names resolved against the source; ``targets`` are
    absolute custom face counts. With neither, defaults to a single
    ``low_retopo`` plan.
    """
    plans: list[LodPlan] = []
    for level in levels or []:
        plans.append(LodPlan(level, resolve_target_face_count(source_face_count, level)))
    for target in targets or []:
        plans.append(LodPlan(CUSTOM_LEVEL, max(MIN_TARGET_FACES, int(target))))
    if not plans:
        plans.append(LodPlan("low_retopo", resolve_target_face_count(source_face_count, "low_retopo")))

    # De-duplicate by target face count (keep first/most-specific level label),
    # then sort high-detail first.
    seen: dict[int, LodPlan] = {}
    for plan in plans:
        seen.setdefault(plan.target_face_count, plan)
    return sorted(seen.values(), key=lambda p: p.target_face_count, reverse=True)


@dataclass
class LodEntry:
    """One LOD's headline metrics, gathered from the generation / validation /
    shape reports for side-by-side comparison."""

    level: str
    target_face_count: int
    actual_face_count: int
    generation_method: str
    generation_band: str
    validation_status: str
    quad_ratio: float
    triangle_count: int
    ngon_count: int
    non_manifold_edge_count: int
    shape_status: str
    surface_distance_mean_ratio: float
    surface_distance_max_ratio: float
    normal_deviation_mean_deg: float

    @property
    def target_error_ratio(self) -> float:
        if self.target_face_count <= 0:
            return 0.0
        return abs(self.actual_face_count - self.target_face_count) / self.target_face_count

    @classmethod
    def from_reports(
        cls,
        level: str,
        target_face_count: int,
        *,
        actual_face_count: int,
        generation_method: str,
        generation_band: str,
        validation,
        shape,
    ) -> "LodEntry":
        return cls(
            level=level,
            target_face_count=target_face_count,
            actual_face_count=actual_face_count,
            generation_method=generation_method,
            generation_band=generation_band,
            validation_status=validation.status,
            quad_ratio=round(validation.quad_ratio, 4),
            triangle_count=validation.triangle_count,
            ngon_count=validation.ngon_count,
            non_manifold_edge_count=validation.non_manifold_edge_count,
            shape_status=shape.status,
            surface_distance_mean_ratio=round(shape.surface_distance_mean_ratio, 5),
            surface_distance_max_ratio=round(shape.surface_distance_max_ratio, 5),
            normal_deviation_mean_deg=round(shape.normal_deviation_mean_deg, 3),
        )

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "target_face_count": self.target_face_count,
            "actual_face_count": self.actual_face_count,
            "target_error_ratio": round(self.target_error_ratio, 4),
            "generation_method": self.generation_method,
            "generation_band": self.generation_band,
            "validation_status": self.validation_status,
            "quad_ratio": self.quad_ratio,
            "triangle_count": self.triangle_count,
            "ngon_count": self.ngon_count,
            "non_manifold_edge_count": self.non_manifold_edge_count,
            "shape_status": self.shape_status,
            "surface_distance_mean_ratio": self.surface_distance_mean_ratio,
            "surface_distance_max_ratio": self.surface_distance_max_ratio,
            "normal_deviation_mean_deg": self.normal_deviation_mean_deg,
        }


@dataclass
class LodComparison:
    source_face_count: int
    entries: list[LodEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_face_count": self.source_face_count,
            "lod_count": len(self.entries),
            "levels": [e.level for e in self.entries],
            "lods": [e.to_dict() for e in self.entries],
        }
