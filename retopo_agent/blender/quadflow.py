"""Blender quad-flow improvement (retopology plan §6.8, §10 Phase 6).

The production counterpart of :func:`retopo_agent.geometry.quadflow.improve_quad_flow`:
converts triangles to quads and relaxes edge flow with native operators, then
(optionally) re-projects onto the high-poly so the relax doesn't drift off the
surface. Scoring is done by the pure :func:`quad_flow_score` on the extracted
graph, so Blender and offline report the same number. Only runs inside Blender.
"""

from __future__ import annotations

import math


def improve_quad_flow_blender(
    obj,
    *,
    smooth_iterations: int = 5,
    smooth_factor: float = 0.5,
    face_angle_deg: float = 40.0,
    shape_angle_deg: float = 40.0,
    shrinkwrap_target=None,
) -> list[str]:
    """Tris->quads + relax on ``obj`` in place (plan §6.8).

    If ``shrinkwrap_target`` is given, a Shrinkwrap is applied afterwards so the
    relaxed vertices snap back onto the high-poly surface. Returns op notes.
    """
    import bpy

    notes: list[str] = []
    _make_only_active(obj)

    before_faces = len(obj.data.polygons)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bpy.ops.mesh.select_all(action="SELECT")
        try:
            bpy.ops.mesh.tris_convert_to_quads(
                face_threshold=math.radians(face_angle_deg),
                shape_threshold=math.radians(shape_angle_deg),
            )
        except RuntimeError as exc:
            notes.append(f"tris_convert_to_quads failed: {exc}")
        if smooth_iterations > 0:
            try:
                bpy.ops.mesh.vertices_smooth(factor=float(smooth_factor), repeat=int(smooth_iterations))
            except RuntimeError as exc:
                notes.append(f"vertices_smooth failed: {exc}")
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    notes.append(f"tris->quads + relax: {before_faces} -> {len(obj.data.polygons)} faces")

    if shrinkwrap_target is not None:
        try:
            mod = obj.modifiers.new(name="AI_QF_Shrinkwrap", type="SHRINKWRAP")
            mod.target = shrinkwrap_target
            mod.wrap_method = "NEAREST_SURFACEPOINT"
            _make_only_active(obj)
            bpy.ops.object.modifier_apply(modifier=mod.name)
            notes.append("re-projected onto high-poly (shrinkwrap)")
        except (RuntimeError, AttributeError) as exc:
            notes.append(f"re-projection shrinkwrap failed: {exc}")

    return notes


def _make_only_active(obj) -> None:
    import bpy

    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
