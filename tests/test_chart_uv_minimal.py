"""Minimal-island distortion-constrained UV — the user's three rules
(MINIMAL_DISTORTION_UV_PLAN §8). Blender-free: exercises the pure segmentation /
audit / gate / distortion helpers. The real per-face stretch refinement (the U2–U4
Blender loop) is covered by the worker regression run, not here.

The three rules:
  1. keep the UV island count as low as possible,
  2. every ≥90° model fold becomes a UV seam,
  3. split (raise island count) only when checker/stretch distortion exceeds threshold.
"""

import numpy as np

from chart_uv_agent.fixtures import (
    build_capsule_with_spikes, build_displaced_sphere, build_folded_planes,
    build_humanoid_blob,
)
from chart_uv_agent.gate import ChartGateConfig, evaluate_chart_gate
from chart_uv_agent.pipeline import (
    _audit_metrics, _chart_distortion, _classify_seams, _count_types, _distortion_report,
    _island_conclusion, _prune_seams, _user_seam_conclusion, _worst_stretch_chart,
)
from chart_uv_agent.segmentation import (
    edge_cut_cost, enforce_fold_boundaries, flood_charts, interior_fold_edges,
    mandatory_seam_audit, mandatory_seam_edges, segment, split_welded_folds,
)
from uv_agent.geometry.evaluation import mandatory_seam_uv_audit
from uv_agent.io.fixtures import build_grid_plane
from uv_agent.geometry.solution import UVMap


# -- Test 1: every ≥90° fold is a mandatory seam (Rule 2) --------------------

def test_mandatory_90_fold_becomes_a_seam():
    mesh = build_folded_planes(n=6)
    fold_edges = {e.id for e in mesh.edges
                  if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0}
    assert fold_edges  # the 90° join exists

    seg = segment(mesh)
    seams = set(seg.seams)
    # The shared fold edge is in the final seam set...
    assert fold_edges.issubset(seams)
    # ...and the audit proves no mandatory 90° edge is missing.
    audit = mandatory_seam_audit(mesh, seams)
    assert audit["mandatory_90_missing"] == 0
    assert audit["mandatory_90_edges"] >= len(fold_edges)


def test_mandatory_seams_survive_full_segmentation_passes():
    # absorb / merge / straighten must never re-route an R2 fold away.
    for mesh in (build_folded_planes(n=8), build_displaced_sphere()):
        required = mandatory_seam_edges(mesh)
        seams = set(segment(mesh).seams)
        assert required.issubset(seams)


# -- Test 2: no split when distortion passes (Rule 1, minimal) ---------------

def test_flat_plane_stays_one_island():
    # A flat sheet has zero curvature/distortion, so the minimal solution is ONE chart;
    # nothing should split it.
    plane = build_grid_plane(nx=6, ny=6)
    seg = segment(plane, cone_limit=50)
    charts = flood_charts(plane, set(seg.seams))
    assert len(charts) == 1
    assert mandatory_seam_audit(plane, set(seg.seams))["mandatory_90_missing"] == 0


def test_two_flat_halves_of_a_fold_stay_minimal():
    # The 90° fold forces exactly the one mandatory seam → two flat (zero-distortion)
    # charts. R1 must not split either half further.
    mesh = build_folded_planes(n=6)
    charts = flood_charts(mesh, set(segment(mesh).seams))
    assert len(charts) == 2


# -- Test 3: split when distortion fails (Rule 3) ----------------------------

def test_higher_distortion_raises_island_count():
    # Segmentation's distortion proxy (normal-cone half-angle, the Blender-free stand-in
    # for checker stretch): tightening the distortion bar forces more splits. This is the
    # mechanism the U2–U4 loop drives with real per-face stretch.
    mesh = build_displaced_sphere(segments=16, rings=12, amp=0.0)
    loose = len(flood_charts(mesh, set(segment(mesh, cone_limit=170).seams)))
    tight = len(flood_charts(mesh, set(segment(mesh, cone_limit=25).seams)))
    assert tight > loose
    # Even under aggressive splitting the mandatory seams remain satisfied.
    assert mandatory_seam_audit(mesh, set(segment(mesh, cone_limit=25).seams))["mandatory_90_missing"] == 0


