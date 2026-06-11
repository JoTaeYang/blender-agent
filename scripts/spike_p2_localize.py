"""Localize WHERE the proxy→quad coverage gap is (P2 cause isolation, plan §8).

The coverage sweep showed huge proxy→quad max (86 units) and a 20%-shrunk bbox at
2,900 faces, yet the render shows a complete figure+trident. So *what* is the quad
missing? This opens proxy.blend, reports the proxy's component structure (face count
+ world bbox per shell), remeshes to a given target, and finds the proxy points that
are far from the quad — reporting their spatial cluster + which proxy component they
belong to. That pinpoints the missing region instead of guessing.

    Blender --background --python scripts/spike_p2_localize.py -- \
        --proxy out/quad_p1/proxy.blend --target 2900 --far 30
"""

from __future__ import annotations

import json
import os
import sys

import bpy


def _add_repo_to_path():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if root not in sys.path:
        sys.path.insert(0, root)


def _args() -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    o = {}
    i = 0
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


def _bbox(points):
    import numpy as np
    a = np.asarray(points, dtype=float)
    lo = a.min(axis=0).tolist()
    hi = a.max(axis=0).tolist()
    return {"min": [round(x, 1) for x in lo], "max": [round(x, 1) for x in hi],
            "size": [round(hi[i] - lo[i], 1) for i in range(3)]}


def main() -> int:
    _add_repo_to_path()
    import numpy as np
    from mathutils.bvhtree import BVHTree

    from retopo_agent.blender.quadremesh import _quadriflow_once

    o = _args()
    proxy_path = o.get("proxy", "out/quad_p1/proxy.blend")
    target = int(o.get("target", 2900))
    far = float(o.get("far", 30.0))

    bpy.ops.wm.open_mainfile(filepath=os.path.abspath(proxy_path))
    proxy = bpy.data.objects.get("AI_Proxy") or next(x for x in bpy.data.objects if x.type == "MESH")

    # --- proxy component structure (union-find + per-shell world bbox) -----------
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(proxy.data)
    bm.verts.index_update()
    parent = list(range(len(bm.verts)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    for e in bm.edges:
        a, b = e.verts[0].index, e.verts[1].index
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    comp_faces: dict[int, int] = {}
    comp_pts: dict[int, list] = {}
    mw = proxy.matrix_world
    for f in bm.faces:
        r = find(f.verts[0].index)
        comp_faces[r] = comp_faces.get(r, 0) + 1
    for v in bm.verts:
        r = find(v.index)
        comp_pts.setdefault(r, []).append(tuple(mw @ v.co))
    bm.free()

    comps = sorted(comp_faces.items(), key=lambda kv: kv[1], reverse=True)
    components = []
    for root, fcount in comps:
        components.append({"faces": fcount, "bbox": _bbox(comp_pts[root])})
    print(f"[loc] proxy components: {len(components)}", flush=True)
    for i, c in enumerate(components):
        print(f"[loc]   comp{i}: {c['faces']} faces, bbox size {c['bbox']['size']} "
              f"min {c['bbox']['min']} max {c['bbox']['max']}", flush=True)

    # --- remesh + far-point localization -----------------------------------------
    quad = _quadriflow_once(proxy, target, seed=0, preserve_sharp=False,
                            preserve_boundary=False, name="quad")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    tree = BVHTree.FromObject(quad, depsgraph)
    to_local = quad.matrix_world.inverted_safe() @ proxy.matrix_world

    verts = proxy.data.vertices
    n = len(verts)
    k = min(n, 60000)
    step = n / k
    far_pts = []
    all_d = []
    for j in range(k):
        vi = int(j * step)
        co = verts[vi].co
        loc, nrm, idx, d = tree.find_nearest(to_local @ co)
        if d is None:
            continue
        all_d.append(d)
        if d > far:
            far_pts.append(tuple(mw @ co))

    arr = np.asarray(all_d)
    out = {
        "proxy_components": components,
        "quad_faces": len(quad.data.polygons),
        "target": target,
        "samples": int(arr.size),
        "far_threshold": far,
        "far_count": len(far_pts),
        "far_fraction": round(len(far_pts) / max(1, arr.size), 4),
        "dist_max": round(float(arr.max()), 2),
        "dist_p99": round(float(np.percentile(arr, 99)), 2),
        "proxy_bbox": _bbox([tuple(mw @ v.co) for v in verts][:: max(1, n // 20000)]),
    }
    if far_pts:
        out["far_region_bbox"] = _bbox(far_pts)
    print(f"[loc] target {target}: quad {out['quad_faces']} faces, "
          f"far(>{far}) {out['far_count']}/{out['samples']} ({out['far_fraction']:.1%}), "
          f"max {out['dist_max']}", flush=True)
    if far_pts:
        print(f"[loc] FAR-REGION bbox: size {out['far_region_bbox']['size']} "
              f"min {out['far_region_bbox']['min']} max {out['far_region_bbox']['max']}", flush=True)
    print(f"[loc] proxy bbox: size {out['proxy_bbox']['size']}", flush=True)

    with open(o.get("json", "out/quad_p1/p2_exp/localize.json"), "w") as fh:
        json.dump(out, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
