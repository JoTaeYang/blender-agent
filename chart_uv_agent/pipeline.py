"""Phase U4 — chart-UV pipeline orchestration + refinement loop (chart-UV plan §8).

U1 segments → U2 unwraps + packs → U4 gates; on a hard-gate miss the loop refines and
retries (max ~6 rounds, monotonic-best like A4): flipped charts are re-split, the
worst-stretch chart is split, a packing miss is retuned then (if needed) the largest
chart split. No silent shipping — a hard-gate failure after the loop returns the best
attempt marked ``failed``. The Smart-UV fallback is never produced (hard gate).

The segmentation/split decisions are pure (U1); the unwrap/pack/measure steps are
Blender. ``run_chart_uv`` runs inside Blender; ``_chart_metrics`` and the refinement
helpers are import-safe so they can be unit-tested without ``bpy``.
"""

from __future__ import annotations

from chart_uv_agent.gate import ChartGateConfig, evaluate_chart_gate
from chart_uv_agent.segmentation import flood_charts, segment, split_chart
from uv_agent.geometry.evaluation import (
    estimate_vt_count, evaluate_uv_solution, per_face_stretch,
    raster_overlap_diagnosis, relative_small_island_ratio, uv_bounds_ok,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


def _chart_metrics(mesh: MeshGraph, uvmap: UVMap, evaluation, *, fallback_used: bool = False) -> dict:
    """Flat metric dict for :func:`evaluate_chart_gate`."""
    vt = estimate_vt_count(mesh, uvmap)
    return {
        "overlap_ratio": evaluation.overlap_ratio,
        "stretch_score": evaluation.stretch_score,
        "packing_efficiency": evaluation.packing_efficiency,
        "island_count": evaluation.island_count,
        "small_island_ratio": evaluation.small_island_ratio,
        "texel_density_variance": evaluation.texel_density_variance,
        "vt_v_ratio": vt / max(1, mesh.vertex_count),
        "uv_bounds_ok": uv_bounds_ok(uvmap),
        "fallback_used": fallback_used,
        "vt_count": vt,
    }


def _charts(mesh: MeshGraph, seams: set[int]) -> tuple[list[list[int]], dict[int, int]]:
    charts = flood_charts(mesh, seams)
    face_chart = {f: cid for cid, fs in enumerate(charts) for f in fs}
    return charts, face_chart


def _split_flipped_charts(mesh, seams, face_chart, charts, flipped) -> set[int]:
    """Split every chart that contains a flipped face (U2.2)."""
    new: set[int] = set()
    for cid in {face_chart[f] for f in flipped if f in face_chart}:
        _, _, ns = split_chart(mesh, charts[cid], seams)
        new.update(ns)
    return new


def _worst_stretch_chart(mesh, charts, face_stretch):
    return max(charts, key=lambda fs: sum(face_stretch[f] * mesh.faces[f].area_3d for f in fs))


def run_chart_uv(obj, mesh: MeshGraph, *, config: ChartGateConfig | None = None,
                 cone_limit: float = 150.0, max_rounds: int = 6, margin: float = 0.005) -> dict:
    """Run U1→U4 on ``obj`` (in Blender). Returns the gate, metrics, seam set, chart
    count, and per-round history; leaves the object holding the best layout."""
    from chart_uv_agent.unwrap import (
        flipped_faces, island_plan_from_seams, read_uvmap, repack, unwrap_and_pack,
    )

    from chart_uv_agent.segmentation import flood_charts, mandatory_seam_edges
    from chart_uv_agent.shape import measure_charts
    from chart_uv_agent.shape_repair import repair_shapes, tail_round

    config = config or ChartGateConfig()
    seg = segment(mesh, cone_limit=cone_limit, max_charts=config.island_count_max)
    seams = set(seg.seams)
    # U1.6 chart shape repair (concavity split → compact convex charts), before U2.
    repair = repair_shapes(mesh, seams, convexity_min=0.92, max_charts=config.island_count_max)
    # U1.7 FINAL tail round — fix the worst-decile charts or prove them stuck. Runs BEFORE
    # the unwrap loop so its merges can't re-introduce ABF flips (overlap, the no-escape
    # hard gate, stays 0; the loop's flip-resplit may re-lower convex_p10, which is what
    # the shippable-with-stuck rule §5c covers).
    tail = tail_round(mesh, seams, convexity_bar=config.convexity_p10_min,
                      max_charts=config.island_count_max)
    stuck_charts = tail["stuck"]
    mandatory = mandatory_seam_edges(mesh, fold_angle=90.0)
    history: list[dict] = [{"stage": "segment", **seg.history[-1]},
                           {"stage": "shape_repair", "rounds": repair["history"]},
                           {"stage": "tail_round", "rounds": tail["history"], "stuck": tail["stuck"]}]
    best = None
    repacked = False
    raster_margin_bumped = False
    bbox_packed = False

    for rnd in range(max_rounds):
        unwrap_and_pack(obj, seams, margin=margin)
        uvmap = read_uvmap(obj, mesh)
        plan = island_plan_from_seams(mesh, seams)
        ev = evaluate_uv_solution(mesh, plan, uvmap)
        charts, face_chart = _charts(mesh, seams)
        metrics = _chart_metrics(mesh, uvmap, ev)
        metrics["small_island_ratio"] = relative_small_island_ratio(mesh, plan, uvmap)
        metrics.update(_shape_metrics(mesh, seams, mandatory))
        # TRUE raster overlap (correctness round) + per-chart attribution for repair.
        diag = raster_overlap_diagnosis(mesh, uvmap, face_chart)
        metrics["raster_overlap_ratio"] = diag["raster_overlap_ratio"]
        gate = evaluate_chart_gate(metrics, config=config)
        history.append({"round": rnd, "charts": ev.island_count, "stretch": round(ev.stretch_score, 4),
                        "overlap": round(ev.overlap_ratio, 5), "raster": diag["raster_overlap_ratio"],
                        "self": diag["self_overlap_ratio"], "cross": diag["cross_overlap_ratio"],
                        "packing": round(ev.packing_efficiency, 4),
                        "verdict": gate.verdict, "fails": [c.name for c in gate.failures]})
        if best is None or _better(metrics, gate, best):
            best = {"seams": set(seams), "metrics": metrics, "gate": gate, "ev": ev}
        if gate.passed:
            break

        changed = False

        flips = flipped_faces(mesh, uvmap)
        if flips:
            ns = _split_flipped_charts(mesh, seams, face_chart, charts, flips)
            if ns:
                seams.update(ns); changed = True

        # Raster-overlap repair (correctness round): (b) inter-chart invasion → margin
        # bump then BOUNDING_BOX (AABB) re-pack; (a) self-intersection → split the folding
        # charts (the per-chart minimize-stretch is already applied inside unwrap_and_pack,
        # so a still-folding chart is split — the flip-resplit path, raster-triggered).
        if not changed and metrics["raster_overlap_ratio"] > config.raster_overlap_max:
            if diag["cross_charts"] and not bbox_packed:
                if not raster_margin_bumped:
                    repack(obj, margin=min(0.05, margin * 4)); raster_margin_bumped = True
                else:
                    repack(obj, margin=margin, pack_shape="AABB"); bbox_packed = True
                changed = True
            else:
                cap = config.island_count_max
                for cid in diag["self_charts"]:
                    if cid < len(charts) and len(charts[cid]) >= 2 * 5 \
                            and len(_charts(mesh, seams)[0]) < cap:
                        _, _, ns = split_chart(mesh, charts[cid], seams)
                        if ns:
                            seams.update(ns); changed = True

        if not changed and metrics["stretch_score"] > config.stretch_max and len(charts) < config.island_count_max:
            fstr = per_face_stretch(mesh, uvmap)
            _, _, ns = split_chart(mesh, _worst_stretch_chart(mesh, charts, fstr), seams)
            if ns:
                seams.update(ns); changed = True

        if not changed and metrics["packing_efficiency"] < config.packing_min and not repacked:
            # U3.2 retune: one tighter-margin re-pack. (Splitting charts to chase packing
            # is counterproductive — more, smaller blobby charts pack *worse* — so we do
            # NOT split here; packing is shape-limited, see the engine report.)
            repack(obj, margin=margin * 0.5)
            repacked = True
            changed = True  # re-measure next round without changing seams

        if not changed:
            break

    # Correctness round: eliminate TRUE (raster) overlap. Owns the final UV; may raise the
    # chart count / stretch (CONFORMAL) — reported as regression. Pre-correctness metrics
    # are captured for the before/after table.
    final_seams = set(best["seams"])
    pre = dict(best["metrics"])
    correctness = correctness_pass(obj, mesh, final_seams, config, margin=margin)

    uvmap = read_uvmap(obj, mesh)
    plan = island_plan_from_seams(mesh, final_seams)
    ev = evaluate_uv_solution(mesh, plan, uvmap)
    metrics = _chart_metrics(mesh, uvmap, ev)
    metrics["small_island_ratio"] = relative_small_island_ratio(mesh, plan, uvmap)
    metrics.update(_shape_metrics(mesh, final_seams, mandatory))
    metrics["raster_overlap_ratio"] = correctness["raster_overlap_ratio"]
    gate = evaluate_chart_gate(metrics, config=config)
    return {
        "engine": "chart", "seams": sorted(final_seams), "chart_count": ev.island_count,
        "metrics": metrics, "gate": gate, "history": history, "rounds": len(history) - 1,
        "shape_repair": repair["history"], "tail_round": tail["history"],
        "correctness": correctness["history"],
        "metrics_before_correctness": {k: pre.get(k) for k in
            ("raster_overlap_ratio", "stretch_score", "packing_efficiency",
             "convexity_p10", "island_count")},
        "stuck_charts": stuck_charts, "shippable": shippable_with_stuck(gate, stuck_charts),
    }


def correctness_pass(obj, mesh, seams, config, *, max_rounds=4, margin=0.005, hard_cap=60):
    """Correctness round (§5d, SLIM-driven). Owns the FINAL UV. The main unwrap already uses
    SLIM (``MINIMUM_STRETCH``, locally injective), so self-folds are gone up front. This
    pass is the safety net: re-SLIM the still-folding charts in isolation, and ONLY a chart
    that still self-overlaps after that (rare boundary self-overlap) is split and re-SLIMed —
    split is the exception, never the driver, so the chart count stays in the cap.
    Cross-invasion (the packer prevents it; 0 in practice) → margin bump + AABB re-pack.
    Mutates ``seams``; returns the per-round history + final diagnosis."""
    from chart_uv_agent.segmentation import flood_charts, split_chart
    from chart_uv_agent.unwrap import read_uvmap, reunwrap_faces, repack, unwrap_and_pack
    from uv_agent.geometry.evaluation import raster_overlap_diagnosis

    def diag():
        uvmap = read_uvmap(obj, mesh)
        fc = {f: i for i, fs in enumerate(flood_charts(mesh, seams)) for f in fs}
        return raster_overlap_diagnosis(mesh, uvmap, fc)

    unwrap_and_pack(obj, seams, margin=margin)             # SLIM
    history: list[dict] = []
    bumped = False
    for rnd in range(max_rounds):
        d = diag()
        history.append({"round": rnd, "raster": d["raster_overlap_ratio"],
                        "self": d["self_overlap_ratio"], "cross": d["cross_overlap_ratio"],
                        "charts": len(flood_charts(mesh, seams)),
                        "action": "ok" if d["raster_overlap_ratio"] <= config.raster_overlap_max else "repair"})
        if d["raster_overlap_ratio"] <= config.raster_overlap_max:
            break
        if d["cross_charts"] and not bumped:               # inter-chart invasion (rare)
            repack(obj, margin=min(0.05, margin * 4), pack_shape="AABB"); bumped = True
            continue
        charts = flood_charts(mesh, seams)
        fold = {f for c in d["self_charts"] if c < len(charts) for f in charts[c]}
        if fold:
            reunwrap_faces(obj, fold, method="MINIMUM_STRETCH", margin=0.001)  # SLIM, isolated
            repack(obj, margin=margin)
        d2 = diag()
        if d2["raster_overlap_ratio"] > config.raster_overlap_max:
            # Exception path: SLIM still leaves a fold (boundary self-overlap) → split it.
            charts = flood_charts(mesh, seams)
            n = len(charts)
            for cid in d2["self_charts"]:
                if cid < len(charts) and len(charts[cid]) >= 10 and n < hard_cap:
                    _, _, ns = split_chart(mesh, charts[cid], seams)
                    if ns:
                        seams.update(ns); n += 1
            unwrap_and_pack(obj, seams, margin=margin)      # SLIM
    final = diag()
    return {"history": history, "final": final, "raster_overlap_ratio": final["raster_overlap_ratio"],
            "splits": len(flood_charts(mesh, seams))}


def _shape_metrics(mesh, seams, mandatory) -> dict:
    """U1.6 shape metrics for the gate (convexity / smoothness / tendrils)."""
    from chart_uv_agent.segmentation import flood_charts
    from chart_uv_agent.shape import measure_charts

    sh = measure_charts(mesh, flood_charts(mesh, set(seams)), set(seams), mandatory)
    return {"convexity_mean": sh["convexity_mean"], "convexity_p10": sh["convexity_p10"],
            "boundary_smoothness_mean": sh["boundary_smoothness_mean"],
            "tendril_count": sh["tendril_count"]}


def shippable_with_stuck(gate, stuck_charts) -> bool:
    """Ship if the gate passes, OR the ONLY hard failure is the U1.7 tail (convexity_p10)
    and the tail loop proved the below-bar charts are stuck (chart-UV plan §5c: this is
    the last shape round — ship what it yields with the stuck report)."""
    if gate.passed:
        return True
    fails = [c.name for c in gate.failures]
    return fails == ["convexity_p10"] and len(stuck_charts) > 0


def _better(metrics, gate, best) -> bool:
    """Prefer a passing gate; among equals, fewer hard fails, then lower TRUE overlap
    (correctness first), then lower stretch."""
    if gate.passed != best["gate"].passed:
        return gate.passed
    if len(gate.failures) != len(best["gate"].failures):
        return len(gate.failures) < len(best["gate"].failures)
    ro, bro = metrics.get("raster_overlap_ratio", 0.0), best["metrics"].get("raster_overlap_ratio", 0.0)
    if abs(ro - bro) > 1e-6:
        return ro < bro
    return metrics["stretch_score"] < best["metrics"]["stretch_score"]
