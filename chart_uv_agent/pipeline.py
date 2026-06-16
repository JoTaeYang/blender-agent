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

from chart_uv_agent.gate import ChartGateConfig, ChartGateResult, evaluate_chart_gate
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
    return max(charts, key=lambda fs: _chart_distortion(mesh, fs, face_stretch))


def _chart_distortion(mesh, face_ids, face_stretch) -> float:
    """Area-weighted mean per-face stretch over a chart — the checker/stretch distortion
    of one island (MINIMAL_DISTORTION_UV_PLAN §M1). Area-weighting (not a raw sum) makes
    the worst-island pick scale-invariant, so a big low-distortion chart never out-ranks a
    small badly-stretched one."""
    area = sum(mesh.faces[f].area_3d for f in face_ids)
    if area <= 1e-12:
        return 0.0
    return sum(face_stretch[f] * mesh.faces[f].area_3d for f in face_ids) / area


def _distortion_report(mesh, uvmap, charts) -> dict:
    """Checker-distortion summary for the report (MINIMAL_DISTORTION_UV_PLAN §M1). Aliases
    the existing area-stretch metric under checker-distortion names so a reviewer can tie
    the number to the visual checker render. ``charts`` is a list of face-id lists."""
    fstr = per_face_stretch(mesh, uvmap)
    worst_id, worst_val, worst_n = -1, 0.0, 0
    for cid, fs in enumerate(charts):
        d = _chart_distortion(mesh, fs, fstr)
        if d > worst_val:
            worst_id, worst_val, worst_n = cid, d, len(fs)
    # Global checker distortion: same area-weighted mean over all faces (== stretch_score
    # family) so the headline number and the per-island worst share one scale.
    glob = _chart_distortion(mesh, range(len(mesh.faces)), fstr)
    return {"checker_distortion_score": round(float(glob), 6),
            "worst_island_distortion": round(float(worst_val), 6),
            "worst_island_id": worst_id, "worst_face_count": worst_n}


def _audit_metrics(mesh, seams) -> dict:
    """R2 seam-SET audit metrics for the gate (MINIMAL_DISTORTION_UV_PLAN §M2)."""
    from chart_uv_agent.segmentation import mandatory_seam_audit
    a = mandatory_seam_audit(mesh, set(seams), fold_angle=90.0)
    return {"mandatory_90_edges": a["mandatory_90_edges"],
            "mandatory_90_missing": a["mandatory_90_missing"]}


def _uv_audit_metrics(mesh, uvmap) -> dict:
    """R2 UV-LEVEL audit metrics — measured on the actual UVMap, not the seam set, so a
    fold that welds in the exported UV is caught (MINIMAL_DISTORTION_UV_PLAN §M2)."""
    from uv_agent.geometry.evaluation import mandatory_seam_uv_audit
    a = mandatory_seam_uv_audit(mesh, uvmap, fold_angle=90.0)
    return {"mandatory_90_fold_edges": a["mandatory_90_fold_edges"],
            "mandatory_90_uv_unsplit": a["mandatory_90_uv_unsplit"],
            "uv_unsplit_edge_ids": a["uv_unsplit_edge_ids"]}


