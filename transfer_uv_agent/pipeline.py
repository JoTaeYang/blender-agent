"""P5 transfer engine orchestration (UV_TRANSFER_PLAN §3). Runs inside Blender.

T1 extract ref charts + BVH → T2 project chart ids (normal/distance guards + cleanup) →
T3 seams + diskify + SLIM unwrap → T4 reference-guided placement + overlap resolution →
T5 hard gates + correspondence report. ``run_transfer_uv`` is the Blender entry point;
the pure cores it calls are unit-tested without ``bpy``.
"""

from __future__ import annotations

import numpy as np

from transfer_uv_agent.gate import (
    correspondence_report, evaluate_transfer_gate, TransferGateConfig,
)
from transfer_uv_agent.placement import resolve_overlaps
from transfer_uv_agent.projection import (
    enforce_connected_components, fill_unassigned, project_chart_ids, smooth_labels,
)
from transfer_uv_agent.reference import extract_reference_charts
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


class NoReferenceUVError(RuntimeError):
    """Raised when the transfer engine is selected but the reference has no UVs — the
    engine fails LOUDLY rather than silently switching to another engine (plan §4)."""


def _mean_edge_length(mesh: MeshGraph) -> float:
    tot = 0.0
    n = 0
    for e in mesh.edges:
        a, b = e.vertex_ids
        tot += float(np.linalg.norm(mesh.vertex_co(a) - mesh.vertex_co(b)))
        n += 1
    return (tot / n) if n else 1.0


def _build_bvh_oracle(ref_mesh: MeshGraph, ref_face_chart: dict[int, int]):
    """A BVHTree-backed k-nearest reference-surface oracle (T1.4 / T2). Triangulates the
    reference faces, builds a BVH, and returns ``query(centroid) -> [(chart_id, dist,
    ref_normal), ...]`` — the same contract the brute oracle gives the unit tests."""
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    verts = [tuple(ref_mesh.vertices[v].co) for v in range(len(ref_mesh.vertices))]
    tris: list[tuple[int, int, int]] = []
    tri_face: list[int] = []
    for f in ref_mesh.faces:
        vids = f.vertex_ids
        for i in range(1, len(vids) - 1):
            tris.append((vids[0], vids[i], vids[i + 1]))
            tri_face.append(f.id)
    bvh = BVHTree.FromPolygons(verts, tris, all_triangles=True)
    normals = {f.id: np.asarray(f.normal, float) for f in ref_mesh.faces}
    radius = 3.0 * _mean_edge_length(ref_mesh)

    def query(centroid):
        co = Vector((float(centroid[0]), float(centroid[1]), float(centroid[2])))
        hits = bvh.find_nearest_range(co, radius)
        if not hits:
            loc, nrm, idx, dist = bvh.find_nearest(co)
            if idx is None:
                return []
            fid = tri_face[idx]
            return [(ref_face_chart.get(fid, 0), float(dist), normals[fid])]
        out = []
        for loc, nrm, idx, dist in hits:
            fid = tri_face[idx]
            out.append((ref_face_chart.get(fid, 0), float(dist), normals[fid]))
        out.sort(key=lambda h: h[1])
        return out

    return query


def _seams_from_labels(mesh: MeshGraph, label: dict[int, int]) -> set[int]:
    """Every edge whose two faces have different chart ids is a seam; boundary /
    non-manifold edges are seams by definition (T3.1)."""
    seams: set[int] = set()
    for e in mesh.edges:
        if e.is_boundary or e.is_non_manifold:
            seams.add(e.id)
        elif len(e.face_ids) == 2:
            a, b = e.face_ids
            if label.get(a) != label.get(b):
                seams.add(e.id)
    return seams