def test_worst_island_is_the_split_target_scale_invariant():
    # The loop splits the ONE worst island. The worst-island pick is area-weighted, so a
    # small badly-stretched chart out-ranks a big mildly-stretched one (rule 3 targets the
    # genuinely distorted island, not merely the largest).
    mesh = build_displaced_sphere(segments=12, rings=8)
    charts = flood_charts(mesh, mandatory_seam_edges(mesh))
    big, small = max(charts, key=len), min(charts, key=len)
    assert big is not small
    fstr = np.zeros(mesh.face_count)
    for f in big:
        fstr[f] = 0.3            # large chart, mild distortion
    for f in small:
        fstr[f] = 2.0            # small chart, severe distortion
    assert _chart_distortion(mesh, small, fstr) > _chart_distortion(mesh, big, fstr)
    assert _worst_stretch_chart(mesh, charts, fstr) is small


# -- Test 4: do not split for convexity / packing alone (Rule 1) -------------

_GOOD = {
    "mandatory_90_missing": 0, "mandatory_90_uv_unsplit": 0, "overlap_ratio": 0.0,
    "raster_overlap_ratio": 0.001, "stretch_score": 0.3, "worst_island_distortion": 0.45,
    "texel_density_variance": 0.5, "island_count": 12, "uv_bounds_ok": True,
    "fallback_used": False,
}


def test_low_convexity_disk_still_passes_the_gate():
    # An oddly-shaped but low-distortion, overlap-free disk: convexity / smoothness /
    # tendrils / packing are advisory, so the gate passes and ships without an extra split.
    gate = evaluate_chart_gate({**_GOOD, "convexity_mean": 0.3, "convexity_p10": 0.2,
                                "boundary_smoothness_mean": 3.0, "tendril_count": 5,
                                "packing_efficiency": 0.30, "small_island_ratio": 0.6,
                                "vt_v_ratio": 1.4}, config=ChartGateConfig())
    assert gate.passed
    advisory_names = {c.name for c in gate.advisories}
    assert {"convexity_mean", "boundary_smoothness", "tendril_count",
            "packing_efficiency"} <= advisory_names
    assert not gate.failures


def test_distortion_failure_still_blocks_even_if_shape_is_fine():
    # The flip side: a high-distortion chart fails the gate even with perfect shape.
    gate = evaluate_chart_gate({**_GOOD, "stretch_score": 1.2, "convexity_mean": 0.99,
                                "convexity_p10": 0.99, "boundary_smoothness_mean": 1.0,
                                "tendril_count": 0, "packing_efficiency": 0.9,
                                "small_island_ratio": 0.0, "vt_v_ratio": 1.1},
                               config=ChartGateConfig())
    assert not gate.passed
    assert "stretch_score" in [c.name for c in gate.failures]


# -- Rule 3 PER-ISLAND: a single bad island blocks even if the global mean passes -------

def test_worst_island_distortion_blocks_when_global_passes():
    # Exactly the user's case: global stretch is fine but one island is badly stretched.
    cfg = ChartGateConfig()
    gate = evaluate_chart_gate({**_GOOD, "stretch_score": 0.17,
                                "worst_island_distortion": 0.87}, config=cfg)
    assert not gate.passed
    fails = [c.name for c in gate.failures]
    assert "worst_island_distortion" in fails
    assert "stretch_score" not in fails           # global mean still passes
    # Lowering the worst island under the bar clears the gate.
    ok = evaluate_chart_gate({**_GOOD, "stretch_score": 0.17,
                              "worst_island_distortion": 0.55}, config=cfg)
    assert ok.passed


