"""Track-1 organic unwrap probe (UV repair plan §3/§5).

Calibrates the stretch baseline on the reference's own UVs, then runs organic-seam
unwrap on an A4-accepted mesh and prints the gate metrics vs that baseline (+ the
Smart-UV baseline for comparison). Throwaway; informs how much Track 2 is needed.

    Blender --background --python scripts/spike_uv_organic.py -- \
        --mesh out/adaptive_acc_5850/adaptive_t5850.blend \
        --reference sample/humanstatue_low.obj --out out/uv_probe
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

import bpy


def _add_repo_to_path():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if root not in sys.path:
        sys.path.insert(0, root)


def _parse():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    o, i = {}, 0
    while i < len(argv):
        if argv[i].startswith("--"):
            k = argv[i][2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                o[k] = argv[i + 1]; i += 2
            else:
                o[k] = "true"; i += 1
        else:
            i += 1
    return o


def main() -> int:
    _add_repo_to_path()
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.blender.organic_unwrap import (
        island_plan_from_seams, read_uvmap, unwrap_organic,
    )
    from uv_agent.geometry.evaluation import (
        estimate_vt_count, evaluate_uv_solution, per_face_stretch, uv_bounds_ok,
    )
    from uv_agent.planner.island_planner import PlanConstraints, plan_islands
    from uv_agent.planner.organic_seams import (
        classify_seam_strategy, edge_over_threshold_fraction, organic_seam_edges,
    )

    opts = _parse()
    mesh_path = opts.get("mesh", "out/adaptive_acc_5850/adaptive_t5850.blend")
    ref_path = opts.get("reference", "sample/humanstatue_low.obj")
    out_dir = opts.get("out", "out/uv_probe")
    os.makedirs(out_dir, exist_ok=True)

    # --- Reference UV baseline (its own artist UVs). ---
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.obj_import(filepath=os.path.abspath(ref_path))
    ref = next(o for o in bpy.data.objects if o.type == "MESH")
    mg_ref = extract_mesh_graph(ref)
    uv_ref = read_uvmap(ref, mg_ref, layer_name=ref.data.uv_layers.active.name)
    plan_ref = plan_islands(mg_ref, angle_threshold=1e9, split_by_material=False)
    ev_ref = evaluate_uv_solution(mg_ref, plan_ref, uv_ref)
    vt_ref = estimate_vt_count(mg_ref, uv_ref)
    print(f"[ref] stretch={ev_ref.stretch_score:.5f} overlap={ev_ref.overlap_ratio:.5f} "
          f"vt/v={vt_ref/mg_ref.vertex_count:.4f} ({vt_ref}/{mg_ref.vertex_count}) "
          f"pack={ev_ref.packing_efficiency:.3f}", flush=True)

    # --- Load the candidate mesh. ---
    bpy.ops.wm.open_mainfile(filepath=os.path.abspath(mesh_path))
    low = next(o for o in bpy.data.objects if o.type == "MESH")
    mg = extract_mesh_graph(low)
    frac = edge_over_threshold_fraction(mg, 30.0)
    strat = classify_seam_strategy(mg, angle_threshold=30.0)
    print(f"[mesh] '{low.name}' faces={mg.face_count} verts={mg.vertex_count} "
          f"edge_over_30deg={frac:.3f} -> strategy={strat}", flush=True)

    from uv_agent.blender.organic_unwrap import organic_unwrap_with_refinement
    from uv_agent.geometry.uv_gate import UVReferenceBaseline, UVGateThresholds

    baseline = UVReferenceBaseline(stretch_score=ev_ref.stretch_score,
                                   vt_v_ratio=vt_ref/mg_ref.vertex_count,
                                   island_count=ev_ref.island_count)
    t = time.monotonic()
    res = organic_unwrap_with_refinement(
        low, mg, baseline=baseline, thresholds=UVGateThresholds(),
        max_rounds=int(opts.get("rounds", 14)), n_extremities=int(opts.get("extremities", 8)))
    ev, m, gate = res["evaluation"], res["metrics"], res["gate"]
    print(f"[organic] {len(res['seams'])} seams, {ev.island_count} islands, "
          f"overlap={m['overlap_ratio']:.5f} stretch={m['stretch_score']:.5f} "
          f"small_isl={m['small_island_ratio']:.3f} vt/v={m['vt_v_ratio']:.4f} "
          f"pack={m['packing_efficiency']:.3f} bounds={m['uv_bounds_ok']} "
          f"rounds={res['rounds']} [{time.monotonic()-t:.1f}s]", flush=True)
    print(f"[gate] verdict={gate.verdict} failures={[c.name for c in gate.failures]}", flush=True)
    for c in gate.checks:
        print(f"   [{'ok ' if c.passed else 'FAIL'}] {c.name}: value={c.value} limit={c.limit}", flush=True)
    print("[history]", flush=True)
    for h in res["history"]:
        print(f"   r{h['round']} {h['action']}: islands={h['islands']} stretch={h['stretch']} "
              f"overlap={h['overlap']} vt/v={h['vt_v']} pack={h['packing']}", flush=True)

    json.dump({
        "reference": {"stretch": ev_ref.stretch_score, "vt_v": vt_ref/mg_ref.vertex_count,
                      "overlap": ev_ref.overlap_ratio},
        "organic": {"metrics": m, "gate": gate.to_dict(), "rounds": res["rounds"]},
        "history": res["history"],
    }, open(os.path.join(out_dir, "uv_probe.json"), "w"), indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
