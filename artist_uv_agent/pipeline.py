"""A0–A7 orchestration (AUTO_ARTIST_UV_PLAN §5). Runs inside Blender.

A1 segment parts → A2 descriptors → A3 classify → A4 seam templates → A5 SLIM unwrap →
A6 layout grammar (band/group shelf pack) → A7 density report → hard + quality gate +
artist-style report. ``run_artist_uv`` is the Blender entry point; ``compute_artist_metrics``,
``build_artist_parts_json`` and ``build_artist_layout_json`` are pure (Blender-free) so the
gate wiring and output schema are unit-tested without ``bpy``.

No engine ships the Smart-UV fallback (hard gate). The layout packer is overlap-free by
construction, so the SLIM unwrap + grammar pack normally needs no correctness repair; a
residual raster-overlap (a SLIM boundary self-touch) triggers a single reported repack,
never a silent Smart-UV fallback.
"""

from __future__ import annotations

import numpy as np

from artist_uv_agent.classification import classify_parts
from artist_uv_agent.density import density_report, density_weights
from artist_uv_agent.descriptors import describe_parts, quiet_fp
from artist_uv_agent.gate import ArtistGateConfig, artist_report, evaluate_artist_gate
from artist_uv_agent.seams import SeamResult, part_seams
from artist_uv_agent.segmentation import segment_parts, split_branched_parts
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


# Cylinder rectangularity bars (plan §5.A4 / user correction: a cylinder must unwrap to a
# rectangular strip, not a blob/fragment).
LONG_CYL_ELONG = 4.0      # a tube this elongated MUST flatten to a long rectangle
CYL_MIN_ASPECT = 3.0      # … its body island's UV aspect ratio must reach this
CYL_MIN_FILL = 0.45       # … and fill its bbox (a blob has low fill)
CYL_MAX_CHARTS = 6        # more charts than this ⇒ fragmented ("조각")


def _island_aspect_fill(mesh: MeshGraph, faces, uvmap: UVMap) -> tuple[float, float]:
    """UV island aspect ratio (long/short bbox side) and fill (island area / bbox area).
    A rectangle has high fill (~1) and aspect ≈ the tube's length/circumference; a blob has
    low fill and aspect ≈ 1."""
    import numpy as np
    pts = np.array([uvmap.get(li) for f in faces for li in mesh.faces[f].loop_indices])
    if len(pts) == 0:
        return 1.0, 0.0
    w, h = pts.max(0) - pts.min(0)
    area = 0.0
    for f in faces:
        li = mesh.faces[f].loop_indices
        for i in range(1, len(li) - 1):
            a, b, c = uvmap.get(li[0]), uvmap.get(li[i]), uvmap.get(li[i + 1])
            area += abs(0.5 * ((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])))
    fill = area / max(float(w) * float(h), 1e-12)
    aspect = max(float(w), float(h)) / max(min(float(w), float(h)), 1e-12)
    return aspect, fill


def cylinder_quality(mesh: MeshGraph, uvmap: UVMap, seam: SeamResult, descriptors,
                     classes) -> tuple[int, list[dict]]:
    """Per-cylinder rectangularity audit. A cylinder part is a BLOB/FRAGMENT failure when it
    is fragmented (> ``CYL_MAX_CHARTS`` charts) OR it is a long tube (3D elongation ≥
    ``LONG_CYL_ELONG``) whose body island is not rectangular (aspect < ``CYL_MIN_ASPECT`` or
    fill < ``CYL_MIN_FILL``). Returns ``(blob_count, per_cylinder_detail)``."""
    from collections import defaultdict
    from chart_uv_agent.segmentation import flood_charts

    desc_by = {d.part_id: d for d in descriptors}
    class_by = {c.part_id: c for c in classes}
    charts = flood_charts(mesh, seam.seams)
    pcharts: dict[int, list] = defaultdict(list)
    for cid, fs in enumerate(charts):
        pcharts[seam.chart_to_part[cid]].append(fs)

    blob = 0
    details: list[dict] = []
    for pid, fss in pcharts.items():
        if class_by[pid].type != "cylinder":
            continue
        d = desc_by[pid]
        body = max(fss, key=len)
        aspect, fill = _island_aspect_fill(mesh, body, uvmap)
        is_blob = (len(fss) > CYL_MAX_CHARTS) or (
            d.elongation >= LONG_CYL_ELONG and (aspect < CYL_MIN_ASPECT or fill < CYL_MIN_FILL))
        blob += int(is_blob)
        details.append({"part": int(pid), "charts": len(fss), "elongation": round(d.elongation, 2),
                        "body_aspect": round(aspect, 2), "body_fill": round(fill, 3),
                        "blob": bool(is_blob)})
    return blob, details


