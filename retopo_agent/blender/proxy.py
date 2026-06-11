"""Phase P1 — scalable ingest + manifold proxy build (quad-retopo plan §7).

This is the *real* (non-throwaway) successor to the P0 spike. It turns the 1.86 GB
/ 24.9M-face ZBrush export into a watertight, strictly-manifold proxy of
~500k–1.5M faces that is QuadriFlow's input contract for P2, persists it as a
small ``proxy.blend``, and frees the original from RAM — every later phase opens
``proxy.blend`` and never touches the giant OBJ again.

Pipeline (plan §7, with the P0 corrections baked in):

    import (wm.obj_import)           — seconds, not hours; one C++ import only
    source summary                   — cheap numpy face-type stats on the giant mesh
    proxy build                      — voxel remesh applied DIRECTLY to the import,
                                       voxel size found by the search_voxel_size loop
                                       (P0: Decimate-Collapse-first is OOM-only, NOT a
                                       speed step — it ran 42+ min single-threaded)
    manifold check                   — bmesh non-manifold / boundary count on the proxy
    fidelity                         — BVH(proxy) vs sampled original; bounds the error
                                       budget of everything downstream (run BEFORE the
                                       original is discarded)

Memory discipline (plan §7): one heavy object at a time. The voxel search remeshes
*working duplicates* of the source so the search can retry; the duplicate is
24.9M faces only transiently (voxel_remesh replaces it with a small mesh), and
prior candidates are pruned. ``bpy`` is imported lazily so the pure helpers
(:func:`estimate_initial_voxel_size`, band logic) are unit-testable without Blender.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from retopo_agent.geometry.target_search import (
    quality_band,
    search_voxel_size,
    target_error_ratio,
)

# Proxy face-count band (plan §7.3): a strictly-manifold proxy of 500k–1.5M faces.
PROXY_FACE_MIN = 500_000
PROXY_FACE_MAX = 1_500_000
PROXY_FACE_TARGET = 1_000_000


@dataclass
class ProxyResult:
    """Outcome of :func:`build_proxy` — the proxy object plus how it was reached."""

    obj: object  # the proxy bpy object (voxel-remeshed, manifold)
    source_face_count: int
    target_face_count: int
    voxel_size: float
    initial_voxel_size: float
    search_iterations: int
    search_history: list = field(default_factory=list)  # [(voxel, faces), ...]
    notes: list[str] = field(default_factory=list)

    @property
    def proxy_face_count(self) -> int:
        return len(self.obj.data.polygons)

    @property
    def target_error_ratio(self) -> float:
        return target_error_ratio(self.proxy_face_count, self.target_face_count)

    @property
    def band(self) -> str:
        return quality_band(self.proxy_face_count, self.target_face_count)

    def to_dict(self) -> dict:
        return {
            "source_face_count": self.source_face_count,
            "proxy_face_count": self.proxy_face_count,
            "target_face_count": self.target_face_count,
            "voxel_size": round(self.voxel_size, 6),
            "initial_voxel_size": round(self.initial_voxel_size, 6),
            "target_error_ratio": round(self.target_error_ratio, 4),
            "band": self.band,
            "search_iterations": self.search_iterations,
            "search_history": [[round(v, 6), f] for v, f in self.search_history],
            "notes": self.notes,
        }


# --------------------------------------------------------------------- pure helpers

def estimate_initial_voxel_size(total_area: float, target_faces: int, bbox_diagonal: float) -> float:
    """First-guess voxel size for a voxel remesh that should land near
    ``target_faces`` (pure; unit-testable without Blender).

    Voxel remesh emits roughly one quad per voxel-sized patch of surface, so
    ``faces ≈ surface_area / voxel_size²`` (the same area/voxel² law the
    :func:`~retopo_agent.geometry.target_search.search_voxel_size` loop exploits).
    Inverting gives ``voxel ≈ sqrt(area / target)``. The P0 spike confirmed the
    constant is ≈ 1 on the statue: voxel 0.974 → 383,362 faces ⇒ implied area
    ≈ 3.64e5, and ``sqrt(3.64e5 / 1e6) = 0.603`` is exactly the divisor (≈ diag/969)
    that the spike's §6 note predicted for a ~1M proxy.

    Clamped to ``[diag/4000, diag/100]`` so a degenerate area estimate can't request
    an absurd voxel size. Falls back to ``diag/600`` (the plan's original starting
    point) when the area is unusable.
    """
    lo = bbox_diagonal / 4000.0 if bbox_diagonal > 0 else 1e-4
    hi = bbox_diagonal / 100.0 if bbox_diagonal > 0 else 1.0
    if total_area > 0 and target_faces > 0:
        guess = math.sqrt(total_area / target_faces)
    elif bbox_diagonal > 0:
        guess = bbox_diagonal / 600.0
    else:
        guess = 1.0
    return min(max(guess, lo), hi)


# ------------------------------------------------------------------------- import

def import_source(filepath: str):
    """Import an OBJ with the C++ importer and return ``(obj, seconds)``.

    Joins multiple components into one object (a ZBrush export can be multi-shell;
    voxel remesh will fuse them anyway — plan §7.2). Raises on a missing file or an
    import that produced no mesh.
    """
    import os

    import bpy

    if not os.path.exists(filepath):
        raise FileNotFoundError(filepath)
    t0 = time.monotonic()
    before = set(bpy.data.objects)
    bpy.ops.wm.obj_import(filepath=os.path.abspath(filepath))
    meshes = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
    if not meshes:
        raise RuntimeError(f"no mesh imported from {filepath}")
    if len(meshes) > 1:
        _make_only_active(meshes[0])
        for m in meshes:
            m.select_set(True)
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active or meshes[0]
    return obj, time.monotonic() - t0


def source_summary(obj) -> dict:
    """Cheap face-type / size stats on the (possibly 24.9M-face) source.

    Uses ``foreach_get`` so it stays C-fast and never builds a Python mesh graph
    (which would be far too heavy at this density — cf. the 2M-face cap in
    :func:`retopo_agent.blender.diagnosis.diagnose_decimation_blender`). Component
    / non-manifold structure of the *source* is intentionally NOT computed here:
    voxel remesh fuses components and produces its own manifold, so the structural
    contract is checked on the *proxy* instead (plan §7.2 / :func:`manifold_check`).
    """
    import numpy as np

    mesh = obj.data
    n = len(mesh.polygons)
    loop_totals = np.empty(n, dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", loop_totals)
    tris = int(np.count_nonzero(loop_totals == 3))
    quads = int(np.count_nonzero(loop_totals == 4))
    ngons = int(n - tris - quads)

    areas = np.empty(n, dtype=np.float64)
    mesh.polygons.foreach_get("area", areas)
    total_area = float(areas.sum())

    # Degenerate faces: near-zero area relative to the mesh scale (cheap, no bmesh).
    diag = _world_bbox_diagonal(obj)
    degenerate_eps = 1e-10 * (diag * diag) if diag > 0 else 1e-12
    degenerate = int(np.count_nonzero(areas <= degenerate_eps))

    return {
        "verts": len(mesh.vertices),
        "faces": n,
        "tris": tris,
        "quads": quads,
        "ngons": ngons,
        "quad_ratio": round(quads / n, 4) if n else 0.0,
        "degenerate_faces": degenerate,
        "total_surface_area": round(total_area, 4),
        "bbox_diagonal": round(diag, 4),
    }


def source_diagnosis(obj, *, tiny_face_fraction: float = 0.001) -> dict:
    """Topology diagnosis of the *original* source (plan §7.2): component count,
    non-manifold / boundary edges, and how many components are tiny detached shells.

    This is the backfill §7.2 asks for: a ZBrush export can carry small detached
    fragments that voxel remesh silently drops, and that loss — not just sub-voxel
    smoothing — can be the real driver of the proxy↔original max distance. Recording
    the source's component structure makes that explainable rather than a mystery.

    Built with a single bmesh pass (one loop computes non-manifold, boundary, and the
    union-find for components together), guarded so a memory failure on the 24.9M-face
    source degrades to a note instead of crashing the job. Runs while the source is
    still loaded, before the proxy replaces it.
    """
    try:
        topo = _bmesh_topology(obj)
    except (MemoryError, RuntimeError) as exc:  # noqa: BLE001 - degrade, don't crash
        return {"error": f"source diagnosis skipped ({type(exc).__name__}: {exc})"}

    sizes = sorted(topo["component_sizes"], reverse=True)
    total = sum(sizes) or 1
    tiny_threshold = max(1, int(tiny_face_fraction * total))
    tiny = [s for s in sizes if s < tiny_threshold]
    return {
        "components": len(sizes),
        "largest_component_faces": sizes[0] if sizes else 0,
        "largest_component_ratio": round(sizes[0] / total, 4) if sizes else 0.0,
        "smallest_component_faces": sizes[-1] if sizes else 0,
        "tiny_component_count": len(tiny),
        "tiny_component_faces": sum(tiny),
        "tiny_face_fraction": tiny_face_fraction,
        "non_manifold_edges": topo["non_manifold_edges"],
        "boundary_edges": topo["boundary_edges"],
    }


# ------------------------------------------------------------------- proxy build

def build_proxy(
    source_obj,
    *,
    target_faces: int = PROXY_FACE_TARGET,
    total_area: float | None = None,
    max_iter: int = 4,
    tol_ratio: float = 0.15,
) -> ProxyResult:
    """Voxel-remesh ``source_obj`` into a manifold proxy of ~``target_faces`` faces.

    Voxel remesh is applied **directly to the full-resolution import** (P0 finding:
    the Decimate-Collapse pre-step is OOM-only, never a speed step). The voxel
    *size* that lands the face count in band is found by
    :func:`~retopo_agent.geometry.target_search.search_voxel_size`, seeded with the
    area/voxel² estimate (:func:`estimate_initial_voxel_size`) so it usually
    converges in 1–2 iterations.

    Each search probe remeshes a *working duplicate* of the source (voxel_remesh is
    destructive); the duplicate is 24.9M faces only until the remesh replaces it with
    a small mesh, and stale candidates are pruned, so peak memory stays near
    "source + one duplicate". The source object is left intact for the fidelity pass.
    """
    import bpy  # noqa: F401

    source_faces = len(source_obj.data.polygons)
    diag = _world_bbox_diagonal(source_obj)
    if total_area is None:
        import numpy as np

        areas = np.empty(source_faces, dtype=np.float64)
        source_obj.data.polygons.foreach_get("area", areas)
        total_area = float(areas.sum())

    initial = estimate_initial_voxel_size(total_area, target_faces, diag)
    notes: list[str] = [
        f"voxel-direct on {source_faces} faces; initial voxel={initial:.5g} "
        f"(area={total_area:.4g}, diag={diag:.4g})"
    ]

    candidates: dict[float, object] = {}

    def measure(voxel: float) -> int:
        # Prune everything but keep this probe's remesh as a candidate object.
        dup = source_obj.copy()
        dup.data = source_obj.data.copy()
        dup.name = "AI_Proxy_probe"
        dup.data.name = dup.name
        bpy.context.collection.objects.link(dup)
        _make_only_active(dup)
        dup.data.remesh_voxel_size = max(voxel, 1e-6)
        dup.data.remesh_voxel_adaptivity = 0.0
        try:
            bpy.ops.object.voxel_remesh()
        except RuntimeError as exc:
            notes.append(f"voxel_remesh failed at voxel={voxel:.5g}: {exc}")
            bpy.data.objects.remove(dup, do_unlink=True)
            return 0
        faces = len(dup.data.polygons)
        candidates[voxel] = dup
        notes.append(f"probe voxel={voxel:.5g} -> {faces} faces")
        return faces

    search = search_voxel_size(
        measure,
        target_faces,
        initial=initial,
        min_voxel=diag / 4000.0,
        max_voxel=diag / 100.0,
        max_iter=max_iter,
        tol_ratio=tol_ratio,
    )

    # Keep the best candidate as the proxy; drop the rest and purge their meshes.
    proxy = candidates.pop(search.value, None)
    for obj in candidates.values():
        _remove_object(obj)
    candidates.clear()
    if proxy is None:
        raise RuntimeError("voxel remesh produced no usable proxy (all probes failed)")
    proxy.name = "AI_Proxy"
    proxy.data.name = "AI_Proxy"
    _purge_orphans()

    return ProxyResult(
        obj=proxy,
        source_face_count=source_faces,
        target_face_count=target_faces,
        voxel_size=search.value,
        initial_voxel_size=initial,
        search_iterations=search.iterations,
        search_history=search.history,
        notes=notes,
    )


# --------------------------------------------------------------- manifold check

def manifold_check(obj) -> dict:
    """Cheap bmesh topology contract for the proxy (plan §7.2 / QuadriFlow input).

    QuadriFlow needs a watertight manifold; voxel remesh should guarantee it. This
    confirms it directly: counts non-manifold edges, open boundary edges, and
    connected components (via bmesh, fast on a ~1M proxy). ``is_manifold`` is the
    pass/fail the P2 stage depends on.
    """
    topo = _bmesh_topology(obj)
    return {
        "faces": len(obj.data.polygons),
        "non_manifold_edges": topo["non_manifold_edges"],
        "boundary_edges": topo["boundary_edges"],
        "components": len(topo["component_sizes"]),
        "is_manifold": topo["non_manifold_edges"] == 0 and topo["boundary_edges"] == 0,
    }


def _bmesh_topology(obj) -> dict:
    """One bmesh pass → non-manifold edge count, boundary edge count, and per-shell
    face counts. A single loop over the edges does the non-manifold/boundary tallies
    and the union-find together, so even the 24.9M-face source is one pass, not three.
    """
    import bmesh

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.verts.index_update()
        parent = list(range(len(bm.verts)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        non_manifold = boundary = 0
        for e in bm.edges:
            if not e.is_manifold:
                non_manifold += 1
            if e.is_boundary:
                boundary += 1
            a, b = e.verts[0].index, e.verts[1].index
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        sizes: dict[int, int] = {}
        for f in bm.faces:
            root = find(f.verts[0].index)
            sizes[root] = sizes.get(root, 0) + 1
    finally:
        bm.free()
    return {
        "non_manifold_edges": non_manifold,
        "boundary_edges": boundary,
        "component_sizes": list(sizes.values()),
    }


def drop_tiny_components(obj, *, min_face_fraction: float = 0.001) -> dict:
    """Delete detached shells smaller than ``min_face_fraction`` of the total face
    count, keeping every meaningful shell (always at least the largest).

    Voxel remesh of the 52-component ZBrush source leaves the proxy as one watertight
    body plus a stray micro-shell (the 12-vert floater, plan §6.2). That floater is
    a second connected component, which would otherwise force the downstream
    component bound to 2 and let a genuine extra shell slip through unnoticed.
    Removing it here lets A3/A4 assert the tight ``components == 1`` (plan §6.2).

    Returns a report (counts before/after + dropped faces) so P1 can LOG the drop
    rather than silently mutating the proxy. A no-op when there is only one shell.
    """
    import bmesh

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.verts.index_update()
        parent = list(range(len(bm.verts)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for e in bm.edges:
            a, b = e.verts[0].index, e.verts[1].index
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        face_root = {}
        sizes: dict[int, int] = {}
        for f in bm.faces:
            root = find(f.verts[0].index)
            face_root[f.index] = root
            sizes[root] = sizes.get(root, 0) + 1

        total_faces = sum(sizes.values())
        before = len(sizes)
        if before <= 1 or total_faces == 0:
            return {"components_before": before, "components_after": before,
                    "dropped_components": 0, "dropped_faces": 0}

        largest_root = max(sizes, key=lambda r: sizes[r])
        threshold = max(1, int(min_face_fraction * total_faces))
        keep_roots = {r for r, n in sizes.items() if n >= threshold} | {largest_root}
        doomed = [f for f in bm.faces if face_root[f.index] not in keep_roots]
        dropped_faces = len(doomed)
        if doomed:
            bmesh.ops.delete(bm, geom=doomed, context="FACES")
            bm.to_mesh(obj.data)
            obj.data.update()

        after = len(keep_roots)
        return {
            "components_before": before,
            "components_after": after,
            "dropped_components": before - after,
            "dropped_faces": dropped_faces,
            "min_face_fraction": min_face_fraction,
            "threshold_faces": threshold,
        }
    finally:
        bm.free()


# ------------------------------------------------------------------- fidelity

def proxy_fidelity(
    source_obj,
    proxy_obj,
    *,
    max_distance_samples: int = 20000,
    max_normal_samples: int = 8000,
    voxel_size: float | None = None,
):
    """Bound how much detail the proxy lost vs the original (plan §7.5).

    Builds a ``BVHTree`` on the **proxy** (small, cheap) and measures the distance
    from sampled *original* surface points to the proxy — i.e. "how far is the
    original from the proxy", the error budget that bounds everything downstream.
    Deliberately avoids :func:`retopo_agent.blender.shape.evaluate_shape_match_blender`
    here because that builds a full bmesh of *both* objects for a volume ratio,
    which would mean a 24.9M-face bmesh; this samples the original instead and
    reports ``volume_error_ratio=None``. Reuses the shared
    :func:`~retopo_agent.geometry.shape_eval.build_shape_report` so the bands match
    the rest of the pipeline. Must run while the original still exists.
    """
    import bpy  # noqa: F401
    from mathutils.bvhtree import BVHTree

    from retopo_agent.geometry.shape_eval import _folded_angle_deg, build_shape_report

    depsgraph = bpy.context.evaluated_depsgraph_get()
    tree = BVHTree.FromObject(proxy_obj, depsgraph)  # proxy is small -> cheap BVH

    # Both meshes share world space; map original geometry into the proxy's local space.
    to_proxy_local = proxy_obj.matrix_world.inverted_safe() @ source_obj.matrix_world
    normal_mat = to_proxy_local.to_3x3().inverted_safe().transposed()
    src = source_obj.data
    diag = _local_bbox_diagonal(proxy_obj)

    def stride(n: int, k: int):
        if n <= k:
            return range(n)
        step = n / k
        return (int(i * step) for i in range(k))

    distances: list[float] = []
    normal_angles: list[float] = []

    verts = src.vertices
    for vi in stride(len(verts), max_distance_samples):
        loc, nrm, idx, dist = tree.find_nearest(to_proxy_local @ verts[vi].co)
        if dist is not None:
            distances.append(float(dist))

    polys = src.polygons
    for fi in stride(len(polys), max_normal_samples):
        poly = polys[fi]
        loc, nrm, idx, dist = tree.find_nearest(to_proxy_local @ poly.center)
        if dist is None:
            continue
        distances.append(float(dist))
        if nrm is not None:
            src_n = (normal_mat @ poly.normal).normalized()
            normal_angles.append(_folded_angle_deg(tuple(src_n), tuple(nrm)))

    report = build_shape_report(
        bbox_diagonal=diag,
        distances=distances,
        normal_angles_deg=normal_angles,
        volume_error_ratio=None,
    )
    return report, _distance_percentiles(distances, voxel_size=voxel_size)


def _distance_percentiles(distances, *, voxel_size: float | None = None) -> dict:
    """Distribution of the original→proxy distances, to separate *broad* sub-voxel
    smoothing (mean ≫ p99 close together, all a small multiple of the voxel) from a
    *few localized* thin-feature losses (low p50, huge p99/max). ``voxel_multiple``
    expresses the mean in voxel-size units — a pure voxel-approximation error sits
    near ~0.5·voxel; much larger means real high-frequency detail was flattened.
    """
    import numpy as np

    if not distances:
        return {}
    arr = np.asarray(distances, dtype=float)
    p50, p90, p99 = (float(x) for x in np.percentile(arr, [50, 90, 99]))
    out = {
        "p50": round(p50, 4),
        "p90": round(p90, 4),
        "p99": round(p99, 4),
        "max": round(float(arr.max()), 4),
        "mean": round(float(arr.mean()), 4),
    }
    if voxel_size and voxel_size > 0:
        out["voxel_size"] = round(voxel_size, 5)
        out["mean_over_voxel"] = round(out["mean"] / voxel_size, 2)
        out["p99_over_voxel"] = round(out["p99"] / voxel_size, 2)
    return out


# --------------------------------------------------------------------- persistence

def persist_proxy(proxy_obj, source_obj, filepath: str) -> None:
    """Save ``filepath`` (a ``proxy.blend``) containing ONLY the proxy (plan §7.4).

    Deletes the original source and purges orphan datablocks first so the original
    24.9M-face mesh never lands in the saved file and is freed from RAM. After this
    every later phase opens ``proxy.blend`` instead of re-importing the 1.86 GB OBJ.
    """
    import os

    import bpy

    if source_obj is not None:
        _remove_object(source_obj)
    _purge_orphans()
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(filepath))


# ----------------------------------------------------------------------- internals

def _make_only_active(obj) -> None:
    import bpy

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _remove_object(obj) -> None:
    import bpy

    mesh = obj.data if getattr(obj, "type", None) == "MESH" else None
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    except (RuntimeError, ReferenceError):
        pass


def _purge_orphans() -> None:
    import bpy

    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)


def _world_bbox_diagonal(obj) -> float:
    from mathutils import Vector

    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector((min(c[i] for c in corners) for i in range(3)))
    hi = Vector((max(c[i] for c in corners) for i in range(3)))
    return max((hi - lo).length, 1e-9)


def _local_bbox_diagonal(obj) -> float:
    corners = [tuple(c) for c in obj.bound_box]
    dx = max(c[0] for c in corners) - min(c[0] for c in corners)
    dy = max(c[1] for c in corners) - min(c[1] for c in corners)
    dz = max(c[2] for c in corners) - min(c[2] for c in corners)
    return max(math.sqrt(dx * dx + dy * dy + dz * dz), 1e-9)
