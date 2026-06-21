"""UV review checker render (MVP 1 plan §7, Session C).

Produces ``checker_front.png`` / ``checker_side.png`` (+ optional
``checker_3q.png``) by applying an emission checker through the object's active UV
and rendering from a single stable orthographic camera
(:func:`render_checker_views`). Ported and trimmed from
``worker/run_quad_retopo_job.py`` (plan §7 "checker 로직을 MVP 1 worker로
복사/정리"). Only runs inside Blender (``bpy`` imported lazily).

The UV **layout** PNG is NOT produced here: Blender's ``uv.export_layout`` PNG
mode requires GPU drawing that is unavailable under ``blender --background``, so
the layout is rasterized headlessly by :func:`uv_agent.geometry.uv_review`
(plan §7, §13). The EEVEE checker render, by contrast, works fine in background.

Safety (plan §7, §13): the checker material is applied to the object **in the
worker's ephemeral Blender process only**; nothing here ever saves the model, so
the original working model on disk is never modified. Each render is best-effort —
a failing view is skipped (a warning), not fatal.
"""

from __future__ import annotations

import os

_CHECKER_MAT = "MVP1_UV_Review_Checker"
# Camera view directions (world space). 3q = three-quarter hero angle (optional).
_VIEWS = {
    "front": (0.0, -1.0, 0.0),
    "side": (1.0, 0.0, 0.0),
    "3q": (1.0, -1.0, 0.6),
}


def _activate_only(bpy, obj) -> None:
    """Make ``obj`` the sole selected + active object in OBJECT mode."""
    try:
        if getattr(bpy.context, "object", None) is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
    except RuntimeError:
        pass
    for o in bpy.context.view_layer.objects:
        if o is not None:
            o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def apply_checker_material(obj, *, scale: float = 40.0):
    """Attach an emission checker mapped through the object's ACTIVE UV layer.

    Emission needs no lights, so the render shows UV stretch/correspondence
    directly. Applied to the object in the ephemeral worker scene; never saved.
    """
    import bpy

    mat = bpy.data.materials.new(_CHECKER_MAT)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    emis = nt.nodes.new("ShaderNodeEmission")
    chk = nt.nodes.new("ShaderNodeTexChecker")
    chk.inputs["Scale"].default_value = float(scale)
    uvn = nt.nodes.new("ShaderNodeUVMap")
    active = obj.data.uv_layers.active
    if active is not None:
        uvn.uv_map = active.name
    nt.links.new(uvn.outputs["UV"], chk.inputs["Vector"])
    nt.links.new(chk.outputs["Color"], emis.inputs["Color"])
    nt.links.new(emis.outputs["Emission"], out.inputs["Surface"])
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return mat


def render_checker_views(
    obj,
    out_dir: str,
    *,
    scale: float = 40.0,
    size: int = 900,
    make_3q: bool = False,
    filenames: dict | None = None,
) -> dict:
    """Render checker previews of ``obj`` from a single stable ortho camera.

    Front + side always; an optional three-quarter view when ``make_3q``. The
    camera is framed once on the object's world bounds and only its position
    changes per view, so framing is consistent across views (plan §7 "front/side
    camera framing은 같은 object bounds를 기준으로 안정적으로 잡는다").

    Requires an active UV layer; returns ``{}`` and renders nothing otherwise
    (plan Session C acceptance). Returns ``{view: path}`` for every render written.
    Best-effort per view — a failing render is skipped, not fatal.
    """
    import bpy
    import mathutils

    if obj.data.uv_layers.active is None:
        return {}

    names = {"front": "checker_front.png", "side": "checker_side.png", "3q": "checker_3q.png"}
    if filenames:
        names.update(filenames)

    apply_checker_material(obj, scale=scale)

    scene = bpy.context.scene
    for eng in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        try:
            scene.render.engine = eng
            break
        except (TypeError, ValueError):  # pragma: no cover - Blender build dependent
            continue
    scene.render.resolution_x = scene.render.resolution_y = int(size)
    scene.render.film_transparent = True

    corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
    centre = sum(corners, mathutils.Vector()) / 8.0
    radius = max((c - centre).length for c in corners) or 1.0

    cam_data = bpy.data.cameras.new("MVP1_Review_Cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = radius * 2.2
    cam = bpy.data.objects.new("MVP1_Review_Cam", cam_data)
    bpy.context.collection.objects.link(cam)
    scene.camera = cam

    views = ["front", "side"] + (["3q"] if make_3q else [])
    out: dict[str, str] = {}
    try:
        for vname in views:
            d = mathutils.Vector(_VIEWS[vname]).normalized()
            cam.location = centre + d * radius * 3
            cam.rotation_euler = (centre - cam.location).normalized().to_track_quat("-Z", "Z").to_euler()
            path = os.path.join(out_dir, names[vname])
            scene.render.filepath = os.path.abspath(path)
            try:
                bpy.ops.render.render(write_still=True)
            except RuntimeError as exc:  # pragma: no cover - Blender op
                print(f"review_render: checker {vname} render skipped ({exc})", flush=True)
                continue
            if os.path.exists(path):
                out[vname] = path
    finally:
        bpy.data.objects.remove(cam, do_unlink=True)
        bpy.data.cameras.remove(cam_data, do_unlink=True)
    return out