def run_chart_uv(obj, mesh: MeshGraph, *, config: ChartGateConfig | None = None,
                 cone_limit: float = 150.0, max_rounds: int = 24, margin: float = 0.005,
                 shape_passes: bool = False, forbidden_edges=None, region_policy=None,
                 prune_auxiliary: bool = True, min_improvement_ratio: float = 0.15,
                 user_seam_spec=None, auto_refine_user_seams: bool = False,
                 repair_user_seams: bool = True, enforce_user_mandatory: bool = True,
                 gate_user_mandatory: bool = True, optimize_layout: bool = False,
                 layout_optimization_config=None) -> dict:
    """Run U1→U4 on ``obj`` (in Blender). Returns the gate, metrics, seam set, chart
    count, and per-round history; leaves the object holding the best layout.

    ``forbidden_edges`` (a set of mesh edge ids the user wants PRESERVED, e.g. a Blender
    selection) are never traversed by the fold-repair cut paths and are stripped from the
    shipped seam set. ``prune_auxiliary`` (default on) runs a post-pass that removes
    low-angle auxiliary fold-repair seams whose removal keeps every hard gate green.

    Minimal-distortion mode (MINIMAL_DISTORTION_UV_PLAN, default): start from the fewest
    charts compatible with mandatory 90° seams + disk topology, then split ONE worst
    island per round only while overlap, the GLOBAL checker distortion, OR the WORST
    single island's distortion exceeds the gate (so a layout that passes the global mean
    but hides one badly-stretched island is still split). One split per round, hence
    ``max_rounds`` is generous (the safety cap ``island_count_max`` bounds the total).
    Island count is never raised for chart-shape aesthetics. The convexity-driven shape
    repair / tail round are OFF by default (``shape_passes=False``) — they split charts to
    chase convexity, which the plan forbids; convexity is now an advisory report only.
    Set ``shape_passes=True`` to restore the legacy U1.6/U1.7 shape rounds.

    Distortion-split accept/revert (RULE_BASED_UV_SEAM_CORE_PLAN §5.2): a checker-distortion
    split is PROVISIONAL — the next round measures the split region's new distortion and the
    seam is KEPT only if it cut that region's distortion by ≥ ``min_improvement_ratio`` (or
    the layout now passes the gate). A split that doesn't pay for its extra island is reverted
    and that region is not retried, so the island count never rises for a negligible gain.

    Important Region Policy (IMPORTANT_REGION_UV_POLICY_PLAN, optional, off when
    ``region_policy is None`` — identical baseline behaviour): an
    :class:`artist_uv_agent.region_policy.RegionPolicy` protecting artist-important regions
    (face front, hands, logo). Precedence is mandatory 90° > region protection > distortion
    split: a distortion split whose new seam edges would cut a protected *smooth* (<90°) edge
    is REJECTED before it is committed (§5.4 post-split reject — ``split_chart`` is NOT
    rewritten); that island is then left alone, and the reject is recorded in the history and
    the ``seam_report`` ``regions`` block. Mandatory ≥90° folds are never protected, so they
    still ship."""
    from chart_uv_agent.unwrap import (
        flipped_faces, island_plan_from_seams, read_uvmap, repack, unwrap_and_pack,
    )

    from chart_uv_agent.segmentation import (
        flood_charts, mandatory_seam_edges, split_welded_folds,
    )
    from chart_uv_agent.shape import measure_charts
    from chart_uv_agent.shape_repair import repair_shapes, tail_round

    config = config or ChartGateConfig()
    forbidden = set(forbidden_edges or ())

    # USER-GUIDED SEAM mode (USER_GUIDED_SEAM_UV_PIPELINE_PLAN §8.2). When the caller supplies a
    # user seam spec, the user's seams are the authoritative source of truth: the auto chart
    # solver is bypassed entirely (so the no-spec path below is byte-for-byte unchanged — plan
    # §12 success criterion #1) and a dedicated report-only path runs instead.
    if user_seam_spec is not None:
        return _run_user_seam_uv(obj, mesh, user_seam_spec, config=config,
                                 base_forbidden=forbidden, margin=margin,
                                 auto_refine=auto_refine_user_seams,
                                 repair_user_seams=repair_user_seams,
                                 enforce_mandatory=enforce_user_mandatory,
                                 gate_mandatory=gate_user_mandatory,
                                 optimize_layout=optimize_layout,
                                 layout_optimization_config=layout_optimization_config)

    aux_seams: set[int] = set()       # welded-fold-auxiliary seams (prune candidates)
    overlap_seams: set[int] = set()   # added by flip / raster overlap repair
    distortion_seams: set[int] = set()  # added by the checker-distortion split
    seg = segment(mesh, cone_limit=cone_limit, max_charts=config.island_count_max)
    seams = set(seg.seams)
    # Region-aware FACE RECOVERY (REGION_AWARE_FACE_UV_RECOVERY_PLAN §6.3): the FRONT-stage
    # lever — dissolve face_front_core interior smooth boundaries the segmentation created, so
    # the face front stays a coherent island (mandatory folds + disk + a bounded normal cone
    # keep hard correctness and stop the v1 distortion blow-up). Off unless a face_recovery
    # region policy is supplied; the experimental post-split reject path is NOT used here.
    region_merge = {"removed": [], "merges": 0, "history": []}
    region_mode = getattr(region_policy, "mode", None) if region_policy is not None else None
    if region_policy is not None and region_mode == "face_recovery":
        from artist_uv_agent.region_policy import region_protected_merge
        region_merge = region_protected_merge(mesh, seams, region_policy, fold_angle=90.0)
    # M3 minimal segmentation: the convexity-driven shape repair / tail round inflate the
    # island count for shape (forbidden by the plan), so they are OFF by default. When
    # enabled (legacy), repair runs before U2 and the tail round proves worst-decile charts.
    if shape_passes:
        repair = repair_shapes(mesh, seams, convexity_min=0.92, max_charts=config.island_count_max)
        tail = tail_round(mesh, seams, convexity_bar=config.convexity_p10_min,
                          max_charts=config.island_count_max)
    else:
        repair = {"history": []}
        tail = {"history": [], "stuck": []}
    stuck_charts = tail["stuck"]
    mandatory = mandatory_seam_edges(mesh, fold_angle=90.0)
    history: list[dict] = [{"stage": "segment", **seg.history[-1]},
                           {"stage": "region_protected_merge", "action": "region_protected_merge",
                            "enabled": region_mode == "face_recovery",
                            "merges": region_merge["merges"],
                            "removed_core_smooth_edges": len(region_merge["removed"]),
                            "rounds": region_merge["history"]},
                           {"stage": "shape_repair", "enabled": shape_passes, "rounds": repair["history"]},
                           {"stage": "tail_round", "enabled": shape_passes,
                            "rounds": tail["history"], "stuck": tail["stuck"]}]
    best = None
    repacked = False
    raster_margin_bumped = False
    bbox_packed = False
    pending_split = None              # provisional distortion split awaiting an accept/revert
    rejected_regions: set[frozenset] = set()  # face sets a reverted split proved not worth cutting

    for rnd in range(max_rounds):
        unwrap_and_pack(obj, seams, margin=margin)
        uvmap = read_uvmap(obj, mesh)
        plan = island_plan_from_seams(mesh, seams)
        ev = evaluate_uv_solution(mesh, plan, uvmap)
        charts, face_chart = _charts(mesh, seams)
        metrics = _chart_metrics(mesh, uvmap, ev)
        metrics["small_island_ratio"] = relative_small_island_ratio(mesh, plan, uvmap)
        metrics.update(_shape_metrics(mesh, seams, mandatory))
        metrics.update(_audit_metrics(mesh, seams))            # R2: mandatory_90_missing (set)
        metrics.update(_uv_audit_metrics(mesh, uvmap))         # R2: mandatory_90_uv_unsplit (UV)
        metrics.update(_distortion_report(mesh, uvmap, charts))  # checker distortion names
        # TRUE raster overlap (correctness round) + per-chart attribution for repair.
        diag = raster_overlap_diagnosis(mesh, uvmap, face_chart)
        metrics["raster_overlap_ratio"] = diag["raster_overlap_ratio"]
        gate = evaluate_chart_gate(metrics, config=config)
        rec = {"round": rnd, "charts": ev.island_count, "stretch": round(ev.stretch_score, 4),
               "checker_distortion": metrics["checker_distortion_score"],
               "worst_island": metrics["worst_island_id"],
               "worst_island_distortion": metrics["worst_island_distortion"],
               "overlap": round(ev.overlap_ratio, 5), "raster": diag["raster_overlap_ratio"],
               "self": diag["self_overlap_ratio"], "cross": diag["cross_overlap_ratio"],
               "packing": round(ev.packing_efficiency, 4),
               "mandatory_90_missing": metrics["mandatory_90_missing"],
               "mandatory_90_uv_unsplit": metrics["mandatory_90_uv_unsplit"],
               "verdict": gate.verdict, "fails": [c.name for c in gate.failures],
               "action": "stop", "reason": ""}
        history.append(rec)

        # Resolve last round's PROVISIONAL distortion split (§5.2 accept/revert). Measure the
        # split region's distortion now; keep the seam only if it improved enough OR the gate
        # now passes, else revert it and never retry that region. Done BEFORE the best-update
        # so a reverted (kept-out) seam set is what competes to be ``best``.
        if pending_split is not None:
            from artist_uv_agent.seam_refinement import improvement_ratio
            after = _chart_distortion(mesh, pending_split["faces"], per_face_stretch(mesh, uvmap))
            imp = improvement_ratio(pending_split["before"], after)
            psrec = pending_split["rec"]
            psrec["distortion_after"] = round(float(after), 6)
            psrec["improvement_ratio"] = round(float(imp), 4)
            if imp >= min_improvement_ratio or gate.passed:
                psrec["accepted"] = True
                pending_split = None
            else:
                seams -= pending_split["added"]
                distortion_seams -= pending_split["added"]
                rejected_regions.add(pending_split["faces"])
                psrec["accepted"] = False
                psrec["reverted"] = True
                rec["action"] = "revert"
                rec["reason"] = "distortion_split_reverted"
                rec["reverted_improvement_ratio"] = round(float(imp), 4)
                pending_split = None
                continue   # re-measure the reverted seam set next round

        if best is None or _better(metrics, gate, best):
            best = {"seams": set(seams), "metrics": metrics, "gate": gate, "ev": ev}
        if gate.passed:
            rec["reason"] = "all hard gates pass (distortion/overlap/bounds/seams)"
            break

        changed = False

        # (0) R2 first: a fold welded in the ACTUAL UV → make it a chart boundary with a
        # LOCAL minimum-cost cut (routes along creases, avoids low-angle / forbidden edges)
        # rather than a broad chart-wide split. Targeted at the welded folds only.
        if metrics["mandatory_90_uv_unsplit"] > 0:
            r = split_welded_folds(mesh, seams, metrics["uv_unsplit_edge_ids"],
                                   forbidden=forbidden, region_policy=region_policy)
            if r["added"] or r["local_cuts"] or r["fallback"]:
                aux_seams |= r["added"]
                changed = True
                rec["action"] = "split"; rec["reason"] = "welded_fold_auxiliary"
                rec["fold_local_cuts"] = r["local_cuts"]; rec["fold_fallback"] = r["fallback"]

        # (1) Flipped UV faces → re-split the folding charts (overlap correctness).
        flips = flipped_faces(mesh, uvmap)
        if not changed and flips:
            ns = _split_flipped_charts(mesh, seams, face_chart, charts, flips)
            if ns:
                seams.update(ns); overlap_seams |= set(ns); changed = True
                rec["action"] = "split"; rec["reason"] = "flipped_faces"

        # (2) Raster-overlap repair: (b) inter-chart invasion → margin bump then AABB
        # re-pack (no split); (a) self-intersection → split the folding charts.
        if not changed and metrics["raster_overlap_ratio"] > config.raster_overlap_max:
            if diag["cross_charts"] and not bbox_packed:
                if not raster_margin_bumped:
                    repack(obj, margin=min(0.05, margin * 4)); raster_margin_bumped = True
                else:
                    repack(obj, margin=margin, pack_shape="AABB"); bbox_packed = True
                changed = True
                rec["action"] = "repack"; rec["reason"] = "raster_overlap_cross_invasion"
            else:
                cap = config.island_count_max
                for cid in diag["self_charts"]:
                    if cid < len(charts) and len(charts[cid]) >= 2 * 5 \
                            and len(_charts(mesh, seams)[0]) < cap:
                        _, _, ns = split_chart(mesh, charts[cid], seams)
                        if ns:
                            seams.update(ns); overlap_seams |= set(ns); changed = True
                if changed:
                    rec["action"] = "split"; rec["reason"] = "raster_overlap_self"

        # (3) Checker/stretch distortion over threshold → split exactly the ONE worst
        # island (MINIMAL_DISTORTION_UV_PLAN §M4: one split, one recorded reason). The
        # trigger is GLOBAL mean stretch OR the WORST single island — a layout whose global
        # mean passes but whose worst island is badly stretched still splits that island.
        global_over = metrics["stretch_score"] > config.stretch_max
        worst_over = metrics["worst_island_distortion"] > config.worst_island_distortion_max
        # Don't open a PROVISIONAL split on the last allowed round — there'd be no next round
        # to measure its improvement, so it could neither be accepted nor reverted (§5.2).
        if not changed and (global_over or worst_over) \
                and len(charts) < config.island_count_max and rnd < max_rounds - 1:
            fstr = per_face_stretch(mesh, uvmap)
            # Pick the worst chart NOT already proved not-worth-splitting (a reverted region).
            ranked = sorted(((cid, fs) for cid, fs in enumerate(charts)
                             if frozenset(fs) not in rejected_regions),
                            key=lambda cf: _chart_distortion(mesh, cf[1], fstr), reverse=True)
            worst_available = _chart_distortion(mesh, ranked[0][1], fstr) if ranked else 0.0
            # When the ONLY thing over the bar is a worst island we can no longer split (it is
            # protected/rejected) and the best AVAILABLE island is already under the per-island
            # threshold, splitting it cannot help — stop honestly rather than inflate the island
            # count on already-fine islands (IMPORTANT_REGION_UV_POLICY_PLAN §11.3; also fixes a
            # latent thrash after a §5.2 revert). No effect on a clean run: there ranked[0] IS
            # the global worst, so ``worst_available`` exceeds the threshold and the split fires.
            if ranked and not global_over and worst_available <= config.worst_island_distortion_max:
                ranked = []
                rec["reason"] = "worst_island_unsplittable_best_effort"
            if ranked:
                worst_cid, worst = ranked[0]
                before = _chart_distortion(mesh, worst, fstr)
                _, _, ns = split_chart(mesh, worst, seams)
                # EXPERIMENTAL post-split reject (mode="post_split_reject" only, off by default —
                # REGION_AWARE_FACE_UV_RECOVERY_PLAN §2.1: the v1 approach did NOT reduce face
                # seams). Reject a split that would cut a protected smooth edge; the island is
                # marked rejected and the next round re-measures the unchanged seam set. The
                # default face_recovery mode handles the face up front (region merge) instead.
                protected_cut = (region_policy.protected_cut(ns)
                                 if (ns and region_policy and region_mode == "post_split_reject")
                                 else set())
                if protected_cut:
                    rejected_regions.add(frozenset(worst))
                    rec["action"] = "reject"
                    rec["reason"] = "protected_region_reject"
                    rec["region"] = (region_policy.region_names_for(protected_cut) or [None])[0]
                    rec["protected_edges_cut"] = sorted(protected_cut)
                    rec["split_island"] = worst_cid
                    changed = True   # re-measure the (unchanged) seam set next round
                elif ns:
                    seams.update(ns); distortion_seams |= set(ns); changed = True
                    rec["action"] = "split"
                    rec["reason"] = "checker_distortion" if global_over else "worst_island_distortion"
                    rec["split_island"] = worst_cid
                    rec["before_distortion"] = round(float(before), 6)
                    rec["added_edges"] = sorted(ns)
                    # Provisional — next round measures the region and accepts/reverts (§5.2).
                    pending_split = {"faces": frozenset(worst), "before": before,
                                     "added": set(ns), "rec": rec}

        # (4) Packing is ADVISORY — never split for it. A single tighter-margin re-pack is
        # the only packing action (more, smaller charts pack *worse*, so splitting is
        # counterproductive). Only reached when a hard gate other than packing still fails.
        if not changed and metrics["packing_efficiency"] < config.packing_min and not repacked:
            repack(obj, margin=margin * 0.5)
            repacked = True
            changed = True  # re-measure next round without changing seams
            rec["action"] = "repack"; rec["reason"] = "packing_retune"

        if not changed:
            rec["reason"] = "no further refinement available (best-effort)"
            break

    # A distortion split that never got a measurement round (loop exhausted) is left honestly
    # UNRESOLVED — its improvement is unproved, so the report excludes it (the guard above
    # normally prevents this; this is belt-and-suspenders). The shipped seam set is still the
    # ``best`` pick, re-measured by the correctness/measure passes below.
    if pending_split is not None:
        psrec = pending_split["rec"]
        psrec["accepted"] = False
        psrec["unresolved"] = True
        psrec["note"] = "distortion_split_unresolved (no round left to measure improvement)"
        pending_split = None

    # Correctness round: eliminate TRUE (raster) overlap. Owns the final UV; may raise the
    # chart count / stretch (CONFORMAL) — reported as regression. Pre-correctness metrics
    # are captured for the before/after table.
    final_seams = set(best["seams"])
    pre = dict(best["metrics"])
    correctness = correctness_pass(obj, mesh, final_seams, config, margin=margin)
    final_seams |= mandatory

    def measure():
        """Unwrap+pack the current ``final_seams`` and return (metrics, gate, ev). Owns the
        object's shipped UV — every seam edit here is followed by exactly one re-unwrap."""
        unwrap_and_pack(obj, final_seams, margin=margin)
        uvm = read_uvmap(obj, mesh)
        pl = island_plan_from_seams(mesh, final_seams)
        e = evaluate_uv_solution(mesh, pl, uvm)
        chs, _fc = _charts(mesh, final_seams)
        m = _chart_metrics(mesh, uvm, e)
        m["small_island_ratio"] = relative_small_island_ratio(mesh, pl, uvm)
        m.update(_shape_metrics(mesh, final_seams, mandatory))
        m.update(_audit_metrics(mesh, final_seams))
        m.update(_uv_audit_metrics(mesh, uvm))            # UV-level R2 on the SHIPPED uvmap
        m.update(_distortion_report(mesh, uvm, chs))
        fcr = {f: i for i, fs in enumerate(flood_charts(mesh, final_seams)) for f in fs}
        m["raster_overlap_ratio"] = raster_overlap_diagnosis(mesh, uvm, fcr)["raster_overlap_ratio"]
        return m, evaluate_chart_gate(m, config=config), e

    # R2 (§M2): cut every welded fold with a LOCAL min-cost cut, re-unwrap, until none weld.
    metrics, gate, ev = measure()
    for _ in range(8):
        if metrics["mandatory_90_uv_unsplit"] == 0:
            break
        r = split_welded_folds(mesh, final_seams, metrics["uv_unsplit_edge_ids"],
                               forbidden=forbidden, region_policy=region_policy)
        if not (r["added"] or r["local_cuts"] or r["fallback"]):
            break  # cannot cut further — surfaced honestly by the hard gate
        aux_seams |= r["added"]
        metrics, gate, ev = measure()

    # User preserve: a non-mandatory forbidden edge must never ship as a seam. The cut paths
    # already avoid them; strip any that an earlier (VSA fallback / segmentation) pass placed.
    strip = {e for e in forbidden if e in final_seams and mesh.edges[e].dihedral_angle < 90.0}
    if strip:
        final_seams -= strip
        aux_seams -= strip
        metrics, gate, ev = measure()

    # Pruning: drop low-angle welded-fold-auxiliary seams whose removal keeps EVERY hard gate
    # green (fewer needless cuts, lower vt/v). One at a time, flattest first; revert on fail.
    pruned: list[int] = []
    if prune_auxiliary and gate.passed:
        cand = sorted((e for e in aux_seams if e in final_seams
                       and mesh.edges[e].dihedral_angle < 90.0),
                      key=lambda e: mesh.edges[e].dihedral_angle)[:_PRUNE_CAP]
        pruned = _prune_seams(final_seams, cand, lambda: measure()[1].passed)
        aux_seams -= set(pruned)
        metrics, gate, ev = measure()     # ship the kept seam set

    seam_types = _classify_seams(mesh, final_seams, aux_seams, overlap_seams,
                                 distortion_seams, forbidden)
    distortion = {k: metrics[k] for k in ("checker_distortion_score", "worst_island_distortion",
                                          "worst_island_id", "worst_face_count")}
    initial_islands = history[0].get("charts", ev.island_count)
    conclusion = _island_conclusion(initial_islands, ev.island_count, history, metrics, config)
    result = {
        "engine": "chart", "seams": sorted(final_seams), "chart_count": ev.island_count,
        "metrics": metrics, "gate": gate, "gate_config": config.to_dict(),
        "history": history, "rounds": len(history) - 1,
        "distortion": distortion, "conclusion": conclusion,
        "mandatory_90_edges": metrics["mandatory_90_edges"],
        "mandatory_90_missing": metrics["mandatory_90_missing"],
        "mandatory_90_fold_edges": metrics["mandatory_90_fold_edges"],
        "mandatory_90_uv_unsplit": metrics["mandatory_90_uv_unsplit"],
        "initial_island_count": initial_islands, "final_island_count": ev.island_count,
        "seam_type_counts": _count_types(seam_types), "pruned_auxiliary": len(pruned),
        "forbidden_edges": sorted(forbidden), "forbidden_stripped": sorted(strip),
        "shape_repair": repair["history"], "tail_round": tail["history"],
        "correctness": correctness["history"],
        "metrics_before_correctness": {k: pre.get(k) for k in
            ("raster_overlap_ratio", "stretch_score", "packing_efficiency",
             "convexity_p10", "island_count")},
        "stuck_charts": stuck_charts, "shippable": shippable_with_stuck(gate, stuck_charts),
    }
    # Important Region Policy report (IMPORTANT_REGION_UV_POLICY_PLAN §5.5) — the mandatory-
    # vs-smooth seam split per protected region + the rejected-split records, built from the
    # SHIPPED seams. Only present when a region policy was supplied (else baseline behaviour).
    if region_policy is not None:
        from artist_uv_agent.region_policy import build_region_report, region_boundary_audit
        result["region_report"] = build_region_report(region_policy, mesh, final_seams, history)
        result["region_audit"] = region_boundary_audit(mesh, final_seams, region_policy)
        result["region_mode"] = region_mode
        result["region_protected_merges"] = region_merge["merges"]

    # Reviewer-facing Seam Decision Core report (RULE_BASED_UV_SEAM_CORE_PLAN §5.3) — pure,
    # built from the result above so the app gets the seam reasons / distortion / conflicts.
    from artist_uv_agent.seam_report import build_seam_report
    result["seam_report"] = build_seam_report(result, source_mesh=getattr(mesh, "object_id", None))
    return result


