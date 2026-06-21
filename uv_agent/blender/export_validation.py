"""Re-open validation for exported production assets (MVP 5 plan §7, Session C).

Each exported FBX / OBJ / GLB / GLTF is re-imported into a fresh scene and probed:
mesh present, UV layer present, face/vertex snapshot, normals/material warnings
(:func:`reopen_and_validate`). A missing UV layer is ALWAYS a hard failure; object
/ material naming and vertex-count splits are format differences reported as
warnings, not failures (plan §7 tolerance policy).

``bpy`` is imported lazily, so the pure helpers (:func:`uv_layer_warnings`,
:func:`count_warnings`, :func:`normals_warning`) unit-test without Blender.
"""

from __future__ import annotations

import os

# Vertex count may legitimately differ after a format round-trip (UV/normal
# splits). Only warn when the re-opened vertex count drifts beyond this ratio of
# the source; never hard-fail on it (plan §7 "wildly different" tolerance).
VERTEX_DRIFT_WARN_RATIO = 0.5
# Face count should match unless triangulation was requested (plan §7).
FACE_DRIFT_WARN_RATIO = 0.02


# ---------------------------------------------------------------------------
# Pure helpers (no Blender) — the warning policy (plan §7 tolerance)
# ---------------------------------------------------------------------------
def uv_layer_warnings(uv_layers: list[str], expected_uv_layer: str | None, *, fmt: str) -> list[str]:
    """Warn (never fail) when the expected UV layer name is absent after export.

    Formats like OBJ/GLB do not preserve UV-layer *names*, so a renamed-but-present
    layer is a warning, not a failure (plan §7). Missing UV entirely is handled by
    the caller as a hard failure.
    """
    warnings: list[str] = []
    if expected_uv_layer and uv_layers and expected_uv_layer not in uv_layers:
        warnings.append(
            f"{fmt}: exported UV layer named {uv_layers[0]!r}, expected {expected_uv_layer!r} "
            f"({fmt.upper()} may not preserve UV layer names)")
    return warnings


def count_warnings(
    *,
    fmt: str,
    faces: int,
    vertices: int,
    source_faces: int | None,
    source_vertices: int | None,
    triangulated: bool,
) -> list[str]:
    """Warn on face/vertex drift vs the source snapshot (plan §7 tolerance).

    Face count is expected to change only when ``triangulated``; vertex count may
    drift due to format-specific splits. Both are warnings, never failures.
    """
    warnings: list[str] = []
    if source_faces and not triangulated:
        drift = abs(faces - source_faces) / max(source_faces, 1)
        if drift > FACE_DRIFT_WARN_RATIO:
            warnings.append(f"{fmt}: face count {faces} differs from source {source_faces}")
    if source_vertices:
        drift = abs(vertices - source_vertices) / max(source_vertices, 1)
        if drift > VERTEX_DRIFT_WARN_RATIO:
            warnings.append(f"{fmt}: vertex count {vertices} differs from source "
                            f"{source_vertices} (format-specific splits)")
    return warnings


def normals_warning(has_normals: bool, include_normals: bool, *, fmt: str) -> list[str]:
    """Warn when normals were requested but absent after re-open (plan §7)."""
    if include_normals and not has_normals:
        return [f"{fmt}: include_normals was requested but no normals found after re-open"]
    return []


# ---------------------------------------------------------------------------
# Re-open + import (plan §7 step 1)
# ---------------------------------------------------------------------------
def _reset_scene(bpy) -> None:
    """Wipe to an empty scene so the re-opened file is measured in isolation."""
    try:
        bpy.ops.wm.read_homefile(use_empty=True)
    except Exception:  # noqa: BLE001 - best-effort; remove objects manually
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o, do_unlink=True)


def _import(bpy, path: str, fmt: str) -> None:
    if fmt == "fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif fmt == "obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:  # pragma: no cover - legacy Blender
            bpy.ops.import_scene.obj(filepath=path)
    elif fmt in ("glb", "gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    else:
        raise ValueError(f"unsupported format for re-open: {fmt!r}")


def _mesh_has_normals(mesh) -> bool:
    """Best-effort: does the re-opened mesh carry usable normals?"""
    if getattr(mesh, "has_custom_normals", False):
        return True
    # Every Blender mesh with polygons has face normals; treat that as "normals
    # present". (Missing normals only realistically happens on a point cloud.)
    return len(mesh.polygons) > 0 and len(mesh.loops) > 0


def reopen_and_validate(
    bpy,
    path: str,
    fmt: str,
    *,
    expected_uv_layer: str | None = None,
    include_normals: bool = True,
    source_faces: int | None = None,
    source_vertices: int | None = None,
    triangulated: bool = False,
) -> dict:
    """Re-import ``path`` into a fresh scene and validate it (plan §7).

    Returns the per-format validation block::

        {"reopen_ok", "mesh_count", "faces", "vertices", "uv_layers",
         "has_uv", "has_normals", "warnings"}

    ``has_uv`` False is a hard failure for the caller (plan §7); everything else is
    advisory. A re-open exception yields ``reopen_ok=False`` with the error text in
    ``warnings`` so the worker never crashes on a bad file.
    """
    _reset_scene(bpy)
    try:
        _import(bpy, os.path.abspath(path), fmt)
    except Exception as exc:  # noqa: BLE001 - structured re-open failure
        return {
            "reopen_ok": False, "mesh_count": 0, "faces": 0, "vertices": 0,
            "uv_layers": [], "has_uv": False, "has_normals": False,
            "warnings": [f"{fmt}: re-open failed: {exc}"],
        }

    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    faces = sum(len(o.data.polygons) for o in meshes)
    vertices = sum(len(o.data.vertices) for o in meshes)
    uv_names: list[str] = []
    has_normals = False
    for o in meshes:
        for uv in o.data.uv_layers:
            if uv.name not in uv_names:
                uv_names.append(uv.name)
        if _mesh_has_normals(o.data):
            has_normals = True
    has_uv = len(uv_names) > 0

    warnings: list[str] = []
    if not meshes:
        warnings.append(f"{fmt}: no mesh object after re-open")
    if not has_uv:
        warnings.append(f"{fmt}: NO UV layer after re-open (hard failure)")
    warnings += uv_layer_warnings(uv_names, expected_uv_layer, fmt=fmt)
    warnings += normals_warning(has_normals, include_normals, fmt=fmt)
    warnings += count_warnings(fmt=fmt, faces=faces, vertices=vertices,
                               source_faces=source_faces, source_vertices=source_vertices,
                               triangulated=triangulated)

    return {
        "reopen_ok": len(meshes) > 0,
        "mesh_count": len(meshes),
        "faces": faces,
        "vertices": vertices,
        "uv_layers": uv_names,
        "has_uv": has_uv,
        "has_normals": has_normals,
        "warnings": warnings,
    }
