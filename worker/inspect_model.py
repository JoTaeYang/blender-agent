"""Headless Blender model inspector for the MVP 0 review app (plan §5.1).

Run inside Blender:

    blender --background --python worker/inspect_model.py -- \
        --path /abs/path/to/source.fbx \
        --out  /abs/path/to/inspect_result.json \
        [--project-id project_uuid]

Imports a source model (FBX / OBJ / GLB / GLTF), summarizes every mesh object
(vertices, edges, faces, materials, uv layers, world-space bounds, role hint) and
writes the app ``inspect_model`` contract JSON to ``--out``. The same document is
printed to stdout, but the app is expected to read ``--out`` (plan §3: UI reads
JSON status, not stdout).

Failures are written to ``--out`` as a structured error envelope and the process
exits non-zero, so the app never has to parse stdout to learn what went wrong
(plan §10 "import/export 편차" mitigation).
"""

from __future__ import annotations

import json
import os
import sys


def _parse_args(argv: list[str]) -> dict:
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            key = argv[i][2:].replace("-", "_")
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[key] = argv[i + 1]
                i += 2
            else:
                opts[key] = "true"
                i += 1
        else:
            i += 1
    return opts


def _ensure_importable() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for p in (root, here):
        if p not in sys.path:
            sys.path.insert(0, p)


def _import_source(bpy, path: str) -> None:
    """Import ``path`` into the current scene, branching on extension (plan §10)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        # Blender 4.x uses wm.obj_import; older builds expose import_scene.obj.
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:  # pragma: no cover - legacy Blender
            bpy.ops.import_scene.obj(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    else:
        raise ValueError(f"unsupported source format: {ext or '(none)'}")


def _world_bounds(obj):
    """World-space axis-aligned bounds as ``(min[3], max[3])`` or ``None``."""
    from mathutils import Vector

    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    if not corners:
        return None
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    zs = [c[2] for c in corners]
    return {
        "min": [round(min(xs), 6), round(min(ys), 6), round(min(zs), 6)],
        "max": [round(max(xs), 6), round(max(ys), 6), round(max(zs), 6)],
    }


def _summarize_object(contract, obj) -> dict:
    mesh = obj.data
    faces = len(mesh.polygons)
    return {
        "name": obj.name,
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "faces": faces,
        "materials": [m.name for m in mesh.materials if m is not None],
        "uv_layers": [uv.name for uv in mesh.uv_layers],
        "bounds": _world_bounds(obj),
        "mesh_role_hint": contract.role_hint(faces),
    }


def main() -> int:
    _ensure_importable()
    import app_job_contract as contract  # type: ignore

    opts = _parse_args(sys.argv)
    path = opts.get("path")
    out_path = opts.get("out")
    project_id = opts.get("project_id")

    if not path or not out_path:
        msg = "inspect_model requires --path and --out"
        print(msg, file=sys.stderr)
        if out_path:
            contract.write_json(out_path, contract.error_envelope(
                contract.CMD_INSPECT_MODEL, msg, code="bad_args"))
        return 2

    if not os.path.exists(path):
        contract.write_json(out_path, contract.error_envelope(
            contract.CMD_INSPECT_MODEL, f"source not found: {path}",
            code="source_missing", project_id=project_id, path=path))
        return 2

    ext = os.path.splitext(path)[1].lower()
    if ext not in contract.SUPPORTED_IMPORT_EXTS:
        contract.write_json(out_path, contract.error_envelope(
            contract.CMD_INSPECT_MODEL,
            f"unsupported format {ext!r}; supported: {', '.join(contract.SUPPORTED_IMPORT_EXTS)}",
            code="unsupported_format", project_id=project_id, path=path))
        return 2

    import bpy  # only available inside Blender

    # Start from an empty scene so we only summarize the imported model.
    try:
        bpy.ops.wm.read_homefile(use_empty=True)
    except Exception:  # noqa: BLE001 - best-effort; default scene is acceptable
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o, do_unlink=True)

    try:
        _import_source(bpy, path)
    except Exception as exc:  # noqa: BLE001 - import failure is structured (plan §10)
        contract.write_json(out_path, contract.error_envelope(
            contract.CMD_INSPECT_MODEL, f"import failed: {exc}",
            code="import_failed", project_id=project_id, path=path))
        print(f"inspect_model: import failed: {exc}", file=sys.stderr)
        return 3

    objects = [
        _summarize_object(contract, o)
        for o in bpy.data.objects
        if o.type == "MESH"
    ]

    result = {
        "schema_version": contract.SCHEMA_VERSION,
        "status": contract.STATUS_ACCEPTED,
        "command": contract.CMD_INSPECT_MODEL,
        "project_id": project_id,
        "path": path,
        "objects": objects,
        "recommended_next_step": contract.recommended_next_step(objects),
    }
    if not objects:
        result["warnings"] = ["no mesh objects found in source"]

    contract.write_json(out_path, result)
    print(json.dumps(result))
    print(f"inspect_model: {len(objects)} mesh object(s) -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