def _run_user_seam_uv(obj, mesh: MeshGraph, spec, *, config: ChartGateConfig,
                      base_forbidden: set[int], margin: float, auto_refine: bool,
                      repair_user_seams: bool, enforce_mandatory: bool,
                      gate_mandatory: bool, optimize_layout: bool = False,
                      layout_optimization_config=None) -> dict:
    """User-guided seam UV path (USER_GUIDED_SEAM_UV_PIPELINE_PLAN §8.2). The user's seam spec
    is the source of truth: the initial seam set is ``mandatory_90 ∪ user_seam_edges`` and the
    non-mandatory ``user_protected_edges`` are forwarded as forbidden (never cut). The app does
    NOT add seams of its own — it unwraps/packs the user's seams, then HONESTLY reports
    distortion / overlap / mandatory audits (plan §10: report first, don't auto-fix). The only
    seams the engine may add are mandatory-fold boundary cuts (a ≥90° fold welded in the actual
    UV must become a chart boundary — mandatory wins, plan §7) and, ONLY when
    ``auto_refine=True``, distortion splits — each tracked separately as ``auto_added_seams``.

    Pure-ish: this owns the object's UV (each seam edit is followed by one re-unwrap) but adds
    no auto chart segmentation, so the no-spec ``run_chart_uv`` path is untouched (plan §13)."""
    from chart_uv_agent.unwrap import (
        island_plan_from_seams, read_uvmap, unwrap_and_pack,
    )
    from chart_uv_agent.segmentation import flood_charts, split_chart, split_welded_folds
    from artist_uv_agent.user_seams import build_user_seam_set

    usr = build_user_seam_set(mesh, spec)
    mandatory = set(usr.mandatory_edges) if enforce_mandatory else set()
    if enforce_mandatory:
        forbidden = set(base_forbidden) | usr.forbidden_edges
        final_seams = set(usr.initial_seams)
    else:
        # Strict user/reference mode: use exactly the user's seam intent, without adding
        # mandatory folds. Protected edges still matter only for optional auto-refine.
        forbidden = set(base_forbidden) | (usr.user_protected_edges - usr.user_seam_edges)
        final_seams = set(usr.user_seam_edges)
    aux_seams: set[int] = set()         # mandatory-fold boundary cuts (auto, but mandatory-driven)
    distortion_seams: set[int] = set()  # auto_refine distortion splits (auto_added)
    history: list[dict] = [{"stage": "user_seams", "mode": "user_seams",
                            "user_seam_edges": len(usr.user_seam_edges),
                            "user_protected_edges": len(usr.user_protected_edges),
                            "mandatory_90_edges": len(mandatory),
                            "forbidden_edges": len(forbidden),
                            "conflicts": usr.conflicts, "invalid_edges": usr.invalid_edges,
                            "auto_refine": auto_refine,
                            "repair_user_seams": repair_user_seams,
                            "enforce_mandatory": enforce_mandatory,
                            "gate_mandatory": gate_mandatory}]

    def _eval_current():
        """Measure the object's CURRENT UV against ``final_seams`` (no re-unwrap)."""
        uvm = read_uvmap(obj, mesh)
        pl = island_plan_from_seams(mesh, final_seams)
        e = evaluate_uv_solution(mesh, pl, uvm)
        chs, _fc = _charts(mesh, final_seams)
        m = _chart_metrics(mesh, uvm, e)
        m["small_island_ratio"] = relative_small_island_ratio(mesh, pl, uvm)
        m.update(_shape_metrics(mesh, final_seams, mandatory))
        m.update(_audit_metrics(mesh, final_seams))
        m.update(_uv_audit_metrics(mesh, uvm))
        m.update(_distortion_report(mesh, uvm, chs))
        fcr = {f: i for i, fs in enumerate(flood_charts(mesh, final_seams)) for f in fs}
        m["raster_overlap_ratio"] = raster_overlap_diagnosis(mesh, uvm, fcr)["raster_overlap_ratio"]
        g = evaluate_chart_gate(m, config=config)
        if not gate_mandatory:
            g = _without_mandatory_gate_checks(g)
        return m, g, e

    def measure():
        """Unwrap+pack the current ``final_seams`` and measure — owns the shipped UV."""
        unwrap_and_pack(obj, final_seams, margin=margin)
        return _eval_current()

    metrics, gate, ev = measure()
    initial_islands = ev.island_count

    # Mandatory ≥90° folds welded in the ACTUAL UV → cut to a chart boundary (mandatory wins,
    # plan §7 + §12: mandatory_90_uv_unsplit == 0). The LOCAL min-cost cut avoids forbidden
    # (protected) edges. These are mandatory-driven, NOT "auto seam"; still counted under
    # ``auto_added_seams`` honestly (they're not user/mandatory-fold edges themselves).
    if repair_user_seams and enforce_mandatory:
        for _ in range(8):
            if metrics["mandatory_90_uv_unsplit"] == 0:
                break
            r = split_welded_folds(mesh, final_seams, metrics["uv_unsplit_edge_ids"], forbidden=forbidden)
            if not (r["added"] or r["local_cuts"] or r["fallback"]):
                break  # cannot cut further — surfaced honestly by the hard gate
            aux_seams |= r["added"]
            history.append({"stage": "mandatory_fold_cut", "added": sorted(r["added"]),
                            "local_cuts": r["local_cuts"], "fallback": r["fallback"]})
            metrics, gate, ev = measure()
    elif gate_mandatory and metrics["mandatory_90_uv_unsplit"] != 0:
        history.append({"stage": "mandatory_fold_report_only",
                        "uv_unsplit": metrics["mandatory_90_uv_unsplit"],
                        "edge_ids": metrics.get("uv_unsplit_edge_ids", [])})

    # OPTIONAL auto distortion refine (plan §10, ``auto_refine_user_seams=true`` only). Split the
    # worst island while distortion exceeds the gate; never cut a protected edge (precedence:
    # protected > auto). Every added seam is tracked as an ``auto_added`` seam and reported.
    if auto_refine:
        rejected: set[frozenset] = set()
        for rnd in range(config.island_count_max):
            global_over = metrics["stretch_score"] > config.stretch_max
            worst_over = metrics["worst_island_distortion"] > config.worst_island_distortion_max
            charts, _fc = _charts(mesh, final_seams)
            if not (global_over or worst_over) or len(charts) >= config.island_count_max:
                break
            fstr = per_face_stretch(mesh, read_uvmap(obj, mesh))
            ranked = sorted((fs for fs in charts if frozenset(fs) not in rejected),
                            key=lambda fs: _chart_distortion(mesh, fs, fstr), reverse=True)
            if not ranked:
                break
            worst = ranked[0]
            before = _chart_distortion(mesh, worst, fstr)
            _, _, ns = split_chart(mesh, worst, final_seams)
            ns = set(ns)
            if not ns or (ns & forbidden):
                # The only available split would cut a protected edge → reject (report-only).
                rejected.add(frozenset(worst))
                history.append({"stage": "auto_refine_reject", "round": rnd,
                                "reason": "protected_edge_cut" if ns else "unsplittable"})
                continue
            final_seams |= ns
            distortion_seams |= ns
            metrics, gate, ev = measure()
            history.append({"stage": "auto_refine_split", "round": rnd, "added_edges": sorted(ns),
                            "before_distortion": round(float(before), 6),
                            "after_worst_distortion": metrics["worst_island_distortion"],
                            "auto_added": True})

    # UV LAYOUT OPTIMIZATION LOOP (UV_LAYOUT_OPTIMIZATION_LOOP_PLAN §9.2). The seam set is now
    # FINAL — this never adds/removes a seam. It sweeps relax/scale/rotate/pack candidates on
    # the SAME ``final_seams``, scores each (checker distortion + texel density + overlap +
    # packing), and applies the best (or keeps the baseline if no candidate clearly wins).
    # Mandatory-90 audits are report-only here (plan §3.1) — already so when gate_mandatory off.
    layout_opt = None
    if optimize_layout:
        from chart_uv_agent.layout_optimization import (
            LayoutOptimizationConfig, run_layout_optimization,
        )
        lo_cfg = layout_optimization_config or LayoutOptimizationConfig(
            enabled=True, mode="user_reference")

        def _measure_candidate(cand_spec):
            unwrap_and_pack(obj, final_seams, margin=cand_spec["margin"],
                            method=cand_spec["unwrap_method"],
                            minimize_iters=cand_spec["minimize_iters"],
                            pack_shape=cand_spec["pack_shape"], rotate=cand_spec["rotate"],
                            average_scale=cand_spec["average_scale"])
            return _eval_current()[0]

        layout_opt = run_layout_optimization(_measure_candidate, dict(metrics), lo_cfg,
                                             mode="user_reference")
        sp = layout_opt.selected_spec
        unwrap_and_pack(obj, final_seams, margin=sp["margin"], method=sp["unwrap_method"],
                        minimize_iters=sp["minimize_iters"], pack_shape=sp["pack_shape"],
                        rotate=sp["rotate"], average_scale=sp["average_scale"])
        metrics, gate, ev = _eval_current()   # the shipped (best) layout
        history.append({"stage": "layout_optimization",
                        "selected_candidate_id": layout_opt.selected_candidate_id,
                        "kept_baseline": layout_opt.kept_baseline,
                        "candidate_count": len(layout_opt.candidates),
                        "score_before": round(float(layout_opt.score_before), 6),
                        "score_after": round(float(layout_opt.score_after), 6)})

    seam_types = _classify_user_seams(mesh, final_seams, usr, aux_seams, distortion_seams,
                                      fold_angle=spec.mandatory_fold_angle,
                                      classify_mandatory=enforce_mandatory)
    # auto_added = every shipped seam that is NEITHER a user seam NOR a mandatory fold edge
    # (= the mandatory-fold path cuts + any auto_refine splits). 0 when the user spec already
    # separates every fold and no refine ran (plan §12: auto_added_seams default 0).
    auto_added = len(final_seams - usr.user_seam_edges - mandatory)
    distortion = {k: metrics[k] for k in ("checker_distortion_score", "worst_island_distortion",
                                          "worst_island_id", "worst_face_count")}
    user_block = {"mode": "user_seams",
                  **usr.report(final_seams=final_seams, auto_added=auto_added)}
    if not enforce_mandatory:
        user_block["mandatory_rule_enabled"] = False
        user_block["mandatory_90_edges"] = 0
    if not gate_mandatory:
        user_block["mandatory_gate_enabled"] = False
    conclusion = _user_seam_conclusion(usr, metrics, config, auto_added, auto_refine,
                                       include_mandatory_diagnostics=gate_mandatory)
    result = {
        "engine": "chart", "mode": "user_seams", "seams": sorted(final_seams),
        "chart_count": ev.island_count, "metrics": metrics, "gate": gate,
        "gate_config": config.to_dict(), "history": history, "rounds": len(history) - 1,
        "distortion": distortion, "conclusion": conclusion,
        "mandatory_90_edges": metrics["mandatory_90_edges"],
        "mandatory_90_missing": metrics["mandatory_90_missing"],
        "mandatory_90_fold_edges": metrics["mandatory_90_fold_edges"],
        "mandatory_90_uv_unsplit": metrics["mandatory_90_uv_unsplit"],
        "initial_island_count": initial_islands, "final_island_count": ev.island_count,
        "seam_type_counts": _count_types(seam_types), "pruned_auxiliary": 0,
        "forbidden_edges": sorted(forbidden), "forbidden_stripped": [],
        "shape_repair": [], "tail_round": [], "correctness": [],
        "metrics_before_correctness": {}, "stuck_charts": [],
        "shippable": shippable_with_stuck(gate, []),
        "user_seams": user_block,
    }
    if layout_opt is not None:
        result["layout_optimization"] = layout_opt.report()
        result["layout_optimization_summary"] = layout_opt.summary()
    from artist_uv_agent.seam_report import build_seam_report
    result["seam_report"] = build_seam_report(result, source_mesh=getattr(mesh, "object_id", None))
    return result


