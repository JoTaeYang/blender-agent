"""Run the AI UV Layout Agent on an object inside Blender.

Two ways to use it:

1) Interactive (see it live in the UV Editor)
   - Open Blender, select your mesh object.
   - Scripting workspace -> open this file -> Run Script (Alt+P).
   - Switch to "UV Editing" workspace to inspect the result (it writes an
     "AI_UV" UV map and makes it active).

2) Headless (no UI)
   blender --background your.blend \
       --python scripts/blender_unwrap_active.py -- --object MyObject

   Add a test primitive instead of needing a file:
   blender --background \
       --python scripts/blender_unwrap_active.py -- --add suzanne

Args after `--`:  --object NAME | --add {cube,suzanne,uvsphere,cylinder,torus}
                  --provider {mock,openai_oauth_local,openai_api_key}
                  --intent "..."  --angle 30  --padding 8  --texture 1024
"""

from __future__ import annotations

import os
import sys

import bpy


def _add_repo_to_path() -> None:
    # Priority: env var, then this file's repo root, then cwd.
    candidates = []
    if os.environ.get("UV_AGENT_REPO"):
        candidates.append(os.environ["UV_AGENT_REPO"])
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.dirname(here))  # repo root = scripts/..
    except NameError:
        pass
    candidates.append(os.getcwd())
    for root in candidates:
        if root and os.path.isdir(os.path.join(root, "uv_agent")):
            if root not in sys.path:
                sys.path.insert(0, root)
            return
    raise RuntimeError(
        "Could not locate the 'uv_agent' package. Set UV_AGENT_REPO=/path/to/brisbane"
    )


def _parse_args() -> dict:
    argv = sys.argv
    argv = argv[argv.index("--") + 1 :] if "--" in argv else []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            key = argv[i][2:]
            val = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else "true"
            opts[key] = val
            i += 2 if val != "true" else 1
        else:
            i += 1
    return opts


_PRIMITIVES = {
    "cube": lambda: bpy.ops.mesh.primitive_cube_add(),
    "suzanne": lambda: bpy.ops.mesh.primitive_monkey_add(),
    "uvsphere": lambda: bpy.ops.mesh.primitive_uv_sphere_add(),
    "cylinder": lambda: bpy.ops.mesh.primitive_cylinder_add(),
    "torus": lambda: bpy.ops.mesh.primitive_torus_add(),
}


def _import_file(path: str):
    """Import a mesh file and return the imported mesh object."""
    if not os.path.exists(path):
        raise SystemExit(f"--import file not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    before = set(bpy.data.objects)
    if ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext in (".gltf", ".glb"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    else:
        raise SystemExit(f"unsupported import format: {ext} (obj/fbx/gltf/glb/stl/ply)")
    new_meshes = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
    if not new_meshes:
        raise SystemExit(f"no mesh imported from {path}")
    return new_meshes[0]


def _resolve_object(opts: dict):
    if opts.get("import"):
        return _import_file(opts["import"])
    if opts.get("add"):
        shape = opts["add"]
        if shape not in _PRIMITIVES:
            raise SystemExit(f"--add must be one of {sorted(_PRIMITIVES)}")
        _PRIMITIVES[shape]()
        return bpy.context.active_object
    if opts.get("object"):
        obj = bpy.data.objects.get(opts["object"])
        if obj is None:
            raise SystemExit(f"object '{opts['object']}' not found")
        return obj
    obj = bpy.context.active_object
    if obj is None or obj.type != "MESH":
        raise SystemExit("No active mesh object. Select one, or pass --object NAME / --add suzanne")
    return obj


def run_on_object(obj, opts: dict):
    from uv_agent.agent.llm import get_provider
    from uv_agent.agent.pipeline import UVAgentPipeline
    from uv_agent.blender.apply import apply_uv_coordinates
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.planner.island_planner import PlanConstraints

    print(f"[AI-UV] object='{obj.name}'  faces={len(obj.data.polygons)}")

    mesh_graph = extract_mesh_graph(obj)
    print(f"[AI-UV] mesh graph: V={mesh_graph.vertex_count} E={mesh_graph.edge_count} F={mesh_graph.face_count}")

    provider = get_provider(opts.get("provider", "mock"))
    pipeline = UVAgentPipeline(
        provider,
        max_iterations=int(opts.get("iters", 4)),
        angle_threshold=float(opts.get("angle", 30.0)),
    )
    constraints = PlanConstraints(
        padding_px=int(opts.get("padding", 8)),
        texture_size_px=int(opts.get("texture", 1024)),
    )

    result = pipeline.run(mesh_graph, opts.get("intent", "unwrap for texturing"), constraints=constraints)

    written = apply_uv_coordinates(obj, result.solution, seam_edge_ids=result.plan.seam_edge_ids)

    if opts.get("svg"):
        from uv_agent.geometry.preview import uv_layout_svg

        uvmap = result.solution.to_uvmap(mesh_graph)
        with open(opts["svg"], "w", encoding="utf-8") as fh:
            fh.write(uv_layout_svg(mesh_graph, result.plan, uvmap,
                                   title=f"{obj.name} [{result.evaluation.status}]"))
        print(f"[AI-UV] wrote UV preview -> {opts['svg']}")

    if opts.get("save"):
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(opts["save"]))
        print(f"[AI-UV] saved .blend -> {opts['save']} (open it and go to 'UV Editing')")

    ev = result.evaluation
    print(f"[AI-UV] provider={provider.name}  iterations={len(result.history)}")
    print(f"[AI-UV] wrote {written} loop UVs into 'AI_UV' layer (now active)")
    print(f"[AI-UV] islands={ev.island_count}  status={ev.status}")
    print(f"[AI-UV]   overlap={ev.overlap_ratio} stretch={ev.stretch_score} "
          f"angle={ev.angle_distortion} packing={ev.packing_efficiency}")
    for rec in result.history:
        tools = [s["tool"] for s in rec.agent_output["plan"]] if rec.agent_output else []
        print(f"[AI-UV]   it{rec.iteration}: {rec.evaluation.status} "
              f"stretch={rec.evaluation.stretch_score} overlap={rec.evaluation.overlap_ratio} -> {tools}")
    return result


def main() -> int:
    _add_repo_to_path()
    opts = _parse_args()
    obj = _resolve_object(opts)
    # Make sure it's the active/selected object.
    bpy.context.view_layer.objects.active = obj
    run_on_object(obj, opts)
    print("[AI-UV] done. In the UI, open 'UV Editing' to inspect the AI_UV layout.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
