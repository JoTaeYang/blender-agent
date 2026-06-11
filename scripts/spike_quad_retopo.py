"""Phase P0 spike — prove the QuadriFlow quad-retopo hypothesis cheaply.

THROWAWAY. This is the spike from docs/QUAD_RETOPO_PLAN.md §6: no architecture,
no reuse of the package, just enough to answer one question — can the pipeline

    import 24.9M-face OBJ -> proxy (decimate/voxel) -> QuadriFlow ~5.8k quads
    -> shrinkwrap -> export

actually run on this machine, and does QuadriFlow emit clean pure-quad output at
a ~5,800-face target? The real, structured pipeline is P1+. Do not build on this.

Run headless:

    /Applications/Blender.app/Contents/MacOS/Blender --background --python \
        scripts/spike_quad_retopo.py -- \
        --input sample/humanstatue.obj \
        --reference sample/humanstatue_low.obj \
        --target-faces 5800 \
        --out out/spike_p0

Knobs (all optional, sane defaults for the statue):
    --proxy-faces 1000000   target tri count for the proxy band
    --voxel-div 600         voxel_size = bbox_diagonal / voxel-div
    --decimate-first 1      collapse to ~2*proxy-faces before voxel remesh (memory)
    --no-render             skip the silhouette renders (faster)

Everything is wrapped so a single failing step still writes a report saying which
step failed, with timings and peak RSS up to that point — that IS the decision-gate
artifact the plan asks for.
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time

import bpy
import mathutils


# ----------------------------------------------------------------------------- args

def parse_args() -> dict:
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            key = argv[i][2:]
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt is not None and not nxt.startswith("--"):
                opts[key] = nxt
                i += 2
            else:
                opts[key] = "true"
                i += 1
        else:
            i += 1
    return opts


# ----------------------------------------------------------------------- timing/mem

def peak_rss_gb() -> float:
    """Peak resident set size of this process. On macOS ru_maxrss is bytes."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kB, macOS bytes — detect by magnitude.
    if rss > 1 << 40:  # absurd as bytes-from-kB; treat as bytes
        return rss / (1024 ** 3)
    if sys.platform == "darwin":
        return rss / (1024 ** 3)
    return rss / (1024 ** 2)


class Spike:
    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        self.t0 = time.monotonic()
        self.steps: list[dict] = []
        self.report: dict = {"steps": self.steps, "status": "running"}
        os.makedirs(out_dir, exist_ok=True)

    def step(self, name: str):
        """Context-manager-ish: returns a recorder you call .done(**metrics) on."""
        rec = {"name": name, "t_start_s": round(time.monotonic() - self.t0, 2)}
        self.steps.append(rec)
        print(f"\n[P0] === {name} ===", flush=True)
        self._t = time.monotonic()
        self._rec = rec
        return self

    def done(self, **metrics):
        dt = time.monotonic() - self._t
        self._rec["wall_s"] = round(dt, 2)
        self._rec["peak_rss_gb"] = round(peak_rss_gb(), 2)
        self._rec.update(metrics)
        line = " ".join(f"{k}={v}" for k, v in metrics.items())
        print(f"[P0] {self._rec['name']}: {dt:.1f}s  rss={self._rec['peak_rss_gb']}GB  {line}", flush=True)
        self.flush()

    def fail(self, exc: Exception):
        self._rec["wall_s"] = round(time.monotonic() - self._t, 2)
        self._rec["peak_rss_gb"] = round(peak_rss_gb(), 2)
        self._rec["error"] = f"{type(exc).__name__}: {exc}"
        self.report["status"] = "failed"
        self.report["failed_step"] = self._rec["name"]
        print(f"[P0] !! {self._rec['name']} FAILED: {self._rec['error']}", flush=True)
        self.flush()

    def flush(self):
        self.report["total_wall_s"] = round(time.monotonic() - self.t0, 2)
        self.report["peak_rss_gb"] = round(peak_rss_gb(), 2)
        with open(os.path.join(self.out_dir, "spike_report.json"), "w") as fh:
            json.dump(self.report, fh, indent=2)


# ------------------------------------------------------------------------- helpers

def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def mesh_stats(obj) -> dict:
    polys = obj.data.polygons
    n = len(polys)
    tris = quads = ngons = 0
    for p in polys:
        c = p.loop_total
        if c == 3:
            tris += 1
        elif c == 4:
            quads += 1
        else:
            ngons += 1
    return {
        "verts": len(obj.data.vertices),
        "faces": n,
        "tris": tris,
        "quads": quads,
        "ngons": ngons,
        "quad_ratio": round(quads / n, 4) if n else 0.0,
    }