def _classify_user_seams(mesh, seams, usr, aux_seams, distortion_seams, *,
                         fold_angle: float = 90.0, classify_mandatory: bool = True) -> dict:
    """Tag each shipped user-mode seam by origin. Precedence (plan §7): a ≥``fold_angle`` fold
    is always ``mandatory_90``; then user seams, then auto distortion splits, then the
    mandatory-fold boundary cuts; anything else is a leftover ``auto`` seam (should not occur)."""
    out: dict[int, str] = {}
    for e in seams:
        if classify_mandatory and (mesh.edges[e].dihedral_angle >= fold_angle or e in usr.mandatory_edges):
            out[e] = "mandatory_90"
        elif e in usr.user_seam_edges:
            out[e] = "user_seam"
        elif e in distortion_seams:
            out[e] = "distortion_split"
        elif e in aux_seams:
            out[e] = "mandatory_fold_auxiliary"
        else:
            out[e] = "auto"
    return out


def _without_mandatory_gate_checks(gate: ChartGateResult) -> ChartGateResult:
    """User/reference seam mode can be run with mandatory fold rules disabled. Keep the
    measurements in the report, but remove them from the blocking gate decision."""
    return ChartGateResult(
        checks=[c for c in gate.checks
                if c.name not in {"mandatory_90_missing", "mandatory_90_uv_unsplit"}]
    )