@quiet_fp
def compute_artist_metrics(mesh: MeshGraph, uvmap: UVMap, seam: SeamResult,
                           classes, descriptors=None) -> dict:
    """Flat gate-metric dict from a final ``uvmap`` (pure — no Blender). Mirrors the chart
    engine's metric set plus the artist min-island-size and cylinder-rectangularity inputs.
    ``descriptors`` enables the cylinder audit (a long tube that stays a blob is a fail)."""
    from chart_uv_agent.segmentation import flood_charts, mandatory_seam_edges
    from chart_uv_agent.shape import measure_charts
    from uv_agent.blender.organic_unwrap import island_plan_from_seams
    from uv_agent.geometry.evaluation import (
        estimate_vt_count, evaluate_uv_solution, raster_overlap_diagnosis,
        relative_small_island_ratio, uv_bounds_ok,
    )

    charts = flood_charts(mesh, seam.seams)
    plan = island_plan_from_seams(mesh, seam.seams)
    ev = evaluate_uv_solution(mesh, plan, uvmap)
    face_chart = {f: cid for cid, fs in enumerate(charts) for f in fs}
    diag = raster_overlap_diagnosis(mesh, uvmap, face_chart)
    mandatory = mandatory_seam_edges(mesh, fold_angle=90.0)
    sh = measure_charts(mesh, charts, set(seam.seams), mandatory)
    vt = estimate_vt_count(mesh, uvmap)

    # smallest NON-detail chart (a sub-floor non-detail island is a hard miss, plan §6).
    nondetail = [len(fs) for cid, fs in enumerate(charts)
                 if seam.chart_role.get(cid) not in ("detail", "cap")]
    min_nondetail = min(nondetail) if nondetail else 999

    cyl_blob, cyl_detail = (cylinder_quality(mesh, uvmap, seam, descriptors, classes)
                            if descriptors is not None else (0, []))

    return {
        "overlap_ratio": ev.overlap_ratio,
        "raster_overlap_ratio": diag["raster_overlap_ratio"],
        "stretch_score": ev.stretch_score,
        "packing_efficiency": ev.packing_efficiency,
        "texel_density_variance": ev.texel_density_variance,
        "island_count": ev.island_count,
        "small_island_ratio": relative_small_island_ratio(mesh, plan, uvmap),
        "vt_v_ratio": vt / max(1, mesh.vertex_count),
        "vt_count": vt,
        "uv_bounds_ok": uv_bounds_ok(uvmap),
        "fallback_used": False,
        "convexity_mean": sh["convexity_mean"],
        "convexity_p10": sh["convexity_p10"],
        "boundary_smoothness_mean": sh["boundary_smoothness_mean"],
        "tendril_count": sh["tendril_count"],
        "min_nondetail_island_faces": min_nondetail,
        "cylinder_blob_count": cyl_blob,
        "cylinder_detail": cyl_detail,
    }


def build_artist_parts_json(parts, descriptors, classes, seam: SeamResult,
                            segmentation_history: list) -> dict:
    """``artist_parts.json`` content (plan §7): the per-part table + the seam repair
    history + segmentation history. Pure."""
    from artist_uv_agent.debug import part_debug_rows

    return {
        "engine": "artist",
        "part_count": len(parts),
        "parts": part_debug_rows(parts, descriptors, classes, seam),
        "seam_repair_history": seam.repair_log,
        "chart_to_part": {int(k): int(v) for k, v in seam.chart_to_part.items()},
        "chart_role": {int(k): v for k, v in seam.chart_role.items()},
        "cap_exceeded": seam.cap_exceeded,
        "segmentation_history": segmentation_history,
    }


def build_artist_layout_json(layout_meta: dict, report: dict) -> dict:
    """``artist_layout.json`` content (plan §7): the REPORT-ONLY layout metadata (intended
    part grouping / bands + measured orientation/symmetry/density) and the artist report.
    No forced per-chart UV transforms — the final layout is the Blender CONCAVE pack. Pure."""
    return {"engine": "artist", "layout": layout_meta, "report": report}


def _write_uvmap(obj, mesh: MeshGraph, uvmap: UVMap, *, layer_name: str = "AI_UV") -> None:
    layer = obj.data.uv_layers.get(layer_name) or obj.data.uv_layers.active
    flat = np.asarray(uvmap.uv[: len(layer.data)], dtype=np.float64).reshape(-1)
    layer.data.foreach_set("uv", flat)
    obj.data.update()


