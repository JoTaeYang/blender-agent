"""Blender adapter for the DM5 progressive decimation retry ladder (plan §8).

When the primary Collapse pass plateaus, this runs the DM5 ladder on the *plateau
result* (small enough for a Python mesh graph -- e.g. the anchor's 8008 faces),
escalating through feature-protected collapse, cleanup, planar reduction and DM3
component-budget removal. The orchestration and the per-attempt strategies are the
pure :mod:`retopo_agent.geometry.retry_ladder`; this adapter only extracts the
graph, runs the ladder, and materializes the selected candidate as a new Blender
object.

Shape is scored against the high-poly ``reference_obj`` when it is small enough to
extract, else against the plateau base itself ("how much did this attempt degrade
the already-accepted plateau result?"). ``max_graph_faces`` guards both. The DM6
custom-QEM rung is skipped until implemented. Only runs inside Blender.
"""

from __future__ import annotations


def run_decimation_retry_ladder_blender(
    base_obj,
    target_face_count: int,
    *,
    reference_obj=None,
    feature_angle: float = 30.0,
    relaxed_angle: float = 60.0,
    allow_component_removal: bool = False,
    shape_thresholds=None,
    max_graph_faces: int = 2_000_000,
    max_attempts: int | None = None,
):
    """Run the DM5 ladder on ``base_obj``.

    Returns ``(ladder_result, result_obj)``: the
    :class:`~retopo_agent.geometry.retry_ladder.LadderResult` (or ``None`` if the
    base mesh is too large to diagnose) and a new Blender object holding the
    selected candidate (or ``None`` if no attempt improved on the base)."""
    from retopo_agent.geometry.retry_ladder import LADDER, make_attempt_executor, run_retry_ladder
    from retopo_agent.geometry.shape_eval import DECIMATION_SHAPE_THRESHOLDS
    from uv_agent.blender.extract import extract_mesh_graph

    if len(base_obj.data.polygons) > max_graph_faces:
        return None, None

    base_mesh = extract_mesh_graph(base_obj)
    reference_mesh = None
    if reference_obj is not None and len(reference_obj.data.polygons) <= max_graph_faces:
        reference_mesh = extract_mesh_graph(reference_obj)

    execute = make_attempt_executor(
        base_mesh,
        target_face_count,
        reference_mesh=reference_mesh,
        shape_thresholds=shape_thresholds or DECIMATION_SHAPE_THRESHOLDS,
        feature_angle=feature_angle,
        relaxed_angle=relaxed_angle,
        allow_component_removal=allow_component_removal,
    )
    rungs = LADDER if max_attempts is None else LADDER[: max(0, int(max_attempts))]
    ladder = run_retry_ladder(execute, target_face_count=target_face_count, ladder=rungs)

    selected = ladder.selected
    result_obj = None
    if selected is not None and selected.mesh is not None and selected.mesh.face_count != base_mesh.face_count:
        result_obj = _object_from_graph(base_obj, selected.mesh, f"{base_obj.name}_RETRY{selected.attempt}")
    return ladder, result_obj


def _object_from_graph(template_obj, graph, name: str):
    """Create a new Blender object (linked to the results collection) whose mesh is
    built from ``graph``, copying ``template_obj``'s transform/material setup."""
    import bpy  # noqa: F401

    from retopo_agent.blender.retopo import _link_to_results_collection, _replace_mesh_from_graph

    new_obj = template_obj.copy()
    new_obj.data = template_obj.data.copy()
    new_obj.name = name
    new_obj.data.name = name
    _replace_mesh_from_graph(new_obj, graph)
    _link_to_results_collection(new_obj)
    return new_obj