def _user_seam_conclusion(usr, metrics, config, auto_added: int, auto_refine: bool, *,
                          include_mandatory_diagnostics: bool = True) -> str:
    """One-line honest verdict for user-seam mode (plan §10 quality feedback)."""
    parts: list[str] = []
    if usr.conflicts:
        parts.append(f"{len(usr.conflicts)} protected/mandatory conflict(s) (mandatory wins)")
    if usr.invalid_edges:
        parts.append(f"{len(usr.invalid_edges)} invalid edge id(s) ignored")
    if include_mandatory_diagnostics:
        unsplit = int(metrics.get("mandatory_90_uv_unsplit", 0))
        missing = int(metrics.get("mandatory_90_missing", 0))
        if missing:
            parts.append(f"{missing} mandatory ≥90° fold(s) MISSING from the seam set")
        if unsplit:
            parts.append(f"{unsplit} mandatory fold(s) still WELD in the UV")
    if metrics.get("raster_overlap_ratio", 0.0) > config.raster_overlap_max:
        parts.append(f"raster overlap {metrics['raster_overlap_ratio']:.4f} over "
                     f"{config.raster_overlap_max}")
    if metrics.get("worst_island_distortion", 0.0) > config.worst_island_distortion_max:
        parts.append(f"worst island distortion {metrics['worst_island_distortion']:.3f} over "
                     f"{config.worst_island_distortion_max}")
    if auto_added:
        kind = "auto distortion + mandatory-fold" if auto_refine else "mandatory-fold"
        parts.append(f"{auto_added} {kind} seam(s) added")
    head = "user-seam mode: applied the user's seam plan"
    if not parts:
        return f"{head}; all hard gates pass with no auto-added seams."
    return f"{head}; " + "; ".join(parts) + " (reported, not auto-fixed)."


