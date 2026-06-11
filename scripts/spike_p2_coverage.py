"""P2 cause-isolation experiment — where do the trident prongs survive? (plan §8)

Throwaway diagnostic. Opens the P1 ``proxy.blend`` once and runs a matrix of
QuadriFlow configs in a single Blender session (≈30 s each), measuring **directional
coverage** for every one:

    * bbox per-axis coverage (cheap screen: does the quad reach the proxy's extent)
    * proxy→quad distance max / p99 (does the quad miss any proxy region) — the
      direction the P1 quad→proxy fidelity is blind to.

Matrix: 2900 @ seeds 0–3, 2900 @ preserve_sharp, 6k / 10k / 20k, and two-stage
10k→2900. Renders front+side silhouettes per config so the prongs can be eyeballed.
Writes ``p2_coverage.json`` + a printed table; from it we calibrate the P2 coverage
hard-assert thresholds and decide the retry ladder.

    /Applications/Blender.app/Contents/MacOS/Blender --background \
        --python scripts/spike_p2_coverage.py -- --proxy out/quad_p1/proxy.blend \
        --out out/quad_p1/p2_exp
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
    opts = {}
    i = 0
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
    try:
        scene.display.shading.light = "STUDIO"
        scene.display.shading.show_object_outline = False
    except Exception:
        pass
    corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
    centre = sum(corners, mathutils.Vector()) / 8.0
    radius = max((c - centre).length for c in corners)
    cam_data = bpy.data.cameras.new("c"); cam_data.type = "ORTHO"; cam_data.ortho_scale = radius * 2.2
    cam = bpy.data.objects.new("c", cam_data); bpy.context.collection.objects.link(cam); scene.camera = cam
    for name, d in (("front", mathutils.Vector((0, -1, 0))), ("side", mathutils.Vector((1, 0, 0)))):
        cam.location = centre + d * radius * 3
        cam.rotation_euler = (centre - cam.location).normalized().to_track_quat("-Z", "Z").to_euler()
        scene.render.filepath = os.path.join(out_dir, f"{tag}_{name}.png")
        bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(cam, do_unlink=True)
    bpy.data.cameras.remove(cam_data, do_unlink=True)


def main() -> int:
    _add_repo_to_path()
    from retopo_agent.blender.quadremesh import (
        _assess_quad_mesh,
        _quadriflow_once,
        _remove_object,
        bbox_axis_coverage,
        directional_coverage,
    )

    opts = _parse_args()
    proxy_path = opts.get("proxy", "out/quad_p1/proxy.blend")
    out_dir = opts.get("out", "out/quad_p1/p2_exp")
    do_render = "no-render" not in opts
    os.makedirs(out_dir, exist_ok=True)

    bpy.ops.wm.open_mainfile(filepath=os.path.abspath(proxy_path))
    proxy = bpy.data.objects.get("AI_Proxy") or next(o for o in bpy.data.objects if o.type == "MESH")
    bound = 2
    print(f"[P2-exp] proxy '{proxy.name}' {len(proxy.data.polygons)} faces", flush=True)

    # (label, target, seed, preserve_sharp, two_stage_stage1_or_None)
    matrix = [
        ("t2900_s0", 2900, 0, False, None),
        ("t2900_s1", 2900, 1, False, None),
        ("t2900_s2", 2900, 2, False, None),
        ("t2900_s3", 2900, 3, False, None),
        ("t2900_sharp", 2900, 0, True, None),
        ("t6000", 6000, 0, False, None),
        ("t10000", 10000, 0, False, None),
        ("t20000", 20000, 0, False, None),
        ("two_stage_10k_2900", 2900, 0, False, 10000),
    ]

    results = []
    for label, target, seed, sharp, stage1 in matrix:
        t = time.monotonic()
        src = proxy
        intermediate = None
        if stage1 is not None:
            intermediate = _quadriflow_once(proxy, stage1, seed=seed, preserve_sharp=sharp,
                                            preserve_boundary=False, name="stage1")
            if intermediate is None:
                results.append({"label": label, "error": "stage1 failed"}); continue
            src = intermediate
        quad = _quadriflow_once(src, target, seed=seed, preserve_sharp=sharp,
                                preserve_boundary=False, name="quad")
        if quad is None:
            if intermediate is not None:
                _remove_object(intermediate)
            results.append({"label": label, "error": "quadriflow failed"}); continue

        metrics = _assess_quad_mesh(quad, target, bound)
        bbox = bbox_axis_coverage(quad, proxy)
        dirn = directional_coverage(proxy, quad)
        rec = {
            "label": label, "target": target, "seed": seed, "preserve_sharp": sharp,
            "two_stage": stage1, "faces": metrics["faces"], "quad_ratio": metrics["quad_ratio"],
            "tris": metrics["tris"], "ngons": metrics["ngons"],
            "non_manifold": metrics["non_manifold_edges"], "components": metrics["components"],
            "bbox_per_axis": bbox["per_axis"], "bbox_min_ratio": bbox["min_ratio"],
            "proxy_to_quad_max": dirn["max"], "proxy_to_quad_max_ratio": dirn["max_ratio"],
            "proxy_to_quad_p99": dirn["p99"], "proxy_to_quad_p99_ratio": dirn["p99_ratio"],
            "proxy_to_quad_mean": dirn["mean"], "proxy_to_quad_p90": dirn["p90"],
            "wall_s": round(time.monotonic() - t, 1),
        }
        results.append(rec)
        print(
            f"[P2-exp] {label}: {rec['faces']} faces, bbox_min={rec['bbox_min_ratio']} "
            f"(xyz={bbox['per_axis']}), proxy->quad max={rec['proxy_to_quad_max']} "
            f"(r={rec['proxy_to_quad_max_ratio']}), p99={rec['proxy_to_quad_p99']} "
            f"(r={rec['proxy_to_quad_p99_ratio']})  {rec['wall_s']}s",
            flush=True,
        )
        if do_render:
            _render(quad, out_dir, label)
        _remove_object(quad)
        if intermediate is not None:
            _remove_object(intermediate)

    with open(os.path.join(out_dir, "p2_coverage.json"), "w") as fh:
        json.dump({"proxy": proxy_path, "proxy_faces": len(proxy.data.polygons), "results": results}, fh, indent=2)
    print(f"\n[P2-exp] wrote {os.path.join(out_dir, 'p2_coverage.json')}", flush=True)
    # Compact table.
    print("\nlabel                faces   bbox_min  xyz                       p2q_max(r)      p99(r)")
    for r in results:
        if "error" in r:
            print(f"{r['label']:<20} ERROR {r['error']}"); continue
        ax = r["bbox_per_axis"]
        print(
            f"{r['label']:<20} {r['faces']:<6} {r['bbox_min_ratio']:<9} "
            f"x{ax['x']} y{ax['y']} z{ax['z']}   "
            f"{r['proxy_to_quad_max']}({r['proxy_to_quad_max_ratio']})  "
            f"{r['proxy_to_quad_p99']}({r['proxy_to_quad_p99_ratio']})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
