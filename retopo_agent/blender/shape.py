"""Blender shape-preservation evaluation (retopology plan §6.7, §10 Phase 3).

The pure evaluator (:mod:`retopo_agent.geometry.shape_eval`) brute-forces
nearest-surface distance, which is fine for tests but not for a multi-million-
face high-poly. This adapter computes the same metrics with a ``mathutils``
``BVHTree`` over the high-poly, so each nearest query is O(log n). It produces the
same :class:`~retopo_agent.geometry.shape_eval.ShapeReport` (built by the shared
``build_shape_report``), so the Blender and offline paths classify identically.

Distances are measured in the high-poly's local space (where the BVH lives); low
query points are transformed into that space first. This is exact when the two
objects share a transform (the low-poly is a duplicate of the high-poly) and when
scale is uniform -- the MVP assumption (plan §15.3). Only runs inside Blender.
"""

from __future__ import annotations

import math

from retopo_agent.geometry.shape_eval import (
    DEFAULT_SHAPE_THRESHOLDS,
    ShapeThresholds,
    _folded_angle_deg,
    build_shape_report,
)


def _local_bbox_diagonal(obj) -> float:
    """Bounding-box diagonal in the object's local space (matches the space the
    BVH distances are measured in)."""
    corners = [tuple(c) for c in obj.bound_box]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    zs = [c[2] for c in corners]
    dx, dy, dz = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)
    return max(math.sqrt(dx * dx + dy * dy + dz * dz), 1e-9)


def evaluate_shape_match_blender(
    high_obj,
    low_obj,
    *,
    max_distance_samples: int = 20000,
    max_normal_samples: int = 8000,
    thresholds: ShapeThresholds = DEFAULT_SHAPE_THRESHOLDS,
):
    """Evaluate how closely ``low_obj`` matches ``high_obj`` using a BVH tree.

    ``thresholds`` selects the gating cutoffs (default quad-retopo; pass
    :data:`~retopo_agent.geometry.shape_eval.DECIMATION_SHAPE_THRESHOLDS` for
    Decimation Optimize mode)."""
    import bmesh
    import bpy  # noqa: F401
    from mathutils.bvhtree import BVHTree

    depsgraph = bpy.context.evaluated_depsgraph_get()
    tree = BVHTree.FromObject(high_obj, depsgraph)  # high-poly, local space

    # Map low-poly geometry into the high-poly's local space.
    to_high_local = high_obj.matrix_world.inverted_safe() @ low_obj.matrix_world
    normal_mat = to_high_local.to_3x3().inverted_safe().transposed()
    low_mesh = low_obj.data

    diag = _local_bbox_diagonal(high_obj)

    distances: list[float] = []
    normal_angles: list[float] = []

    def stride(n: int, k: int):
        if n <= k:
            return range(n)
        step = n / k
        return (int(i * step) for i in range(k))

    # Surface distance: low vertices + face centroids -> nearest point on high.
    verts = low_mesh.vertices
    for vi in stride(len(verts), max_distance_samples):
        p = to_high_local @ verts[vi].co
        loc, nrm, idx, dist = tree.find_nearest(p)
        if dist is not None:
            distances.append(float(dist))

    polys = low_mesh.polygons
    for fi in stride(len(polys), max_normal_samples):
        poly = polys[fi]
        c = to_high_local @ poly.center
        loc, nrm, idx, dist = tree.find_nearest(c)
        if dist is None:
            continue
        distances.append(float(dist))
        if nrm is not None:
            low_n = (normal_mat @ poly.normal).normalized()
            normal_angles.append(_folded_angle_deg(tuple(low_n), tuple(nrm)))

    volume_error_ratio = _volume_error_ratio(bmesh, high_obj, low_obj, depsgraph)

    return build_shape_report(
        bbox_diagonal=diag,
        distances=distances,
        normal_angles_deg=normal_angles,
        volume_error_ratio=volume_error_ratio,
        thresholds=thresholds,
    )


def _volume_error_ratio(bmesh, high_obj, low_obj, depsgraph):
    """Relative enclosed-volume change (informational), via ``bmesh.calc_volume``.
    Both volumes are measured in low-poly local units for a like-for-like ratio."""
    try:
        v_high = _object_volume(bmesh, high_obj, depsgraph)
        v_low = _object_volume(bmesh, low_obj, depsgraph)
    except Exception:  # noqa: BLE001 - volume is best-effort / informational
        return None
    if v_high <= 1e-12:
        return None
    return abs(v_low - v_high) / v_high


def _object_volume(bmesh, obj, depsgraph) -> float:
    bm = bmesh.new()
    try:
        bm.from_object(obj, depsgraph)
        bm.transform(obj.matrix_world)
        return abs(bm.calc_volume(signed=True))
    finally:
        bm.free()


def render_shape_preview(low_obj, filepath: str) -> bool:
    """Best-effort silhouette/preview render of the low-poly (plan §10 Phase 3
    "silhouette preview render"). Sets up a temporary camera + sun and renders
    with the fast Workbench engine. Returns True on success. Only runs in Blender.
    """
    import bpy
    from mathutils import Vector

    scene = bpy.context.scene
    cam = light = None
    try:
        # Frame the object from a 3/4 view based on its bounding sphere.
        center = sum((low_obj.matrix_world @ Vector(c) for c in low_obj.bound_box), Vector()) / 8.0
        radius = max(low_obj.dimensions) or 1.0

        cam_data = bpy.data.cameras.new("AI_Retopo_Cam")
        # Clip range must span the scene's scale: the camera sits ~2.6*radius from the
        # subject, and assets imported in world space can be hundreds of units across and
        # far from the origin. The default clip_end (100) clips the whole subject away,
        # producing a blank preview. Scale both planes to the bounding radius.
        cam_data.clip_start = max(0.01, radius * 0.001)
        cam_data.clip_end = radius * 100.0 + 1000.0
        cam = bpy.data.objects.new("AI_Retopo_Cam", cam_data)
        scene.collection.objects.link(cam)
        cam.location = center + Vector((1.0, -1.4, 0.9)).normalized() * radius * 2.6
        _point_at(cam, center)
        scene.camera = cam

        light_data = bpy.data.lights.new("AI_Retopo_Sun", type="SUN")
        light = bpy.data.objects.new("AI_Retopo_Sun", light_data)
        scene.collection.objects.link(light)
        light.location = center + Vector((1.0, -0.5, 1.5)) * radius
        _point_at(light, center)

        scene.render.engine = "BLENDER_WORKBENCH"
        scene.render.resolution_x = 800
        scene.render.resolution_y = 800
        scene.render.filepath = bpy.path.abspath(filepath)
        scene.render.image_settings.file_format = "PNG"
        bpy.ops.render.render(write_still=True)
        return True
    except Exception as exc:  # noqa: BLE001 - preview is best-effort
        print(f"render_shape_preview: skipped ({exc})")
        return False
    finally:
        for tmp in (cam, light):
            if tmp is not None:
                bpy.data.objects.remove(tmp, do_unlink=True)


def _point_at(obj, target) -> None:
    from mathutils import Vector

    direction = (target - obj.location)
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
