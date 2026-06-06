"""Headless demo / offline job runner (no Blender required).

Runs the full agent pipeline on a synthetic mesh and writes the same artifacts
the Blender worker would (solution.json, evaluation.json, preview.svg). Useful
for trying the engine and for the web-app worker before a real Blender is wired
in.

    python -m uv_agent.demo --shape cylinder --provider mock --out out/demo
"""

from __future__ import annotations

import argparse
import json
import os

from uv_agent.agent.llm import get_provider
from uv_agent.agent.pipeline import UVAgentPipeline
from uv_agent.geometry.preview import uv_layout_svg
from uv_agent.io import fixtures
from uv_agent.planner.island_planner import PlanConstraints

SHAPES = {
    "cube": lambda: fixtures.build_cube(),
    "plane": lambda: fixtures.build_grid_plane(4, 4),
    "cylinder": lambda: fixtures.build_cylinder(16, 4),
    "two_mat": lambda: fixtures.build_two_material_plane(4, 4),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI Direct UV Layout Agent - demo runner")
    ap.add_argument("--shape", choices=sorted(SHAPES), default="cylinder")
    ap.add_argument("--provider", default="mock", help="mock | openai_oauth_local | openai_api_key")
    ap.add_argument("--intent", default="unwrap for hard-surface texturing")
    ap.add_argument("--angle-threshold", type=float, default=30.0)
    ap.add_argument("--padding-px", type=int, default=8)
    ap.add_argument("--texture-size", type=int, default=1024)
    ap.add_argument("--max-iterations", type=int, default=4)
    ap.add_argument("--out", default="out/demo")
    args = ap.parse_args(argv)

    mesh = SHAPES[args.shape]()
    provider = get_provider(args.provider)
    pipeline = UVAgentPipeline(
        provider,
        max_iterations=args.max_iterations,
        angle_threshold=args.angle_threshold,
    )
    constraints = PlanConstraints(padding_px=args.padding_px, texture_size_px=args.texture_size)

    result = pipeline.run(mesh, args.intent, constraints=constraints)

    os.makedirs(args.out, exist_ok=True)
    uvmap = result.solution.to_uvmap(mesh)
    with open(os.path.join(args.out, "solution.json"), "w", encoding="utf-8") as fh:
        json.dump(result.solution.to_dict(), fh, indent=2)
    with open(os.path.join(args.out, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, indent=2)
    svg_path = os.path.join(args.out, "preview.svg")
    with open(svg_path, "w", encoding="utf-8") as fh:
        fh.write(uv_layout_svg(mesh, result.plan, uvmap, title=f"{mesh.object_id} [{result.evaluation.status}]"))

    ev = result.evaluation
    print(f"shape={args.shape} provider={provider.name}")
    print(f"iterations={len(result.history)} islands={ev.island_count} status={ev.status}")
    print(
        f"  overlap={ev.overlap_ratio} stretch={ev.stretch_score} "
        f"angle={ev.angle_distortion} packing={ev.packing_efficiency}"
    )
    for rec in result.history:
        tools = [s["tool"] for s in rec.agent_output["plan"]] if rec.agent_output else []
        print(
            f"  it{rec.iteration}: status={rec.evaluation.status} "
            f"stretch={rec.evaluation.stretch_score} overlap={rec.evaluation.overlap_ratio} "
            f"-> {tools}"
        )
    print(f"artifacts -> {args.out}/ (solution.json, result.json, preview.svg)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
