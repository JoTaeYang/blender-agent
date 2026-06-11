"""Blender normal / visual cleanup for Decimation Optimize mode (Phase D4, §6.4).

After decimation a triangle LOD shades flat and facets visibly. This adapter does
the §6.4 cleanup inside Blender, mirroring the offline model in
:mod:`retopo_agent.geometry.normals`:

1. optional **Triangulate** (the Collapse output is already triangulated, but an
   explicit pass guarantees no n-gons survive a later edit);
2. **Auto Smooth** -- set every face to smooth shading and mark edges sharp where
   the dihedral angle exceeds ``auto_smooth_angle`` (reusing Phase 5's
   :func:`retopo_agent.blender.features.mark_sharp_edges_by_angle`), so shading
   splits only at creases (the "smoothing split control");
3. **Weighted Normal** modifier (``keep_sharp``) -- area-weighted custom split
   normals, which is what makes the smoothed shading track the surface;
4. optional **Normal transfer** -- a Data Transfer modifier copying the high-poly's
   custom split normals onto the low-poly, the most faithful option of all.

Everything is best-effort and version-tolerant (``bpy`` differs across releases on
auto-smooth handling): each step is guarded and appends a note rather than
aborting. Only runs inside Blender.
"""

from __future__ import annotations


def cleanup_decimated_normals(
    low_obj,
    high_obj=None,
    *,
    auto_smooth_angle: float = 30.0,
    weighted_normal: bool = True,
    transfer_normals: bool = False,
    triangulate: bool = False,
) -> dict:
    """Apply the Phase D4 normal cleanup to ``low_obj`` (plan §6.4). Returns a dict
    of what was applied. ``high_obj`` is required only for ``transfer_normals``."""
    import bpy  # noqa: F401

    from retopo_agent.blender.features import mark_sharp_edges_by_angle

    notes: list[str] = []
    applied: list[str] = []
    _make_only_active(low_obj)

    if triangulate:
        if _apply_modifier(low_obj, "AI_Triangulate", "TRIANGULATE", notes):
            applied.append("triangulate")

    # Auto Smooth: smooth faces, then split shading at creases via sharp edges.
    try:
        for poly in low_obj.data.polygons:
            poly.use_smooth = True
        marked = mark_sharp_edges_by_angle(low_obj, auto_smooth_angle)
        applied.append("auto_smooth")
        notes.append(f"auto_smooth: faces smooth, {marked} sharp edges (>= {auto_smooth_angle} deg)")
    except (RuntimeError, AttributeError) as exc:
        notes.append(f"auto_smooth failed: {exc}")

    if weighted_normal:
        def _cfg(mod):
            mod.keep_sharp = True
            mod.weight = 50
        if _apply_modifier(low_obj, "AI_WeightedNormal", "WEIGHTED_NORMAL", notes, configure=_cfg):
            applied.append("weighted_normal")

    if transfer_normals and high_obj is not None:
        def _cfg(mod):
            mod.object = high_obj
            mod.use_loop_data = True
            mod.data_types_loops = {"CUSTOM_NORMAL"}
            mod.loop_mapping = "POLYINTERP_NEAREST"
        if _apply_modifier(low_obj, "AI_NormalTransfer", "DATA_TRANSFER", notes, configure=_cfg):
            applied.append("normal_transfer")
    elif transfer_normals:
        notes.append("normal_transfer skipped: no high-poly source given")

    return {
        "auto_smooth_angle_deg": auto_smooth_angle,
        "applied": applied,
        "notes": notes,
    }


def _apply_modifier(obj, name: str, mod_type: str, notes: list[str], *, configure=None) -> bool:
    """Add ``mod_type`` to ``obj``, configure it, and apply it. Best-effort: logs a
    note and returns False on failure. Returns True when applied."""
    import bpy

    try:
        mod = obj.modifiers.new(name=name, type=mod_type)
        if configure is not None:
            configure(mod)
        _make_only_active(obj)
        bpy.ops.object.modifier_apply(modifier=mod.name)
        return True
    except (RuntimeError, AttributeError, TypeError) as exc:
        notes.append(f"{mod_type.lower()} failed: {exc}")
        return False


def _make_only_active(obj) -> None:
    import bpy

    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
