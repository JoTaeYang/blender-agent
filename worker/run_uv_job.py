"""Headless Blender worker entrypoint (plan §9.2).

Run inside Blender:

    blender --background project.blend \
        --python worker/run_uv_job.py -- --job job.json

``job.json`` (plan §13 BlenderJob inputs) looks like:

    {
      "job_id": "job_123",
      "object_name": "robot_arm_001",
      "user_intent": "hard-surface texturing unwrap",
      "provider": "mock",                 # or openai_oauth_local / openai_api_key
      "angle_threshold": 30,
      "padding_px": 8,
      "texture_size_px": 1024,
      "out_dir": "out/job_123"
    }

Outputs written to ``out_dir``: ``solution.json``, ``evaluation.json``,
``preview.svg`` and (if rendering succeeds) ``preview.png``.
"""

from __future__ import annotations

import json
import os
import sys


def _parse_args(argv: list[str]) -> dict:
    # Only consider args after the "--" separator (Blender convention).
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            key = argv[i][2:]
            val = argv[i + 1] if i + 1 < len(argv) else "true"
            opts[key] = val
            i += 2
        else:
            i += 1
    return opts


def _ensure_uv_agent_importable() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def main() -> int:
    _ensure_uv_agent_importable()

    import bpy  # only available inside Blender

    from uv_agent.agent.llm import get_provider
    from uv_agent.agent.pipeline import UVAgentPipeline
    from uv_agent.blender.apply import apply_checker_material, apply_uv_coordinates
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.geometry.preview import uv_layout_svg
    from uv_agent.planner.island_planner import PlanConstraints

    opts = _parse_args(sys.argv)
    if "job" in opts:
        with open(opts["job"], "r", encoding="utf-8") as fh:
            job = json.load(fh)
    else:
        job = dict(opts)

    object_name = job.get("object_name")
    obj = bpy.data.objects.get(object_name) if object_name else None
    if obj is None:
        # Fall back to the first mesh object in the scene.
        obj = next((o for o in bpy.data.objects if o.type == "MESH"), None)
    if obj is None:
        print("run_uv_job: no mesh object found", file=sys.stderr)
        return 2

    out_dir = job.get("out_dir", os.path.join("out", str(job.get("job_id", "job"))))
    os.makedirs(out_dir, exist_ok=True)

    constraints = PlanConstraints(
        padding_px=int(job.get("padding_px", 8)),
        texture_size_px=int(job.get("texture_size_px", 1024)),
        max_overlap_ratio=float(job.get("max_overlap_ratio", 0.0)),
    )
    provider = get_provider(job.get("provider", "mock"))
    pipeline = UVAgentPipeline(
        provider,
        max_iterations=int(job.get("max_iterations", 4)),
        angle_threshold=float(job.get("angle_threshold", 30.0)),
    )

    mesh_graph = extract_mesh_graph(obj)
    result = pipeline.run(mesh_graph, job.get("user_intent", ""), constraints=constraints)

    written = apply_uv_coordinates(obj, result.solution, seam_edge_ids=result.plan.seam_edge_ids)
    print(f"run_uv_job: wrote {written} loop UVs to '{obj.name}'")

    with open(os.path.join(out_dir, "solution.json"), "w", encoding="utf-8") as fh:
        json.dump(result.solution.to_dict(), fh, indent=2)
    with open(os.path.join(out_dir, "evaluation.json"), "w", encoding="utf-8") as fh:
        json.dump(result.to_dict()["evaluation"], fh, indent=2)
    uvmap = result.solution.to_uvmap(mesh_graph)
    with open(os.path.join(out_dir, "preview.svg"), "w", encoding="utf-8") as fh:
        fh.write(uv_layout_svg(mesh_graph, result.plan, uvmap, title=f"{obj.name} {result.evaluation.status}"))

    # Optional textured render preview.
    try:
        apply_checker_material(obj)
        png_path = os.path.join(out_dir, "preview.png")
        bpy.context.scene.render.filepath = os.path.abspath(png_path)
        bpy.context.scene.render.image_settings.file_format = "PNG"
        bpy.ops.render.render(write_still=True)
        print(f"run_uv_job: render -> {png_path}")
    except Exception as exc:  # rendering is best-effort
        print(f"run_uv_job: render skipped ({exc})")

    print(f"run_uv_job: status={result.evaluation.status} out_dir={out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
