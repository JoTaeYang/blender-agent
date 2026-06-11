"""Blender LOD batch generation + comparison (retopology plan §10 Phase 4).

Generates several low-poly versions from one high-poly object -- one per topology
level / target -- and evaluates each (topology validation + BVH shape match
against the shared high-poly), collecting them into a
:class:`~retopo_agent.levels.LodComparison`. All LOD objects are left in the scene
(distinct ``{name}_LOW_{target}`` names) so they can be saved/compared together.

This is the production equivalent of :func:`retopo_agent.pipeline.generate_lod_set_offline`.
Only runs inside Blender.
"""

from __future__ import annotations

from dataclasses import dataclass

from retopo_agent.levels import LodComparison, LodEntry, LodPlan


@dataclass
class LodObjectResult:
    plan: LodPlan
    entry: LodEntry
    obj: object  # the generated bpy object


def generate_and_evaluate_lods(
    high_obj,
    plans: list[LodPlan],
    *,
    quad_required: bool = True,
    ngon_allowed: bool = False,
    apply_shrinkwrap: bool = True,
    preserve_sharp: bool = True,
    expect_closed: bool = True,
    preserve_features: bool = False,
    feature_angle: float = 30.0,
    voxel_adaptivity: float = 0.0,
) -> tuple[LodComparison, list[LodObjectResult]]:
    """Generate + evaluate one LOD per plan; return the comparison and the
    per-LOD objects/results (objects kept in the scene)."""
    from retopo_agent.blender.retopo import generate_lowpoly_object
    from retopo_agent.blender.shape import evaluate_shape_match_blender
    from retopo_agent.geometry.validate import validate_topology
    from uv_agent.blender.extract import extract_mesh_graph

    source_faces = len(high_obj.data.polygons)
    results: list[LodObjectResult] = []
    entries: list[LodEntry] = []

    for plan in plans:
        gen = generate_lowpoly_object(
            high_obj,
            plan.target_face_count,
            apply_shrinkwrap=apply_shrinkwrap,
            preserve_sharp=preserve_sharp,
            preserve_features=preserve_features,
            feature_angle=feature_angle,
            voxel_adaptivity=voxel_adaptivity,
        )
        graph = extract_mesh_graph(gen.obj)
        validation = validate_topology(
            graph,
            plan.target_face_count,
            quad_required=quad_required,
            ngon_allowed=ngon_allowed,
            expect_closed=expect_closed,
        )
        shape = evaluate_shape_match_blender(high_obj, gen.obj)
        entry = LodEntry.from_reports(
            plan.level,
            plan.target_face_count,
            actual_face_count=gen.actual_face_count,
            generation_method=gen.method,
            generation_band=gen.band,
            validation=validation,
            shape=shape,
        )
        entries.append(entry)
        results.append(LodObjectResult(plan, entry, gen.obj))

    return LodComparison(source_face_count=source_faces, entries=entries), results
