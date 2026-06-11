"""Offline LOD batch pipeline (retopology plan §10 Phase 4, §15.12).

Ties the Phase 1-3 stages together without Blender: for each requested topology
level it runs cluster-decimation -> topology validation -> shape evaluation, and
collects the results into a :class:`~retopo_agent.levels.LodComparison`. This is
the deterministic ``--provider mock`` path (plan §15.12) and what the Phase 4
tests exercise; the Blender batch (:mod:`retopo_agent.blender.batch`) is the
production equivalent using QuadriFlow/voxel + a BVH shape check.

Note: offline generation uses cluster-decimation, which is not a quad mesher, so
``quad_required`` defaults to ``False`` here -- the offline comparison is about
face-count control and shape preservation across levels, which is the Phase 4
completion criterion. The Blender path produces quad-dominant LODs.
"""

from __future__ import annotations

from retopo_agent.geometry.decimate import decimate_to_target
from retopo_agent.geometry.shape_eval import evaluate_shape_match
from retopo_agent.geometry.target_search import quality_band
from retopo_agent.geometry.validate import validate_topology
from retopo_agent.levels import LodComparison, LodEntry, plan_topology_levels
from uv_agent.geometry.mesh_graph import MeshGraph


def evaluate_lod_offline(
    high: MeshGraph,
    target_face_count: int,
    *,
    level: str = "custom",
    quad_required: bool = False,
    ngon_allowed: bool = False,
    expect_closed: bool = False,
) -> LodEntry:
    """Generate and evaluate a single LOD from ``high`` (offline)."""
    gen = decimate_to_target(high, target_face_count)
    low = gen.low_mesh
    validation = validate_topology(
        low,
        target_face_count,
        quad_required=quad_required,
        ngon_allowed=ngon_allowed,
        expect_closed=expect_closed,
    )
    shape = evaluate_shape_match(high, low)
    return LodEntry.from_reports(
        level,
        target_face_count,
        actual_face_count=low.face_count,
        generation_method=gen.method,
        generation_band=quality_band(low.face_count, target_face_count),
        validation=validation,
        shape=shape,
    )


def generate_lod_set_offline(
    high: MeshGraph,
    *,
    levels: list[str] | None = None,
    targets: list[int] | None = None,
    quad_required: bool = False,
    ngon_allowed: bool = False,
    expect_closed: bool = False,
) -> LodComparison:
    """Batch-generate and compare multiple LODs from one high-poly (plan §10 Phase 4).

    Completion criterion: pass ``targets=[50000, 20000, 10000]`` (or
    ``levels=["high_retopo", "mid_retopo", "low_retopo"]``) to get the three
    versions compared in a single :class:`LodComparison`.
    """
    plans = plan_topology_levels(high.face_count, levels=levels, targets=targets)
    entries = [
        evaluate_lod_offline(
            high,
            plan.target_face_count,
            level=plan.level,
            quad_required=quad_required,
            ngon_allowed=ngon_allowed,
            expect_closed=expect_closed,
        )
        for plan in plans
    ]
    return LodComparison(source_face_count=high.face_count, entries=entries)
