"""Tests for the MVP 3 generate contract (plan §4, §5, §9, Session A).

Pure-Python: these load ``worker/app_uv_generate_contract.py`` stand-alone (no
Blender) and exercise the strict-option defaults, metric flattening, summary +
artifact builders, and the status lifecycle helpers.
"""

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_contract():
    path = os.path.join(_ROOT, "worker", "app_uv_generate_contract.py")
    spec = importlib.util.spec_from_file_location("app_uv_generate_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contract = _load_contract()


# --- strict defaults (plan §1) ---------------------------------------------
def test_default_options_are_strict():
    opts = contract.default_options()
    assert opts["auto_refine_user_seams"] is False
    assert opts["repair_user_seams"] is False
    assert opts["enforce_user_mandatory"] is False
    assert opts["gate_user_mandatory"] is False
    assert opts["optimize_layout"] is True
    assert opts["layout_opt_preset"] == "user_reference"
    assert opts["layout_opt_max_candidates"] == 24


def test_default_options_is_a_copy():
    a = contract.default_options()
    a["optimize_layout"] = False
    assert contract.default_options()["optimize_layout"] is True


def test_merge_options_overlays_and_keeps_strict_defaults():
    merged = contract.merge_options({"layout_opt_max_candidates": 8})
    assert merged["layout_opt_max_candidates"] == 8
    # untouched strict flags stay false
    assert merged["repair_user_seams"] is False
    # a caller CAN flip a flag — it is recorded faithfully (and fails integrity)
    merged2 = contract.merge_options({"repair_user_seams": True})
    assert merged2["repair_user_seams"] is True


def test_merge_options_none_is_fully_strict():
    assert contract.merge_options(None) == contract.default_options()


# --- metric flattening (plan §4.1, §5) -------------------------------------
def _full_metrics():
    return {
        "stretch_score": 0.0686612,
        "worst_island_distortion": 0.2029991,
        "raster_overlap_ratio": 0.0,
        "overlap_ratio": 0.0,
        "texel_density_variance": 0.0000021,
        "packing_efficiency": 0.5912781,
        "island_count": 52,
        "uv_bounds_ok": True,
        # engine-internal keys that must NOT leak into the summary subset
        "convexity_p10": 0.5,
        "mandatory_90_missing": 3,
        "vt_count": 999,
    }


def test_flatten_summary_metrics_keeps_only_the_eight():
    m = contract.flatten_summary_metrics(_full_metrics())
    assert set(m.keys()) == set(contract.SUMMARY_METRIC_KEYS)
    assert m["island_count"] == 52 and isinstance(m["island_count"], int)
    assert m["uv_bounds_ok"] is True
    assert m["stretch_score"] == round(0.0686612, 6)


def test_flatten_candidate_metrics_excludes_island_count():
    m = contract.flatten_candidate_metrics(_full_metrics())
    assert "island_count" not in m
    assert set(m.keys()) == set(contract.CANDIDATE_METRIC_KEYS)


def test_flatten_handles_none_and_missing():
    assert contract.flatten_summary_metrics(None) == {}
    assert contract.flatten_summary_metrics({"stretch_score": 0.1}) == {"stretch_score": 0.1}


# --- layout optimization block (plan §4.1) ---------------------------------
def test_build_layout_optimization_block_disabled_when_empty():
    assert contract.build_layout_optimization_block(None) == {"enabled": False}
    assert contract.build_layout_optimization_block({}) == {"enabled": False}


def test_build_layout_optimization_block_flattens_before_after():
    report = {
        "selected_candidate_id": "slim_concave_m002",
        "kept_baseline": False,
        "score_before": -0.0031,
        "score_after": -0.003276,
        "candidates": [{"id": "a"}, {"id": "b"}],
        "before_metrics": {"packing_efficiency": 0.583109, "stretch_score": 0.06866},
        "after_metrics": {"packing_efficiency": 0.591278, "stretch_score": 0.06866},
    }
    blk = contract.build_layout_optimization_block(report)
    assert blk["enabled"] is True
    assert blk["selected_candidate_id"] == "slim_concave_m002"
    assert blk["candidate_count"] == 2
    assert blk["packing_efficiency_before"] == 0.583109
    assert blk["packing_efficiency_after"] == 0.591278
    assert blk["stretch_before"] == 0.06866
    # MVP3 §2 Goal C/D: the block carries an honest improvement + verdict. The §0 pottery
    # run only nudged packing 0.583 -> 0.591 (well under the 0.05 bar) and stays below the
    # 0.65 target, so it is NOT meaningful and the verdict asks for better packing.
    assert blk["improvement"]["meaningful"] is False
    assert abs(blk["improvement"]["packing_delta"] - 0.008169) < 1e-6
    assert blk["verdict"] == "needs_better_packing"
    assert blk["texel_density_before"] is None or isinstance(blk["texel_density_before"], (int, float))


def test_compute_improvement_meaningful_when_packing_jumps_past_target():
    before = {"packing_efficiency": 0.58, "stretch_score": 0.07, "texel_density_variance": 0.01}
    after = {"packing_efficiency": 0.70, "stretch_score": 0.069, "texel_density_variance": 0.01}
    imp = contract.compute_improvement(before, after, -0.5, -0.9)
    assert imp["meaningful"] is True
    v = contract.improvement_verdict(imp, kept_baseline=False, packing_after=0.70)
    assert v == "meaningful"


def test_improvement_verdict_keeps_baseline_when_good_and_unchanged():
    imp = contract.compute_improvement(
        {"packing_efficiency": 0.7}, {"packing_efficiency": 0.7}, -1.0, -1.0)
    assert contract.improvement_verdict(imp, kept_baseline=True, packing_after=0.7) == "baseline_retained"


# --- summary builder (plan §4.1) -------------------------------------------
def test_build_generate_summary_shape():
    integrity = {"user_seam_count": 1230, "user_protected_count": 0, "final_seam_count": 1230,
                 "auto_added_seams": 0, "mandatory_rule_enabled": False,
                 "mandatory_gate_enabled": False, "valid": True}
    summary = contract.build_generate_summary(
        run_id="uv_run_x", status=contract.STATUS_ACCEPTED,
        model="work/working_lowpoly.blend", object_name="Pot",
        seam_spec="work/seams/user_seam_spec.json", metrics=_full_metrics(),
        seam_integrity=integrity,
        layout_optimization={"enabled": True, "kept_baseline": False},
        artifacts={"summary": "uv_generate_summary.json"},
        selected_candidate_id="slim_concave_m002",
        selected_uv_model="work/uv/selected_uv.blend", warnings=[])
    assert summary["schema_version"] == contract.SCHEMA_VERSION
    assert summary["command"] == contract.CMD_GENERATE_UV_FROM_SEAMS
    assert summary["status"] == "accepted"
    assert summary["selected_candidate_id"] == "slim_concave_m002"
    assert summary["selected_uv_model"] == "work/uv/selected_uv.blend"
    # metrics flattened to the eight; raw keys not present
    assert set(summary["metrics"].keys()) == set(contract.SUMMARY_METRIC_KEYS)
    assert summary["seam_integrity"]["valid"] is True


# --- status lifecycle (plan §9) --------------------------------------------
def test_new_and_finalize_status():
    st = contract.new_status(run_id="uv_run_x", input={"model": "m"})
    assert st["status"] == contract.STATUS_QUEUED
    assert st["finished_at"] is None
    contract.finalize_status(st, status=contract.STATUS_NEEDS_USER_REVIEW,
                             artifacts={"summary": "uv_generate_summary.json"})
    assert st["status"] == "needs_user_review"
    assert st["finished_at"] is not None
    assert st["artifacts"]["summary"] == "uv_generate_summary.json"


def test_error_envelope_is_json_safe():
    env = contract.error_envelope(contract.CMD_GENERATE_UV_FROM_SEAMS, "boom",
                                  code="invalid_seam_spec", details={"invalid_edges": [999999]})
    assert env["status"] == contract.STATUS_FAILED
    assert env["error"]["code"] == "invalid_seam_spec"
    assert env["details"]["invalid_edges"] == [999999]


def test_needs_user_review_is_terminal():
    assert contract.STATUS_NEEDS_USER_REVIEW in contract.TERMINAL_STATUSES
    assert contract.STATUS_RUNNING not in contract.TERMINAL_STATUSES


# --- artifact collection (plan §4.1, §7) -----------------------------------
# --- seam source resolution (UV-boundary-fallback revision plan §1, §4, §6) -
def test_needs_input_is_a_terminal_status():
    assert contract.STATUS_NEEDS_INPUT == "needs_input"
    assert contract.STATUS_NEEDS_INPUT in contract.STATUSES
    assert contract.STATUS_NEEDS_INPUT in contract.TERMINAL_STATUSES


def test_decide_seam_source_prefers_explicit_spec_over_uv_layer():
    # An explicit spec FILE wins even when a UV layer is also available (§6.1, §7).
    d = contract.decide_seam_source(
        seam_spec_path="/p/user_seam_spec.json", seam_spec_exists=True, uv_layer="UVChannel_1")
    assert d["kind"] == contract.SEAM_SOURCE_USER_SPEC
    assert d["error"] is None


def test_decide_seam_source_falls_back_to_uv_boundary():
    # Missing spec + a valid UV layer -> derived (§1 case 2).
    d = contract.decide_seam_source(
        seam_spec_path=None, seam_spec_exists=False, uv_layer="UVChannel_1")
    assert d["kind"] == contract.SEAM_SOURCE_UV_BOUNDARY
    assert d["uv_layer"] == "UVChannel_1"
    assert d["error"] is None
    # A configured-but-missing spec file also falls back to the UV boundary.
    d2 = contract.decide_seam_source(
        seam_spec_path="/p/gone.json", seam_spec_exists=False, uv_layer="UVMap")
    assert d2["kind"] == contract.SEAM_SOURCE_UV_BOUNDARY


def test_decide_seam_source_needs_input_when_nothing_available():
    d = contract.decide_seam_source(seam_spec_path=None, seam_spec_exists=False, uv_layer=None)
    assert d["kind"] == contract.STATUS_NEEDS_INPUT
    assert d["error"]["code"] == contract.MISSING_SEAM_SOURCE_CODE
    assert d["error"]["message"] == contract.MISSING_SEAM_SOURCE_MESSAGE


def test_build_seam_source_blocks():
    explicit = contract.build_seam_source(
        source_type=contract.SEAM_SOURCE_USER_SPEC, path="work/seams/user_seam_spec.json",
        uv_layer=None, user_confirmed=True, derived=False)
    assert explicit == {
        "type": "user_seam_spec", "path": "work/seams/user_seam_spec.json",
        "uv_layer": None, "user_confirmed": True, "derived": False,
    }
    derived = contract.build_seam_source(
        source_type=contract.SEAM_SOURCE_UV_BOUNDARY,
        path="work/seams/derived_from_uv_boundary.json", uv_layer="UVChannel_1",
        user_confirmed=False, derived=True)
    assert derived["type"] == "uv_boundary_derived"
    assert derived["user_confirmed"] is False and derived["derived"] is True


def test_make_derived_seam_spec_is_canonical_and_protected_empty():
    spec = contract.make_derived_seam_spec(
        object_name="Pot", user_seam_edges=[3, 1, 2, 1], uv_layer="UVChannel_1")
    assert spec["object"] == "Pot"
    assert spec["mode"] == "user_seams"
    assert spec["user_seam_edges"] == [1, 2, 3]  # sorted + de-duped
    assert spec["user_protected_edges"] == []  # revision plan §6.1
    assert "UVChannel_1" in spec["notes"]


def test_make_derived_seam_spec_loads_via_user_seam_spec():
    # Derived spec round-trips through UserSeamSpec.from_dict (revision plan §6.1).
    from artist_uv_agent.user_seams import UserSeamSpec

    spec = contract.make_derived_seam_spec(
        object_name="Pot", user_seam_edges=[5, 9], uv_layer="UVMap")
    loaded = UserSeamSpec.from_dict(spec)
    assert loaded.user_seam_edges == {5, 9}
    assert loaded.user_protected_edges == set()
    assert loaded.mode == "user_seams"


def test_build_generate_summary_includes_seam_source():
    integrity = {"user_seam_count": 10, "user_protected_count": 0, "final_seam_count": 10,
                 "auto_added_seams": 0, "mandatory_rule_enabled": False,
                 "mandatory_gate_enabled": False, "valid": True}
    seam_source = contract.build_seam_source(
        source_type=contract.SEAM_SOURCE_UV_BOUNDARY,
        path="work/seams/derived_from_uv_boundary.json", uv_layer="UVChannel_1",
        user_confirmed=False, derived=True)
    summary = contract.build_generate_summary(
        run_id="r", status=contract.STATUS_ACCEPTED, model="m", object_name="Pot",
        seam_spec="work/seams/derived_from_uv_boundary.json", seam_source=seam_source,
        metrics=_full_metrics(), seam_integrity=integrity,
        layout_optimization={"enabled": True}, artifacts={"summary": "uv_generate_summary.json"})
    assert summary["seam_source"]["type"] == "uv_boundary_derived"
    assert summary["seam_source"]["derived"] is True
    # Legacy callers that omit seam_source still get a (null) key, never a KeyError.
    legacy = contract.build_generate_summary(
        run_id="r", status=contract.STATUS_ACCEPTED, model="m", object_name="Pot",
        seam_spec=None, metrics=None, seam_integrity=integrity,
        layout_optimization={"enabled": False}, artifacts={})
    assert legacy["seam_source"] is None


def test_collect_generate_artifacts_present_and_missing(tmp_path):
    d = str(tmp_path)
    # write the six required previews + the optional reports, omit one preview
    for name in ("baseline_uv_layout.png", "baseline_checker_front.png",
                 "baseline_checker_side.png", "selected_uv_layout.png",
                 "selected_checker_front.png", "p5_gate.json", "candidate_summary.json",
                 "selected_uv.blend"):
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write("x")
    artifacts, warnings = contract.collect_generate_artifacts(d)
    # summary is always listed (the summary names itself, plan §4.1)
    assert artifacts["summary"] == contract.SUMMARY_FILE
    assert artifacts["baseline_uv_layout"] == "baseline_uv_layout.png"
    assert artifacts["selected_blend"] == "selected_uv.blend"
    # the one missing required preview becomes a warning, not a failure (plan §13)
    assert any("selected_checker_side.png" in w for w in warnings)
    assert "selected_checker_side" not in artifacts