_PRUNE_CAP = 80  # max auxiliary-seam prune attempts (each a re-unwrap) — runtime bound


def _prune_seams(final_seams: set, candidates, accept) -> list:
    """Greedily remove each candidate seam, KEEPING the removal iff ``accept()`` (a no-arg
    callable that re-measures the now-current ``final_seams`` and returns True when every hard
    gate still holds) passes; otherwise put it back. Mutates ``final_seams`` in place and
    returns the list of edges actually removed (MINIMAL_DISTORTION_UV_PLAN follow-up §5)."""
    removed: list[int] = []
    for e in candidates:
        if e not in final_seams:
            continue
        final_seams.discard(e)
        if accept():
            removed.append(e)
        else:
            final_seams.add(e)            # revert — this seam is load-bearing
    return removed


def _classify_seams(mesh, seams, aux_seams, overlap_seams, distortion_seams, forbidden,
                    *, fold_angle: float = 90.0) -> dict:
    """Tag each shipped seam with its origin/type so a post-pass can tell which seams are
    safe to reconsider (only ``welded_fold_auxiliary``). Precedence: a ≥90° fold is always
    ``mandatory_90`` regardless of which pass first added it."""
    out: dict[int, str] = {}
    for e in seams:
        if mesh.edges[e].dihedral_angle >= fold_angle:
            out[e] = "mandatory_90"                 # mandatory wins, even if also forbidden
        elif e in forbidden:
            out[e] = "user_forbidden"               # should not occur after the strip pass
        elif e in aux_seams:
            out[e] = "welded_fold_auxiliary"
        elif e in distortion_seams:
            out[e] = "distortion_split"
        elif e in overlap_seams:
            out[e] = "overlap_repair"
        else:
            out[e] = "segmentation"
    return out


