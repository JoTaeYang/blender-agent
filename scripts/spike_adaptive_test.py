"""A2→A3→A4 end-to-end smoke test on the REAL proxy (Adaptive Low-Poly plan §5–§7).

Throwaway driver: opens the P1 ``proxy.blend`` (the clean 1M manifold), imports the
ground-truth reference to compute the A4 gate baseline, then runs the adaptive
generator the new code implements — A2 adaptive decimation, A3 tris→quads cleanup,
A4 quality gate — for one or more target budgets, printing the per-stage metrics and
the gate verdict. Renders a fixed-camera front/side silhouette per result.

    /Applications/Blender.app/Contents/MacOS/Blender --background --python \
        scripts/spike_adaptive_test.py -- \
        --proxy out/quad_p1/proxy.blend --reference sample/humanstatue_low.obj \
        --targets 5850,10000,2900 --out out/adaptive_smoke
"""

from __future__ import annotations

import json
import os
import sys
import time

import bpy


def _add_repo_to_path():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if root not in sys.path:
        sys.path.insert(0, root)


def _parse_args() -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    opts, i = {}, 0
    while i < len(argv):
        if argv[i].startswith("--"):
            key = argv[i][2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[key] = argv[i + 1]; i += 2
            else:
                opts[key] = "true"; i += 1
        else:
            i += 1
    return opts


def _render(obj, out_dir, tag):
    import mathutils
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = 600
    corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
    centre = sum(corners, mathutils.Vector()) / 8.0
    radius = max((c - centre).length for c in corners) or 1.0
    cam_data = bpy.data.cameras.new("c"); cam_data.type = "ORTHO"; cam_data.ortho_scale = radius * 2.2
    cam = bpy.data.objects.new("c", cam_data); bpy.context.collection.objects.link(cam); scene.camera = cam
    for name, d in (("front", mathutils.Vector((0, -1, 0))), ("side", mathutils.Vector((1, 0, 0)))):
        cam.location = centre + d * radius * 3
        cam.rotation_euler = (centre - cam.location).normalized().to_track_quat("-Z", "Z").to_euler()
        scene.render.filepath = os.path.join(out_dir, f"{tag}_{name}.png")
        bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(cam, do_unlink=True)
    bpy.data.cameras.remove(cam_data, do_unlink=True)


def measure_baseline(proxy, ref):
    """Reference (ground truth) measured vs the proxy in the same world space (§7)."""
    from retopo_agent.blender.quadremesh import directional_coverage
    from retopo_agent.blender.shape import evaluate_shape_match_blender
    from retopo_agent.geometry.adaptive_gate import ReferenceBaseline
    from retopo_agent.geometry.shape_eval import DECIMATION_SHAPE_THRESHOLDS

    p2r = directional_coverage(proxy, ref)             # proxy samples -> nearest ref
    r2p = evaluate_shape_match_blender(proxy, ref, thresholds=DECIMATION_SHAPE_THRESHOLDS)
    return ReferenceBaseline(
        proxy_to_ref_max=float(p2r["max"]),
        proxy_to_ref_p99=float(p2r["p99"]),
        ref_to_proxy_mean=float(r2p.surface_distance_mean),
        ref_to_proxy_normal_dev=float(r2p.normal_deviation_mean_deg),
        ref_vertex_count=len(ref.data.vertices),
    )


def main() -> int:
    _add_repo_to_path()
    from retopo_agent.blender.adaptive_decimate import (
        adaptive_decimate_proxy,
        cleanup_to_mixed_poly,
        CleanupAssertionError,
    )
    from retopo_agent.geometry.adaptive_gate import GateThresholds, evaluate_gate, next_rung

    opts = _parse_args()
    proxy_path = opts.get("proxy", "out/quad_p1/proxy.blend")
    ref_path = opts.get("reference", "sample/humanstatue_low.obj")
    targets = [int(x) for x in opts.get("targets", "5850").split(",")]
    out_dir = opts.get("out", "out/adaptive_smoke")
    do_render = "no-render" not in opts
    os.makedirs(out_dir, exist_ok=True)

    bpy.ops.wm.open_mainfile(filepath=os.path.abspath(proxy_path))
    proxy = bpy.data.objects.get("AI_Proxy") or next(o for o in bpy.data.objects if o.type == "MESH")
    print(f"[smoke] proxy '{proxy.name}' {len(proxy.data.polygons)} faces", flush=True)

    # Import the ground-truth reference for the A4 baseline.
    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(filepath=os.path.abspath(ref_path))
    ref = next(o for o in bpy.data.objects if o not in before and o.type == "MESH")
    print(f"[smoke] reference '{ref.name}' {len(ref.data.polygons)} faces / {len(ref.data.vertices)} verts", flush=True)

    baseline = measure_baseline(proxy, ref)
    print(f"[smoke] baseline (reference vs proxy): {baseline.to_dict()}", flush=True)

    report = {"proxy": proxy_path, "reference": ref_path,
              "proxy_faces": len(proxy.data.polygons), "baseline": baseline.to_dict(),
              "runs": []}

    for target in targets:
        t0 = time.monotonic()
        print(f"\n[smoke] ===== target {target} faces =====", flush=True)

        # --- A2: adaptive decimation on the proxy.
        a2 = adaptive_decimate_proxy(proxy, target, shrinkwrap=True)
        print(f"[smoke] A2: {a2.actual_face_count} faces (band={a2.band}, ratio={a2.ratio:.4g}, "
              f"stopped={a2.stopped_reason}); tris={a2.attempt.tris} quads={a2.attempt.quads} "
              f"ngons={a2.attempt.ngons} non_manifold={a2.attempt.non_manifold_edges} "
              f"components={a2.attempt.components} bbox_min={a2.attempt.bbox_min_ratio} "
              f"sw={a2.attempt.shrinkwrap_applied}", flush=True)

        low = a2.obj

        # --- A3: tris->quads cleanup (component_bound=2 to allow the proxy floater).
        a3 = None
        try:
            a3 = cleanup_to_mixed_poly(low, target_face_count=target, component_bound=2)
            print(f"[smoke] A3: tris={a3['after']['tris']} quads={a3['after']['quads']} "
                  f"ngons={a3['after']['ngons']} (quads_gained={a3['quads_gained']}) "
                  f"faces={a3['after']['faces']} asserts_ok={a3['asserts']['all_ok']}", flush=True)
        except CleanupAssertionError as exc:
            print(f"[smoke] A3 FAILED asserts: {exc}", flush=True)

        # --- A4: quality gate (re-measure the post-A3 mesh for the gate metrics).
        from retopo_agent.blender.adaptive_decimate import (
            _low_to_proxy_shape, _mesh_face_breakdown, _mesh_topology,
        )
        from retopo_agent.blender.quadremesh import bbox_axis_coverage, directional_coverage
        bd = _mesh_face_breakdown(low)
        topo = _mesh_topology(low)
        gate_metrics = {
            "ngons": bd["ngons"], "non_manifold_edges": topo["non_manifold_edges"],
            "faces": bd["faces"], "vertex_count": len(low.data.vertices),
            "bbox_per_axis": bbox_axis_coverage(low, proxy)["per_axis"],
            "proxy_to_low": {k: directional_coverage(proxy, low).get(k) for k in ("max", "p99")},
            "low_to_proxy": _low_to_proxy_shape(low, proxy),
        }
        gate = evaluate_gate(gate_metrics, target_face_count=target, baseline=baseline,
                             thresholds=GateThresholds())
        rung = next_rung(gate, attempted_rungs=[])
        print(f"[smoke] A4 gate: verdict={gate.verdict} passed_hard={gate.passed_hard} "
              f"hard_fail={[c.name for c in gate.hard_failures]} "
              f"soft_fail={[c.name for c in gate.soft_failures]} "
              f"sanity_warn={[c.name for c in gate.sanity_warnings]} "
              f"next_rung={rung or '(none)'}", flush=True)
        for c in gate.checks:
            flag = "ok " if c.passed else "FAIL"
            print(f"[smoke]   [{flag}] {c.kind:<6} {c.name}: {c.detail}", flush=True)

        if "export" in opts:
            low.name = f"AI_Adaptive_{target}"
            low.data.name = low.name
            obj_path = os.path.join(out_dir, f"adaptive_t{target}.obj")
            for o in bpy.context.view_layer.objects:
                o.select_set(False)
            low.select_set(True)
            bpy.context.view_layer.objects.active = low
            bpy.ops.wm.obj_export(
                filepath=os.path.abspath(obj_path), export_selected_objects=True,
                export_normals=True, export_uv=True, export_materials=False,
            )
            blend_path = os.path.join(out_dir, f"adaptive_t{target}.blend")
            bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(blend_path), copy=True)
            print(f"[smoke] exported {obj_path} + {blend_path}", flush=True)

        if do_render:
            _render(low, out_dir, f"adaptive_t{target}")
            _render(ref, out_dir, "reference")

        report["runs"].append({
            "target": target, "wall_s": round(time.monotonic() - t0, 1),
            "a2": a2.to_dict(), "a3": a3, "gate": gate.to_dict(),
            "gate_metrics": gate_metrics, "next_rung": rung,
        })

        # Free the result before the next budget so memory stays flat.
        bpy.data.objects.remove(low, do_unlink=True)

    with open(os.path.join(out_dir, "adaptive_smoke.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"\n[smoke] wrote {os.path.join(out_dir, 'adaptive_smoke.json')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