def run_artist_uv(obj, mesh: MeshGraph, *, config: ArtistGateConfig | None = None,
                  back_dir=None, importance: bool = False, margin: float = 0.005) -> dict:
    """Run the reduced-v1 artist pipeline on ``obj`` (in Blender):

        A1 segment → A2 descriptors → A3 classify → A4 seam templates
        → A5 SLIM unwrap + average-island-scale + **Blender CONCAVE pack** (the final
          layout; shape-aware, usable packing, uniform checker)
        → orient long islands to a consistent axis, kept ONLY if packing stays acceptable
          (orientation is the lowest priority: overlap > packing > checker > grouping >
          orientation)
        → part grouping / bands as REPORT-ONLY metadata (validated, never forced onto UVs)
        → hard + quality gate + part-colour debug.

    The earlier band/shelf BBOX packer wrecked UV space (packing 0.24); the final layout is
    now Blender's CONCAVE packer. Returns the gate, metrics, report, parts/layout JSON, and
    the flooded charts; leaves ``obj`` holding the final layout."""
    from chart_uv_agent.unwrap import read_uvmap, repack, unwrap_and_pack
    from chart_uv_agent.segmentation import flood_charts
    from artist_uv_agent.layout import layout_metadata, orient_long_islands

    config = config or ArtistGateConfig()

    # A1–A4: parts → branch-split tube forks (shaft/tine) → descriptors → classes → seams.
    seg = segment_parts(mesh)
    seg = split_branched_parts(mesh, seg)
    descriptors = describe_parts(mesh, seg)
    neighbors = {p.part_id: p.neighbors for p in seg.parts}
    classes = classify_parts(descriptors, neighbors)
    seam = part_seams(mesh, seg, descriptors, classes, back_dir=back_dir)
    charts = flood_charts(mesh, seam.seams)

    # A5: SLIM unwrap + average island scale + CONCAVE pack (uniform density, shape-aware,
    # rotate=True for best packing). This IS the baseline final layout.
    unwrap_and_pack(obj, seam.seams, method="MINIMUM_STRETCH", margin=margin)
    uv_base = read_uvmap(obj, mesh)
    m_base = compute_artist_metrics(mesh, uv_base, seam, classes, descriptors)

    # Orientation pass (lowest priority): rotate long islands to vertical, then CONCAVE
    # re-pack WITHOUT rotation so it survives. Keep it only if packing stays within reach of
    # the rotation-free penalty AND clears the floor and stays overlap-free; else fall back
    # to the best (rotate=True) pack.
    uv_or = orient_long_islands(mesh, uv_base, charts, descriptors, classes, seam)
    _write_uvmap(obj, mesh, uv_or)
    repack(obj, margin=margin, pack_shape="CONCAVE", rotate=False)
    m_or = compute_artist_metrics(mesh, read_uvmap(obj, mesh), seam, classes, descriptors)
    use_oriented = (m_or["packing_efficiency"] >= max(config.packing_min, 0.9 * m_base["packing_efficiency"])
                    and m_or["raster_overlap_ratio"] <= config.raster_overlap_max
                    and m_or["overlap_ratio"] <= config.overlap_max)
    if use_oriented:
        metrics, final_uv = m_or, read_uvmap(obj, mesh)
    else:
        _write_uvmap(obj, mesh, uv_base)   # restore the best (rotate=True) pack
        metrics, final_uv = m_base, uv_base

    gate = evaluate_artist_gate(metrics, config=config)

    # A6/A7 REPORT-ONLY: grouping/band metadata + measured orientation/symmetry/density.
    weights = density_weights(descriptors, classes, importance=importance)
    lmeta, per_part_density = layout_metadata(mesh, final_uv, seam, descriptors, classes, neighbors)
    drep = density_report(per_part_density, weights)
    report = artist_report(lmeta, seam, classes, drep)
    report["orientation_applied"] = use_oriented

    parts_json = build_artist_parts_json(seg.parts, descriptors, classes, seam, seg.history)
    layout_json = build_artist_layout_json(lmeta, report)

    return {
        "engine": "artist", "seams": sorted(seam.seams), "chart_count": len(charts),
        "part_count": len(seg.parts), "metrics": metrics, "gate": gate,
        "gate_config": config.to_dict(), "report": report,
        "parts_json": parts_json, "layout_json": layout_json,
        "orientation_applied": use_oriented, "charts": charts, "seam_result": seam,
        "shippable": gate.passed,
    }