def _diskify(mesh: MeshGraph, seams: set[int], label: dict[int, int]) -> list[dict]:
    """Sever every non-disk chart with the minimal extra cut (T3.2), reusing the chart
    engine's split. New sub-charts inherit their parent's reference id (so they share the
    placement slot). Returns a log of the splits."""
    from chart_uv_agent.segmentation import flood_charts, is_disk, split_chart

    log: list[dict] = []
    for _ in range(mesh.face_count + 1):
        charts = flood_charts(mesh, seams)
        nondisk = [fs for fs in charts if not is_disk(mesh, fs)]
        if not nondisk:
            break
        progressed = False
        for fs in sorted(nondisk, key=len, reverse=True):
            _, _, ns = split_chart(mesh, fs, seams)
            if ns:
                seams.update(ns)
                progressed = True
                log.append({"split_faces": len(fs)})
                break
        if not progressed:
            break
    return log


def run_transfer_uv(low, low_mesh: MeshGraph, ref, ref_mesh: MeshGraph, *,
                    ref_uv_layer: str | None = None,
                    config: TransferGateConfig | None = None,
                    min_dot: float = 0.2, smooth_rounds: int = 10,
                    margin_frac: float = 0.05) -> dict:
    """Transfer ``ref``'s chart layout onto ``low`` (in Blender). Returns the gate,
    HARD-gate metrics, the correspondence report, placements, and the T4 adjustment log.
    Raises :class:`NoReferenceUVError` if the reference has no UVs (fail loud, plan §4)."""
    from chart_uv_agent.unwrap import read_uvmap, unwrap_and_pack
    from chart_uv_agent.segmentation import flood_charts
    from uv_agent.blender.organic_unwrap import read_uvmap as read_artist_uv
    from uv_agent.geometry.evaluation import (
        evaluate_uv_solution, raster_overlap_diagnosis, uv_bounds_ok,
    )
    from chart_uv_agent.unwrap import island_plan_from_seams

    config = config or TransferGateConfig()

    # --- T1: reference charts from the artist UVs (fail loud if absent).
    active = ref.data.uv_layers.get(ref_uv_layer) if ref_uv_layer else ref.data.uv_layers.active
    if active is None or len(ref.data.uv_layers) == 0:
        raise NoReferenceUVError(
            f"reference '{ref.name}' has no UV layer — the transfer engine requires a UV'd "
            "reference. Use --uv-engine chart for the no-reference geometric engine.")
    ref_uv = read_artist_uv(ref, ref_mesh, layer_name=active.name)
    ref_charts = extract_reference_charts(ref_mesh, ref_uv)
    ref_face_chart = {fid: c.chart_id for c in ref_charts for fid in c.face_ids}

    # --- T2: project chart ids + clean.
    oracle = _build_bvh_oracle(ref_mesh, ref_face_chart)
    max_dist = 2.0 * _mean_edge_length(low_mesh)
    label = project_chart_ids(low_mesh, oracle, min_dot=min_dot, max_distance=max_dist)
    n_unassigned = sum(1 for v in label.values() if v < 0)
    label = fill_unassigned(low_mesh, label)
    label = smooth_labels(low_mesh, label, rounds=smooth_rounds)
    label, split_log = enforce_connected_components(low_mesh, label)
    # adaptive label id → reference chart id (split sub-charts inherit their ref slot).
    label_to_ref = {c.chart_id: c.chart_id for c in ref_charts}
    for s in split_log:
        label_to_ref[s["new_id"]] = s["ref_id"]

    # --- T3: seams + diskify + SLIM unwrap.
    seams = _seams_from_labels(low_mesh, label)
    disk_log = _diskify(low_mesh, seams, label)
    unwrap_and_pack(low, seams, method="MINIMUM_STRETCH", margin=0.01)
    uvmap = read_uvmap(low, low_mesh)

    # Map each flooded adaptive chart to a reference chart by the majority projected label
    # of its faces (diskify sub-charts keep the parent's ref id via label_to_ref), then
    # GROUP charts by reference slot. Several adaptive charts (sub-charts from diskify, a
    # major split, or simple over-segmentation) routinely share one reference part; they
    # must share that part's slot, not collide elsewhere.
    from transfer_uv_agent.placement import (
        place_group_density_first,  # noqa: F401 (re-exported for tests)
    )

    flooded = flood_charts(low_mesh, seams)
    ref_by_id = {c.chart_id: c for c in ref_charts}
    adaptive_to_ref: dict[int, int] = {}
    groups: dict[int, list[int]] = {}   # ref_id -> face ids
    charts_for_overlap: list[tuple[int, list[int]]] = []
    for cid, faces in enumerate(flooded):
        votes: dict = {}
        for fid in faces:
            lab = label.get(fid)
            votes[lab] = votes.get(lab, 0) + 1
        lab = max(votes, key=votes.get)
        ref_id = label_to_ref.get(lab, lab)
        if ref_id not in ref_by_id:
            continue
        adaptive_to_ref[cid] = ref_id
        groups.setdefault(ref_id, []).append(list(faces))
    # One (ref_id → all its faces) entry per part for the cross-part overlap check.
    charts_for_overlap = [(rid, [f for fs in fss for f in fs]) for rid, fss in groups.items()]

    # --- T4 round 3 (density = REFERENCE density, then slot placement).
    # Round 2 failed because the SLIM+pack density (sized to fill [0,1]) is much larger
    # than the reference's slot density: slot-placed giant charts ballooned outward under
    # unbounded separation and a global re-fit shrank everything to 2.9% packing. Fix:
    # 1. normalize the GLOBAL texel density to the reference's (one uniform scale) — now
    #    each part's chart has ~its reference slot's UV area by construction;
    # 2. rotation+translation slot placement (unchanged — never a per-part scale);
    # 3. separation only for small residual collisions, with a per-chart displacement cap
    #    (no ballooning); logged local shrink remains the last resort;
    # 4. NO global re-fit. Bounds are enforced by shift; a safety uniform fit may only
    #    engage for scale ≥ 0.9 (anything smaller means the layout is broken — better to
    #    fail the new packing gate loudly than to ship microscopic charts again).
    from transfer_uv_agent.placement import normalize_global_density, place_all_blocks

    # Density target: NOT the reference's UV/3D density — the reference's 51 shells
    # overlap in 3D (double-layered cloth), inflating its 3D area and deflating that
    # ratio (normalizing to it left our UV area at 0.36 of the tile → packing fail).
    # Target instead a tile fill of ~0.55 (above the 0.50 packing bar, under the ~0.62
    # auto-packer ceiling); the mask first-fit + retry-shrink loop absorbs the rest.
    from transfer_uv_agent.reference import chart_area_3d, chart_uv_area

    cur_uv = sum(chart_uv_area(low_mesh, fs, uvmap) for _, fs in charts_for_overlap)
    cur_3d = sum(chart_area_3d(low_mesh, fs) for _, fs in charts_for_overlap)
    target_fill = 0.55
    target_density = (target_fill / cur_3d) if cur_3d > 1e-12 else 0.0
    density_scale = normalize_global_density(low_mesh, uvmap, charts_for_overlap, target_density)
    # T4 final design (round 3, after measured dead-ends): semantic correspondence comes
    # from the transferred SEAMS (T2/T3) — every chart IS a reference part. What proved
    # jointly infeasible with blob charts is exact slot POSITIONS + packing ≥ 0.50 +
    # overlap 0 (slot-anchored variants measured: raster 0.05–0.14 with separation, or
    # packing 0.25–0.37 with overlap-free grid placement — see RESULTS). So: keep the
    # reference ORIENTATION per part (rotation alignment below), then let Blender's
    # shape-aware CONCAVE packer place the islands (rotate=False preserves the aligned
    # orientations; packer guarantees overlap-free, packing ~0.55+).
    from chart_uv_agent.unwrap import repack
    from transfer_uv_agent.placement import place_group_islands

    group_islands = [(rid, fss) for rid, fss in groups.items()]
    rot_info = {rid: place_group_islands(low_mesh, fss, uvmap, ref_by_id[rid],
                                         fit_slot=False)
                for rid, fss in group_islands}
    _write_uvmap(low, low_mesh, uvmap)
    repack(low, margin=4.0 / 1024.0, rotate=False)
    uvmap = read_uvmap(low, low_mesh)
    # Final-UV IoU per part (honest post-pack measurement, not the pre-pack value).
    from transfer_uv_agent.reference import raster_mask as _rmask

    def _final_iou(fss, ref_chart):
        tris = []
        for fs in fss:
            for fid in fs:
                li = low_mesh.faces[fid].loop_indices
                for i in range(1, len(li) - 1):
                    tris.append([uvmap.get(li[0]), uvmap.get(li[i]), uvmap.get(li[i + 1])])
        m = _rmask(tris, bbox=(0.0, 0.0, 1.0, 1.0), resolution=256)
        inter = int((m & ref_chart.abs_footprint).sum())
        union = int((m | ref_chart.abs_footprint).sum())
        return (inter / union) if union else 0.0

    placements = [_GroupPlacement(rid, 1.0, _final_iou(fss, ref_by_id[rid]))
                  for rid, fss in group_islands]
    resolution = {"adjustments": [], "separation_passes": 0,
                  "placement": "rotation-aligned + Blender CONCAVE pack (rotate=False)",
                  "rotations": {int(r): round(rot_info[r]["rotation_deg"], 1)
                                for r in rot_info},
                  "density_scale": round(density_scale, 4),
                  "target_density": round(target_density, 8)}
    pack_fallback = False

    # --- T5: hard gates + report.
    plan = island_plan_from_seams(low_mesh, seams)
    ev = evaluate_uv_solution(low_mesh, plan, uvmap)
    face_chart = {fid: cid for cid, fs in charts_for_overlap for fid in fs}
    diag = raster_overlap_diagnosis(low_mesh, uvmap, face_chart)
    metrics = {
        "raster_overlap_ratio": diag["raster_overlap_ratio"],
        "overlap_ratio": ev.overlap_ratio,
        "uv_bounds_ok": uv_bounds_ok(uvmap),
        "fallback_used": False,  # Smart-UV fallback (forbidden) — never produced
        "stretch_score": ev.stretch_score,
        "texel_density_variance": ev.texel_density_variance,
        "island_count": ev.island_count,
        "packing_efficiency": ev.packing_efficiency,
    }
    gate = evaluate_transfer_gate(metrics, config=config)
    report = correspondence_report(ref_charts, adaptive_to_ref, placements,
                                   ref_count=len(ref_charts))
    return {
        "engine": "transfer", "seams": sorted(seams), "chart_count": ev.island_count,
        "metrics": metrics, "gate": gate, "report": report,
        "placements": [_placement_dict(p) for p in placements],
        "adjustments": resolution["adjustments"], "pack_fallback": pack_fallback,
        "projection": {"unassigned": n_unassigned, "splits": split_log, "diskify": disk_log},
        "shippable": gate.passed,
    }


class _GroupPlacement:
    """One reference part's slot-group placement (T4): the ref id, the uniform scale used
    to fit the locally-packed block into the slot, and the block's IoU with the ref part."""

    def __init__(self, ref_id: int, scale: float, iou: float):
        self.chart_id = ref_id
        self.ref_id = ref_id
        self.scale = scale
        self.iou = iou


def _placement_dict(p) -> dict:
    return {"ref": p.ref_id, "scale": round(p.scale, 5), "iou": round(p.iou, 4)}


def _write_uvmap(obj, mesh: MeshGraph, uvmap: UVMap, *, layer_name: str = "AI_UV") -> None:
    """Write a :class:`UVMap` back to the object's active UV layer (loop order matches
    :func:`extract_mesh_graph`)."""
    layer = obj.data.uv_layers.get(layer_name) or obj.data.uv_layers.active
    flat = np.asarray(uvmap.uv[: len(layer.data)], dtype=np.float64).reshape(-1)
    layer.data.foreach_set("uv", flat)
    obj.data.update()
