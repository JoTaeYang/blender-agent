"""Tests for the MVP 1 UV review contract (plan §5, §6, §9, Session B).

Pure-Python: these load ``worker/app_uv_review_contract.py`` stand-alone (no
Blender) and exercise the review-status classifier, summary builders, and the
status lifecycle / artifact collection helpers.
"""

import importlib.util
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_contract():
    path = os.path.join(_ROOT, "worker", "app_uv_review_contract.py")
    spec = importlib.util.spec_from_file_location("app_uv_review_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contract = _load_contract()


def _clean_metrics():
    return {
        "stretch_score": 0.06,
        "worst_island_distortion": 0.2,
        "overlap_ratio": 0.0,
        "raster_overlap_ratio": 0.0,
        "self_overlap_ratio": 0.0,
        "cross_overlap_ratio": 0.0,
        "texel_density_variance": 0.000002,
        "packing_efficiency": 0.59,
    }


def _clean_uv():
    return {
        "island_count": 43,
        "uv_bounds": {"min": [0.001, 0.002], "max": [0.998, 0.997], "in_0_1": True},
        "has_negative_uv": False,
        "has_out_of_tile_uv": False,
    }


def test_classify_clean():
    status, issues = contract.classify_review(_clean_metrics(), _clean_uv())
    assert status == contract.REVIEW_CLEAN
    assert issues == []


def test_classify_no_uv_when_metrics_none():
    status, issues = contract.classify_review(None, None)
    assert status == contract.REVIEW_NO_UV
    assert issues == []


def test_classify_raster_overlap_is_error_and_headline():
    m = _clean_metrics()
    m["raster_overlap_ratio"] = 0.012
    status, issues = contract.classify_review(m, _clean_uv())
    assert status == contract.REVIEW_HAS_OVERLAP
    issue = next(i for i in issues if i["code"] == "raster_overlap")
    assert issue["severity"] == contract.SEVERITY_ERROR
    assert issue["metric"] == "raster_overlap_ratio"
    assert issue["value"] == 0.012


def test_classify_out_of_bounds_from_uv_block():
    uv = _clean_uv()
    uv["has_out_of_tile_uv"] = True
    uv["uv_bounds"]["in_0_1"] = False
    status, issues = contract.classify_review(_clean_metrics(), uv)
    assert status == contract.REVIEW_OUT_OF_BOUNDS
    assert any(i["code"] == "out_of_bounds" for i in issues)


def test_classify_high_stretch_and_density():
    m = _clean_metrics()
    m["stretch_score"] = 0.9
    m["texel_density_variance"] = 1.2
    status, issues = contract.classify_review(m, _clean_uv())
    # Both flagged, but overlap-free so the headline is the higher-priority stretch.
    codes = {i["code"] for i in issues}
    assert codes == {"high_stretch", "density_variance"}
    assert status == contract.REVIEW_HIGH_STRETCH


def test_classify_priority_overlap_beats_everything():
    m = _clean_metrics()
    m["raster_overlap_ratio"] = 0.5
    m["stretch_score"] = 0.9
    uv = _clean_uv()
    uv["has_negative_uv"] = True
    status, issues = contract.classify_review(m, uv)
    assert status == contract.REVIEW_HAS_OVERLAP
    assert len(issues) >= 3


def test_build_review_summary_shape():
    status, issues = contract.classify_review(_clean_metrics(), _clean_uv())
    summary = contract.build_review_summary(
        run_id="review_run_1",
        model="work/working_lowpoly.blend",
        object_name="SM_Test_Pottery_a_02",
        uv_layer="UVChannel_1",
        mesh={"vertices": 6562, "edges": 18701, "faces": 12152, "loops": 36396},
        uv=_clean_uv(),
        metrics=_clean_metrics(),
        artifacts={"uv_layout": "uv_layout.png"},
        review_status=status,
        issues=issues,
    )
    assert summary["schema_version"] == 1
    assert summary["status"] == contract.STATUS_ACCEPTED
    assert summary["command"] == contract.CMD_REVIEW_EXISTING_UV
    assert summary["review_status"] == contract.REVIEW_CLEAN
    for key in contract.REQUIRED_METRICS:
        assert key in summary["metrics"], key
    json.loads(json.dumps(summary))  # serializable


def test_no_uv_summary_shape():
    summary = contract.no_uv_summary(
        run_id="review_run_2", model="work/m.blend", object_name="Lowpoly")
    assert summary["status"] == contract.STATUS_NO_UV
    assert summary["review_status"] == contract.REVIEW_NO_UV
    assert summary["metrics"] is None
    assert summary["uv_layer"] is None
    assert summary["warnings"]
    json.loads(json.dumps(summary))


def test_status_lifecycle():
    status = contract.new_status(run_id="r1", command=contract.CMD_REVIEW_EXISTING_UV)
    assert status["status"] == contract.STATUS_QUEUED
    assert status["finished_at"] is None
    contract.finalize_status(status, status=contract.STATUS_NO_UV,
                             artifacts={}, error=None)
    assert status["status"] == contract.STATUS_NO_UV
    assert status["finished_at"] is not None


def test_collect_review_artifacts_partial(tmp_path):
    run_dir = str(tmp_path)
    for f in ("uv_review_summary.json", "uv_metrics.json", "uv_layout.png", "checker_front.png"):
        open(os.path.join(run_dir, f), "w").close()
    artifacts, warnings = contract.collect_review_artifacts(run_dir)
    assert artifacts["uv_layout"] == "uv_layout.png"
    assert artifacts["checker_front"] == "checker_front.png"
    # checker_side.png is required but absent -> warning.
    assert any("checker_side.png" in w for w in warnings)


def test_error_envelope_is_json():
    env = contract.error_envelope(
        contract.CMD_REVIEW_EXISTING_UV, "boom", code="exception", run_id="r")
    assert env["status"] == contract.STATUS_FAILED
    assert env["error"]["code"] == "exception"
    assert env["run_id"] == "r"
    json.loads(json.dumps(env))