# -- distortion report + conclusion plumbing (Phase M1 / M6) -----------------

def test_distortion_report_names_checker_metrics():
    plane = build_grid_plane(nx=4, ny=4)
    uvmap = UVMap.for_mesh(plane)
    for f in plane.faces:                       # UV == XY → zero stretch
        for li in f.loop_indices:
            x, y, _ = plane.vertices[plane.loops[li].vertex_id].co
            uvmap.set(li, x, y)
    charts = flood_charts(plane, mandatory_seam_edges(plane))
    rep = _distortion_report(plane, uvmap, charts)
    for k in ("checker_distortion_score", "worst_island_distortion",
              "worst_island_id", "worst_face_count"):
        assert k in rep
    assert rep["checker_distortion_score"] < 1e-6  # flat, UV==XY → no distortion


def test_audit_metrics_match_segmentation():
    mesh = build_folded_planes(n=6)
    seams = set(segment(mesh).seams)
    m = _audit_metrics(mesh, seams)
    assert m["mandatory_90_missing"] == 0
    assert m["mandatory_90_edges"] > 0


def test_island_conclusion_wording():
    cfg = ChartGateConfig()
    ok = {"stretch_score": 0.2, "worst_island_distortion": 0.4, "worst_island_id": 3}
    grew = _island_conclusion(6, 12, [{"action": "split", "reason": "checker_distortion"}], ok, cfg)
    assert "increased from 6 to 12" in grew and "checker_distortion" in grew
    stayed = _island_conclusion(6, 6, [{"action": "stop", "reason": "ok"}], ok, cfg)
    assert "stayed at 6" in stayed


def test_island_conclusion_flags_global_pass_but_worst_island_fail():
    # The §M6 distinction the user asked for: best-effort ship where the headline metric
    # passes but the worst island is still over the per-island threshold.
    cfg = ChartGateConfig()
    bad_worst = {"stretch_score": 0.17, "worst_island_distortion": 0.87, "worst_island_id": 18,
                 "mandatory_90_uv_unsplit": 0}
    msg = _island_conclusion(27, 28, [{"action": "split", "reason": "worst_island_distortion"}],
                             bad_worst, cfg)
    assert "GLOBAL checker distortion passed" in msg
    assert "WORST island 18" in msg and "0.870" in msg


def test_user_seam_conclusion_hides_mandatory_diagnostics_when_gate_disabled():
    class _Usr:
        conflicts = []
        invalid_edges = []

    cfg = ChartGateConfig()
    metrics = {
        "mandatory_90_missing": 85,
        "mandatory_90_uv_unsplit": 85,
        "raster_overlap_ratio": 0.0,
        "worst_island_distortion": 0.2,
    }
    msg = _user_seam_conclusion(
        _Usr(), metrics, cfg, auto_added=0, auto_refine=False,
        include_mandatory_diagnostics=False,
    )
    assert "mandatory" not in msg
    assert "all hard gates pass" in msg


# -- Rule 2 at the UV LEVEL: a fold welded in the exported UV must be caught --------------

def _xy_uvmap(mesh):
    """UV == XY of each loop's vertex → adjacent coplanar faces weld, perpendicular ones
    (a fold in z) get the SAME u,v on the shared edge too (z is dropped) → welded across
    the fold. Lets us build a 'fold not UV-split' layout without Blender."""
    uvmap = UVMap.for_mesh(mesh)
    for f in mesh.faces:
        for li in f.loop_indices:
            x, y, _z = mesh.vertices[mesh.loops[li].vertex_id].co
            uvmap.set(li, x, y)
    return uvmap


def test_uv_audit_detects_welded_fold():
    # Project the folded planes to XY: the vertical half collapses onto the horizontal one,
    # so the 90° fold edges carry identical UV on both sides → unsplit.
    mesh = build_folded_planes(n=5)
    audit = mandatory_seam_uv_audit(mesh, _xy_uvmap(mesh))
    assert audit["mandatory_90_fold_edges"] > 0
    assert audit["mandatory_90_uv_unsplit"] == audit["mandatory_90_fold_edges"]  # all welded