def bbox_diagonal(obj) -> float:
    corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
    lo = mathutils.Vector((min(c[i] for c in corners) for i in range(3)))
    hi = mathutils.Vector((max(c[i] for c in corners) for i in range(3)))
    return (hi - lo).length


def make_active(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def purge_orphans():
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)


# ------------------------------------------------------------------------- renders

def render_silhouettes(obj, out_dir: str, tag: str) -> list[str]:
    """Best-effort ortho silhouette renders (front / side / 3-4). Workbench engine,
    flat matcap. Returns the written PNG paths."""
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = 512
    scene.render.film_transparent = False
    try:
        scene.display.shading.light = "FLAT"
        scene.display.shading.color_type = "SINGLE"
        scene.display.shading.single_color = (0.7, 0.7, 0.72)
    except Exception:
        pass

    # Centre + radius from the object's world bbox.
    corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
    centre = sum(corners, mathutils.Vector()) / 8.0
    radius = max((c - centre).length for c in corners)

    cam_data = bpy.data.cameras.new("spike_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = radius * 2.2
    cam = bpy.data.objects.new("spike_cam", cam_data)
    bpy.context.collection.objects.link(cam)
    scene.camera = cam

    views = {
        "front": mathutils.Vector((0, -1, 0)),
        "side": mathutils.Vector((1, 0, 0)),
        "three_quarter": mathutils.Vector((1, -1, 0.4)).normalized(),
    }
    paths = []
    dist = radius * 3.0
    for name, direction in views.items():
        cam.location = centre + direction * dist
        # Aim the -Z axis of the camera at the centre.
        track = (centre - cam.location).normalized()
        cam.rotation_euler = track.to_track_quat("-Z", "Y").to_euler()
        path = os.path.join(out_dir, f"{tag}_{name}.png")
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        paths.append(path)
        print(f"[P0] render -> {path}", flush=True)

    bpy.data.objects.remove(cam, do_unlink=True)
    bpy.data.cameras.remove(cam_data, do_unlink=True)
    return paths


# ---------------------------------------------------------------------------- main

def main() -> int:
    opts = parse_args()
    inp = opts.get("input", "sample/humanstatue.obj")
    ref = opts.get("reference", "sample/humanstatue_low.obj")
    out_dir = opts.get("out", "out/spike_p0")
    target_faces = int(opts.get("target-faces", 5800))
    proxy_faces = int(opts.get("proxy-faces", 1_000_000))
    voxel_div = float(opts.get("voxel-div", 600))
    decimate_first = opts.get("decimate-first", "1") not in ("0", "false", "no")
    do_render = "no-render" not in opts
    note = opts.get("note")

    spike = Spike(out_dir)
    spike.report["params"] = {
        "input": inp, "reference": ref, "target_faces": target_faces,
        "proxy_faces": proxy_faces, "voxel_div": voxel_div,
        "decimate_first": decimate_first, "render": do_render,
    }
    if note:
        spike.report["prior_findings"] = note
    spike.flush()

    reset_scene()

    # --- 1. import ----------------------------------------------------------
    spike.step("import")
    try:
        if not os.path.exists(inp):
            raise FileNotFoundError(inp)
        before = set(bpy.data.objects)
        bpy.ops.wm.obj_import(filepath=os.path.abspath(inp))
        meshes = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
        if not meshes:
            raise RuntimeError("no mesh imported")
        # Join multiple components into one object so later ops act on the whole mesh.
        if len(meshes) > 1:
            make_active(meshes[0])
            for m in meshes:
                m.select_set(True)
            bpy.ops.object.join()
        obj = bpy.context.view_layer.objects.active or meshes[0]
        obj.name = "spike_src"
        diag = bbox_diagonal(obj)
        st = mesh_stats(obj)
        spike.done(components=len(meshes), bbox_diag=round(diag, 2), **st)
    except Exception as exc:
        spike.fail(exc)
        return 1

    # --- 2. proxy: (optional) decimate then voxel remesh --------------------
    spike.step("proxy_build")
    try:
        make_active(obj)
        if decimate_first and len(obj.data.polygons) > proxy_faces * 2:
            ratio = min(1.0, (proxy_faces * 2) / len(obj.data.polygons))
            mod = obj.modifiers.new("spike_decimate", "DECIMATE")
            mod.decimate_type = "COLLAPSE"
            mod.ratio = ratio
            bpy.ops.object.modifier_apply(modifier=mod.name)
            print(f"[P0] pre-decimate ratio={ratio:.4g} -> {len(obj.data.polygons)} faces", flush=True)

        voxel_size = diag / voxel_div
        obj.data.remesh_voxel_size = voxel_size
        obj.data.remesh_voxel_adaptivity = 0.0
        bpy.ops.object.voxel_remesh()
        purge_orphans()
        st = mesh_stats(obj)
        spike.done(voxel_size=round(voxel_size, 4), **st)
        proxy_stats = st
    except Exception as exc:
        spike.fail(exc)
        return 1

    # Keep a copy of the proxy as the shrinkwrap target before remeshing destroys it.
    proxy_target = obj.copy()
    proxy_target.data = obj.data.copy()
    proxy_target.name = "spike_proxy_target"
    bpy.context.collection.objects.link(proxy_target)

    # --- 3. QuadriFlow ------------------------------------------------------
    spike.step("quadriflow")
    try:
        make_active(obj)
        bpy.ops.object.quadriflow_remesh(
            target_faces=target_faces,
            use_mesh_symmetry=False,
            use_preserve_sharp=False,
            use_preserve_boundary=False,
            seed=0,
        )
        st = mesh_stats(obj)
        spike.done(**st)
        quad_stats = st
    except Exception as exc:
        spike.fail(exc)
        return 1

    # --- 4. shrinkwrap onto the proxy --------------------------------------
    spike.step("shrinkwrap")
    try:
        make_active(obj)
        mod = obj.modifiers.new("spike_shrinkwrap", "SHRINKWRAP")
        mod.wrap_method = "NEAREST_SURFACEPOINT"
        mod.target = proxy_target
        bpy.ops.object.modifier_apply(modifier=mod.name)
        st = mesh_stats(obj)
        spike.done(**st)
    except Exception as exc:
        spike.fail(exc)
        return 1

    # --- 5. export ----------------------------------------------------------
    spike.step("export")
    try:
        make_active(obj)
        out_obj = os.path.join(out_dir, "spike_quad.obj")
        bpy.ops.wm.obj_export(
            filepath=os.path.abspath(out_obj),
            export_selected_objects=True,
            export_uv=False,
            export_normals=True,
            export_materials=False,
            apply_modifiers=True,
        )
        spike.done(path=out_obj)
    except Exception as exc:
        spike.fail(exc)
        return 1

    # --- 6. renders (generated + reference for side-by-side) ----------------
    if do_render:
        spike.step("render")
        try:
            paths = render_silhouettes(obj, out_dir, "generated")
            # Import the reference into the same scene space and render it too.
            before = set(bpy.data.objects)
            bpy.ops.wm.obj_import(filepath=os.path.abspath(ref))
            ref_meshes = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
            ref_paths = []
            if ref_meshes:
                ref_obj = ref_meshes[0]
                ref_stats = mesh_stats(ref_obj)
                spike.report["reference_stats"] = ref_stats
                # Hide the generated mesh so only the reference renders.
                obj.hide_render = True
                ref_paths = render_silhouettes(ref_obj, out_dir, "reference")
                obj.hide_render = False
            spike.done(generated=len(paths), reference=len(ref_paths))
        except Exception as exc:
            spike.fail(exc)
            # Renders are non-essential; keep going to the verdict.

    # --- verdict ------------------------------------------------------------
    pure_quad = quad_stats["quad_ratio"] == 1.0 and quad_stats["tris"] == 0 and quad_stats["ngons"] == 0
    in_band = 0.7 * target_faces <= quad_stats["faces"] <= 1.3 * target_faces
    spike.report["verdict"] = {
        "quadriflow_ran": True,
        "pure_quad": pure_quad,
        "face_count_in_band": in_band,
        "proxy_faces": proxy_stats["faces"],
        "quad_faces": quad_stats["faces"],
        "quad_ratio": quad_stats["quad_ratio"],
        "feasible": bool(pure_quad and in_band),
        "note": (
            "QuadriFlow produced pure quads near target — single-stage path viable."
            if (pure_quad and in_band) else
            "QuadriFlow output not clean at this target — see §6 decision gate "
            "(two-stage 25k->5.8k or denser proxy)."
        ),
    }
    spike.report["status"] = "ok" if spike.report.get("status") == "running" else spike.report["status"]
    spike.flush()

    print("\n[P0] ===== VERDICT =====", flush=True)
    print(json.dumps(spike.report["verdict"], indent=2), flush=True)
    print(f"[P0] report -> {os.path.join(out_dir, 'spike_report.json')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
