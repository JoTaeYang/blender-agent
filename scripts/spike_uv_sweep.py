"""Seam-density sweep: cut-tree + strong creases at various thresholds (UV repair §3).

One Blender session, opens the 5,850 mesh, and for each crease threshold reports the
ABF unwrap metrics, to find the seam density that lands stretch in the reference band.
"""
from __future__ import annotations
import os, sys, time
import bpy

def _add_repo():
    here = os.path.dirname(os.path.abspath(__file__)); root = os.path.dirname(here)
    if root not in sys.path: sys.path.insert(0, root)

def _parse():
    argv = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else []
    o,i={},0
    while i < len(argv):
        if argv[i].startswith("--"):
            k=argv[i][2:]
            if i+1<len(argv) and not argv[i+1].startswith("--"): o[k]=argv[i+1]; i+=2
            else: o[k]="true"; i+=1
        else: i+=1
    return o

def main():
    _add_repo()
    from uv_agent.blender.extract import extract_mesh_graph
    from uv_agent.blender.organic_unwrap import unwrap_organic, read_uvmap, island_plan_from_seams, build_uv_metrics
    from uv_agent.geometry.evaluation import evaluate_uv_solution
    from uv_agent.planner.island_planner import PlanConstraints
    from uv_agent.planner.organic_seams import organic_seam_edges

    opts = _parse()
    mesh_path = opts.get("mesh", "out/adaptive_acc_5850/adaptive_t5850.blend")
    bpy.ops.wm.open_mainfile(filepath=os.path.abspath(mesh_path))
    low = next(o for o in bpy.data.objects if o.type=="MESH")
    mg = extract_mesh_graph(low)
    tree = organic_seam_edges(mg, n_extremities=8)
    print(f"[sweep] faces={mg.face_count} cut_tree={len(tree)} edges", flush=True)

    def crease_edges(thresh):
        return {e.id for e in mg.edges if len(e.face_ids)==2 and e.dihedral_angle>=thresh}

    def mark(seams):
        ss=set(seams)
        for e in low.data.edges: e.use_seam = e.index in ss
        low.data.update()

    def full_unwrap(seams, minimize=0, avg_scale=False):
        if "AI_UV" not in low.data.uv_layers: low.data.uv_layers.new(name="AI_UV")
        low.data.uv_layers.active = low.data.uv_layers["AI_UV"]
        mark(seams)
        bpy.ops.object.select_all(action="DESELECT"); low.select_set(True)
        bpy.context.view_layer.objects.active = low
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            bpy.ops.mesh.select_all(action="SELECT"); bpy.ops.uv.select_all(action="SELECT")
            bpy.ops.uv.unwrap(method="ANGLE_BASED", margin=0.02)
            if minimize: bpy.ops.uv.minimize_stretch(iterations=minimize)
            if avg_scale: bpy.ops.uv.average_islands_scale()
            try: bpy.ops.uv.pack_islands(rotate=True, margin=0.02)
            except TypeError: bpy.ops.uv.pack_islands(margin=0.02)
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
        low.data.update()

    configs = [
        ("tree avg", set(tree), 0, True),
        ("tree min30avg", set(tree), 30, True),
        ("cr60 avg", tree|crease_edges(60), 0, True),
        ("cr60 min30avg", tree|crease_edges(60), 30, True),
        ("cr50 min30avg", tree|crease_edges(50), 30, True),
        ("cr60 min60avg", tree|crease_edges(60), 60, True),
    ]
    for label, seams, mn, av in configs:
        t=time.monotonic()
        full_unwrap(seams, minimize=mn, avg_scale=av)
        uv = read_uvmap(low, mg)
        plan = island_plan_from_seams(mg, seams, constraints=PlanConstraints())
        ev = evaluate_uv_solution(mg, plan, uv)
        m = build_uv_metrics(mg, uv, ev)
        print(f"[{label:16}] seams={len(seams):4} islands={ev.island_count:4} "
              f"stretch={m['stretch_score']:.4f} overlap={m['overlap_ratio']:.5f} "
              f"vt/v={m['vt_v_ratio']:.4f} pack={m['packing_efficiency']:.4f} "
              f"small={m['small_island_ratio']:.3f} [{time.monotonic()-t:.1f}s]", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