def test_uv_audit_clean_when_each_face_has_unique_uv():
    # Give every face its own UV block → no two faces weld anywhere → 0 unsplit.
    mesh = build_folded_planes(n=5)
    uvmap = UVMap.for_mesh(mesh)
    for f in mesh.faces:
        for li in f.loop_indices:
            uvmap.set(li, f.id * 10.0, f.id * 10.0)
    audit = mandatory_seam_uv_audit(mesh, uvmap)
    assert audit["mandatory_90_fold_edges"] > 0
    assert audit["mandatory_90_uv_unsplit"] == 0


def test_uv_unsplit_is_a_hard_gate():
    gate = evaluate_chart_gate({**_GOOD, "mandatory_90_uv_unsplit": 5}, config=ChartGateConfig())
    assert not gate.passed
    assert "mandatory_90_uv_unsplit" in [c.name for c in gate.failures]


def test_island_count_is_advisory_so_rule2_is_never_blocked():
    # A creased mesh can need more islands than the soft cap to cut every fold; island_count
    # must NOT block that (it is advisory). All hard rules met → ships even way over the cap.
    cfg = ChartGateConfig()
    gate = evaluate_chart_gate({**_GOOD, "island_count": cfg.island_count_max + 50},
                               config=cfg)
    assert gate.passed
    assert "island_count" in [c.name for c in gate.advisories]
    assert "island_count" not in [c.name for c in gate.failures]


# -- cut-cost function: creases cheap, flats expensive, preserve set forbidden -----------

def test_edge_cut_cost_ranks_creases_below_flats_and_forbids():
    mesh = build_capsule_with_spikes()
    two = sorted((e.id for e in mesh.edges if len(e.face_ids) == 2),
                 key=lambda i: mesh.edges[i].dihedral_angle)
    flat_e, sharp_e = two[0], two[-1]
    assert edge_cut_cost(mesh, sharp_e) < edge_cut_cost(mesh, flat_e)   # crease is cheaper
    assert edge_cut_cost(mesh, flat_e, forbidden={flat_e}) == float("inf")  # preserve = blocked
    # a ≥90° fold is essentially free (prefer routing along real creases)
    folds = [e.id for e in mesh.edges if e.dihedral_angle >= 90 and len(e.face_ids) == 2]
    if folds:
        assert edge_cut_cost(mesh, folds[0]) < 0.1


def test_forbidden_edges_are_never_cut():
    # The user's preserve set (e.g. Blender-selected edge 3054) must never be traversed by a
    # cut path nor introduced as a seam.
    mesh = build_capsule_with_spikes(n_spikes=5, spike_len=1.8)
    seams = mandatory_seam_edges(mesh)
    interior = interior_fold_edges(mesh, seams)
    forb = {e.id for e in mesh.edges if e.dihedral_angle < 40 and len(e.face_ids) == 2}
    forb = set(list(forb)[:60])
    r = split_welded_folds(mesh, seams, interior, forbidden=forb)
    assert forb.isdisjoint(r["added"])          # never added as an auxiliary seam
    assert forb.isdisjoint(seams)               # and never present in the seam set at all


# -- post-pass pruning of unnecessary auxiliary seams ------------------------------------

def test_prune_seams_drops_unneeded_keeps_load_bearing():
    seams = {1, 2, 3, 4}

    def accept():            # edge 2 is load-bearing: removing it must be rejected
        return 2 in seams
    removed = _prune_seams(seams, [1, 2, 3], accept)
    assert set(removed) == {1, 3}    # 1 and 3 pruned
    assert 2 in seams                # 2 reverted (kept)
    assert seams == {2, 4}


# -- seam origin/type tagging ------------------------------------------------------------