def _count_types(seam_types: dict) -> dict:
    out: dict[str, int] = {}
    for t in seam_types.values():
        out[t] = out.get(t, 0) + 1
    return out


def _island_conclusion(initial: int, final: int, history, metrics, config) -> str:
    """The plan's required one-line verdict (MINIMAL_DISTORTION_UV_PLAN §M6): state whether
    the island count grew and why, and — per the user's per-island rule — explicitly
    distinguish "global distortion passed but the worst island still failed" (a best-effort
    ship where the loop ran out of splits) from a clean pass."""
    split_reasons = sorted({h.get("reason") for h in history
                            if h.get("action") == "split" and h.get("reason")})
    g_stretch = metrics.get("stretch_score", 0.0)
    worst = metrics.get("worst_island_distortion", 0.0)
    worst_id = metrics.get("worst_island_id", -1)
    global_ok = g_stretch <= config.stretch_max
    worst_ok = worst <= config.worst_island_distortion_max

    unsplit = int(metrics.get("mandatory_90_uv_unsplit", 0))
    if unsplit > 0:
        return (f"best-effort: {unsplit} of {metrics.get('mandatory_90_fold_edges', '?')} "
                f"≥90° folds still WELD in the UV (could not be cut to a chart boundary); "
                f"islands {initial}→{final}.")
    if global_ok and not worst_ok:
        # The user's exact concern: headline metric passes, one island is still distorted.
        return (f"best-effort: GLOBAL checker distortion passed ({g_stretch:.3f} ≤ "
                f"{config.stretch_max}) but WORST island {worst_id} distortion "
                f"{worst:.3f} exceeds the per-island threshold "
                f"{config.worst_island_distortion_max} (islands {initial}→{final}; "
                f"out of splits at the island cap or no-progress).")
    grow = ", ".join(split_reasons) or "distortion/overlap"
    if final > initial:
        return (f"islands increased from {initial} to {final} because checker distortion "
                f"or overlap exceeded threshold ({grow}); final worst-island distortion "
                f"{worst:.3f} ≤ {config.worst_island_distortion_max}.")
    return (f"islands stayed at {final} because both global ({g_stretch:.3f}) and "
            f"worst-island ({worst:.3f}) checker distortion were within threshold.")


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
    """Ship if the gate passes. Under the minimal-distortion gate convexity is ADVISORY
    (never a hard failure), so this is normally just ``gate.passed``. The legacy §5c branch
    — ship when the ONLY hard failure is the convexity_p10 tail and the tail loop proved
    the below-bar charts stuck — is retained for ``shape_passes=True`` runs that still
    surface convexity_p10 as a (now advisory) signal."""
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
