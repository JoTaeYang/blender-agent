"""U0 calibration (chart-UV plan §4): measure organic-pelt vs Smart-UV vs reference
artist UVs on the SAME 5,850 mesh, to pin the chart-engine hard thresholds.

    Blender --background --python scripts/spike_uv_calibrate.py
"""
import json, math, os, sys
sys.path.insert(0, os.getcwd())
import bpy
from uv_agent.blender.extract import extract_mesh_graph
from uv_agent.blender.organic_unwrap import read_uvmap
from uv_agent.geometry.evaluation import (
    evaluate_uv_solution, estimate_vt_count, uv_islands_from_uvmap,
)
from uv_agent.planner.island_planner import Island, IslandPlan, PlanConstraints

def plan_from_islands(mesh, islands):
    isl = [Island(island_id=f"i{n:03d}", face_ids=sorted(fs)) for n, fs in enumerate(islands)]
    return IslandPlan(islands=isl, seam_edge_ids=[], constraints=PlanConstraints())

def measure(mesh, uvmap, label):
    islands = uv_islands_from_uvmap(mesh, uvmap)
    plan = plan_from_islands(mesh, islands)
    ev = evaluate_uv_solution(mesh, plan, uvmap)
    vt = estimate_vt_count(mesh, uvmap)
    rec = {"label": label, "islands": ev.island_count, "stretch": round(ev.stretch_score,4),
           "overlap": round(ev.overlap_ratio,5), "packing": round(ev.packing_efficiency,4),
           "texel_var": round(ev.texel_density_variance,4),
           "small_isl": round(ev.small_island_ratio,3), "vt_v": round(vt/mesh.vertex_count,4)}
    print(f"[{label:14}] islands={rec['islands']:3} stretch={rec['stretch']:.4f} "
          f"overlap={rec['overlap']:.5f} packing={rec['packing']:.4f} "
          f"texel_var={rec['texel_var']:.4f} vt/v={rec['vt_v']:.4f}", flush=True)
    return rec

out = {}
# (c) reference artist UVs
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.obj_import(filepath=os.path.abspath("sample/humanstatue_low.obj"))
ref = next(o for o in bpy.data.objects if o.type=="MESH")
mgr = extract_mesh_graph(ref)
uvr = read_uvmap(ref, mgr, layer_name=ref.data.uv_layers.active.name)
out["reference"] = measure(mgr, uvr, "reference")

# (a) organic pelt result
bpy.ops.wm.open_mainfile(filepath=os.path.abspath("out/acceptance/t5850/adaptive_t5850.blend"))
low = next(o for o in bpy.data.objects if o.type=="MESH")
mg = extract_mesh_graph(low)
uvo = read_uvmap(low, mg, layer_name="AI_UV")
out["organic"] = measure(mg, uvo, "organic_pelt")

# (b) Smart-UV diagnostic on the same mesh
if "SP" not in low.data.uv_layers: low.data.uv_layers.new(name="SP")
low.data.uv_layers.active = low.data.uv_layers["SP"]
bpy.ops.object.select_all(action="DESELECT"); low.select_set(True); bpy.context.view_layer.objects.active=low
bpy.ops.object.mode_set(mode="EDIT"); bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.02)
bpy.ops.object.mode_set(mode="OBJECT")
uvs = read_uvmap(low, mg, layer_name="SP")
out["smart_uv"] = measure(mg, uvs, "smart_uv")

# Pin thresholds
smart_stretch = out["smart_uv"]["stretch"]
stretch_bar = max(0.5, smart_stretch*1.5)
print(f"\n[calib] stretch_bar = max(0.5, smart_uv {smart_stretch:.3f} x1.5) = {stretch_bar:.3f}", flush=True)
print(f"[calib] packing_bar = 0.70 (target); reference packing = {out['reference']['packing']:.3f}", flush=True)
print(f"[calib] texel_var bar = reference {out['reference']['texel_var']:.4f} x 2 = {out['reference']['texel_var']*2:.4f}", flush=True)
os.makedirs("out/uv_calib", exist_ok=True)
json.dump({"layouts": out, "stretch_bar": stretch_bar}, open("out/uv_calib/calib.json","w"), indent=2)