def test_classify_seams_tags_origins():
    mesh = build_capsule_with_spikes()
    folds = [e.id for e in mesh.edges if e.dihedral_angle >= 90 and len(e.face_ids) == 2][:2]
    assert len(folds) == 2
    flat = next(e.id for e in mesh.edges if e.dihedral_angle < 90 and len(e.face_ids) == 2)
    seams = set(folds) | {flat}
    types = _classify_seams(mesh, seams, aux_seams={flat}, overlap_seams=set(),
                            distortion_seams=set(), forbidden=set())
    for f in folds:
        assert types[f] == "mandatory_90"        # a ≥90° fold is always mandatory_90
    assert types[flat] == "welded_fold_auxiliary"
    counts = _count_types(types)
    assert counts["mandatory_90"] == 2 and counts["welded_fold_auxiliary"] == 1


def test_classify_seams_mandatory_outranks_aux_even_if_in_aux_set():
    mesh = build_capsule_with_spikes()
    fold = next(e.id for e in mesh.edges if e.dihedral_angle >= 90 and len(e.face_ids) == 2)
    # even if a fold somehow appears in aux_seams, it must classify as mandatory_90 (never prunable)
    types = _classify_seams(mesh, {fold}, aux_seams={fold}, overlap_seams=set(),
                            distortion_seams=set(), forbidden=set())
    assert types[fold] == "mandatory_90"


# -- fold-boundary repair: LOCAL min-cost cut, not chart-wide VSA -------------------------

def test_split_welded_folds_separates_with_a_local_cut():
    # Given the folds that welded in the UV, the LOCAL cut path must separate each fold's two
    # faces into different charts (so the next unwrap cuts it) without dropping a mandatory
    # fold. No chart-wide VSA needed on this fixture (fallback == 0).
    mesh = build_capsule_with_spikes(n_spikes=5, spike_len=1.8)
    seams = mandatory_seam_edges(mesh)
    interior = interior_fold_edges(mesh, seams)
    assert interior
    welded = interior[:3]
    required = set(mandatory_seam_edges(mesh))
    r = split_welded_folds(mesh, seams, welded)
    assert r["local_cuts"] + r["fallback"] >= 1
    # every requested fold is now a real chart boundary (its faces separated)
    for eid in welded:
        a, b = mesh.edges[eid].face_ids
        fc = {f: i for i, fs in enumerate(flood_charts(mesh, seams)) for f in fs}
        assert fc[a] != fc[b]
    assert required.issubset(seams)                  # never drops a mandatory fold


def test_split_welded_folds_prefers_creases_over_flat_edges():
    # The added auxiliary cut edges should skew toward sharp creases, not the flat connective
    # edges the user wants preserved: their mean dihedral must beat a random interior edge's.
    import numpy as np
    mesh = build_capsule_with_spikes(n_spikes=5, spike_len=1.8)
    base = set(mandatory_seam_edges(mesh))
    interior_all = [e.dihedral_angle for e in mesh.edges
                    if len(e.face_ids) == 2 and e.id not in base]
    r = split_welded_folds(mesh, set(base), interior_fold_edges(mesh, base))
    aux = [mesh.edges[e].dihedral_angle for e in r["added"]]
    assert aux
    assert float(np.mean(aux)) > 1.3 * float(np.mean(interior_all))  # routes along sharper edges


def test_enforce_fold_boundaries_clears_all_interior_folds():
    # The full (aggressive) utility still works for callers that want every fold a boundary;
    # it drives interior_fold_edges to zero and only ever ADDS seams.
    mesh = build_capsule_with_spikes(n_spikes=5, spike_len=1.8)
    seams = mandatory_seam_edges(mesh)
    before_interior = len(interior_fold_edges(mesh, seams))
    required = set(mandatory_seam_edges(mesh))
    n = enforce_fold_boundaries(mesh, seams)
    assert len(interior_fold_edges(mesh, seams)) == 0
    assert required.issubset(seams)
    if before_interior:
        assert n > 0
