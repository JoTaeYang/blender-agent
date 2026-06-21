"""Tests for the MVP 3 seam-integrity acceptance (plan §6, Session B).

Seam integrity is the MVP 3 HARD acceptance: a strict user/reference run must
ship the user's exact seam set — ``auto_added_seams == 0`` and
``final_seam_count == user_seam_count`` — with every strict flag off and no
invalid edge / object mismatch / protected leak (plan §6). A run that breaks any
of these is ``needs_user_review`` and must not ship (plan §6, §13).

Pure-Python: loads ``worker/app_uv_generate_contract.py`` stand-alone.
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


def _user_seams(*, user_seam_count=1230, final_seam_count=1230, auto_added=0,
                seam_edges=None, protected_edges=None, invalid=None):
    seam_edges = list(range(user_seam_count)) if seam_edges is None else seam_edges
    return {
        "user_seam_count": user_seam_count,
        "user_protected_count": len(protected_edges or []),
        "final_seam_count": final_seam_count,
        "auto_added_seams": auto_added,
        "user_seam_edges": seam_edges,
        "user_protected_edges": protected_edges or [],
        "invalid_edges": invalid or [],
    }


# --- clean strict run (plan §6 happy path) ---------------------------------
def test_clean_strict_run_is_valid():
    us = _user_seams(seam_edges=[1, 2, 3], user_seam_count=3, final_seam_count=3)
    r = contract.evaluate_seam_integrity(us, contract.default_options(), final_seams=[1, 2, 3])
    assert r["valid"] is True
    assert r["violations"] == []
    blk = r["block"]
    assert blk["user_seam_count"] == 3
    assert blk["final_seam_count"] == 3
    assert blk["auto_added_seams"] == 0
    assert blk["mandatory_rule_enabled"] is False
    assert blk["mandatory_gate_enabled"] is False
    assert blk["valid"] is True


# --- UV-boundary-derived path (revision plan §7 integrity) -----------------
def test_derived_boundary_path_preserves_integrity():
    # A UV-boundary-derived run ships the exact derived edge set with NO protect
    # intent (user_protected_count == 0); seam integrity must still hold — the
    # strict acceptance is identical regardless of seam source (revision plan §7).
    us = _user_seams(seam_edges=[10, 20, 30], user_seam_count=3, final_seam_count=3,
                     protected_edges=[])
    r = contract.evaluate_seam_integrity(us, contract.default_options(), final_seams=[10, 20, 30])
    assert r["valid"] is True
    assert r["block"]["user_protected_count"] == 0
    assert r["block"]["auto_added_seams"] == 0
    assert r["block"]["final_seam_count"] == r["block"]["user_seam_count"]


# --- seam set changed (plan §6 hard fails) ---------------------------------
def test_auto_added_seams_breaks_integrity():
    us = _user_seams(seam_edges=[1, 2, 3], user_seam_count=3, final_seam_count=5, auto_added=2)
    r = contract.evaluate_seam_integrity(us, contract.default_options(), final_seams=[1, 2, 3, 4, 5])
    assert r["valid"] is False
    codes = {v["code"] for v in r["violations"]}
    assert "auto_added_seams" in codes
    assert "seam_count_changed" in codes


def test_seam_count_changed_alone_breaks_integrity():
    us = _user_seams(seam_edges=[1, 2, 3], user_seam_count=3, final_seam_count=2)
    r = contract.evaluate_seam_integrity(us, contract.default_options())
    assert r["valid"] is False
    assert any(v["code"] == "seam_count_changed" for v in r["violations"])


# --- strict flags (plan §6) ------------------------------------------------
def test_each_strict_flag_breaks_integrity():
    for flag in contract.STRICT_FLAGS:
        opts = contract.default_options()
        opts[flag] = True
        r = contract.evaluate_seam_integrity(_user_seams(seam_edges=[1], user_seam_count=1,
                                                         final_seam_count=1),
                                             opts, final_seams=[1])
        assert r["valid"] is False, flag
        assert any(v.get("flag") == flag for v in r["violations"]), flag


def test_enforce_and_gate_flags_reflected_in_block():
    opts = contract.default_options()
    opts["enforce_user_mandatory"] = True
    opts["gate_user_mandatory"] = True
    r = contract.evaluate_seam_integrity(_user_seams(seam_edges=[1], user_seam_count=1,
                                                     final_seam_count=1), opts, final_seams=[1])
    assert r["block"]["mandatory_rule_enabled"] is True
    assert r["block"]["mandatory_gate_enabled"] is True


# --- invalid edges / object mismatch (plan §6) -----------------------------
def test_invalid_edges_break_integrity():
    us = _user_seams(seam_edges=[1, 2], user_seam_count=2, final_seam_count=2, invalid=[999999])
    r = contract.evaluate_seam_integrity(us, contract.default_options(), final_seams=[1, 2])
    assert r["valid"] is False
    assert any(v["code"] == "invalid_edges" for v in r["violations"])


def test_object_mismatch_breaks_integrity():
    us = _user_seams(seam_edges=[1], user_seam_count=1, final_seam_count=1)
    r = contract.evaluate_seam_integrity(us, contract.default_options(),
                                         final_seams=[1], object_mismatch=True)
    assert r["valid"] is False
    assert any(v["code"] == "object_mismatch" for v in r["violations"])


# --- protected edge leakage (plan §6) --------------------------------------
def test_protected_non_seam_edge_shipping_breaks_integrity():
    # edge 7 is protected (and NOT a user seam) yet appears in the final seam set.
    us = _user_seams(seam_edges=[1, 2], user_seam_count=2, final_seam_count=2,
                     protected_edges=[7])
    r = contract.evaluate_seam_integrity(us, contract.default_options(), final_seams=[1, 2, 7])
    assert r["valid"] is False
    leak = next(v for v in r["violations"] if v["code"] == "protected_edge_shipped")
    assert leak["edges"] == [7]


def test_protected_edge_that_is_also_a_user_seam_is_not_a_leak():
    # edge 2 is both protected and a user seam → seam wins, no leak (plan §6).
    us = _user_seams(seam_edges=[1, 2], user_seam_count=2, final_seam_count=2,
                     protected_edges=[2])
    r = contract.evaluate_seam_integrity(us, contract.default_options(), final_seams=[1, 2])
    assert r["valid"] is True


def test_protected_leak_check_skipped_without_final_seams():
    us = _user_seams(seam_edges=[1, 2], user_seam_count=2, final_seam_count=2,
                     protected_edges=[7])
    r = contract.evaluate_seam_integrity(us, contract.default_options())  # no final_seams
    assert not any(v["code"] == "protected_edge_shipped" for v in r["violations"])


# --- layout quality + status classification (plan §6, §13) -----------------
def test_layout_quality_clean():
    q = contract.evaluate_layout_quality(
        {"raster_overlap_ratio": 0.0, "overlap_ratio": 0.0, "uv_bounds_ok": True})
    assert q["ok"] is True and q["issues"] == []


def test_layout_quality_flags_raster_overlap():
    q = contract.evaluate_layout_quality(
        {"raster_overlap_ratio": 0.02, "overlap_ratio": 0.0, "uv_bounds_ok": True})
    assert q["ok"] is False
    assert any(i["code"] == "raster_overlap" for i in q["issues"])


def test_layout_quality_flags_out_of_bounds():
    q = contract.evaluate_layout_quality(
        {"raster_overlap_ratio": 0.0, "overlap_ratio": 0.0, "uv_bounds_ok": False})
    assert q["ok"] is False
    assert any(i["code"] == "uv_out_of_bounds" for i in q["issues"])


def test_classify_status_accepted_only_when_both_hold():
    good_integrity = {"valid": True}
    bad_integrity = {"valid": False}
    good_quality = {"ok": True}
    bad_quality = {"ok": False}
    assert contract.classify_generate_status(good_integrity, good_quality) == contract.STATUS_ACCEPTED
    assert contract.classify_generate_status(bad_integrity, good_quality) == contract.STATUS_NEEDS_USER_REVIEW
    assert contract.classify_generate_status(good_integrity, bad_quality) == contract.STATUS_NEEDS_USER_REVIEW
