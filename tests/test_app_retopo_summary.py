"""Tests for summary normalization + status lifecycle (plan §5.2, Session B).

These run without Blender by writing fixture per-phase reports into a temp run
folder and exercising the pure-Python normalizer.
"""

import importlib.util
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_contract():
    path = os.path.join(_ROOT, "worker", "app_job_contract.py")
    spec = importlib.util.spec_from_file_location("app_job_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contract = _load_contract()


def _write(run_dir, name, data):
    with open(os.path.join(run_dir, name), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def test_normalize_summary_full(tmp_path):
    run_dir = str(tmp_path)
    _write(run_dir, "generation_report.json", {
        "object_name": "Pot", "result_object_name": "Pot_low", "method": "decimate_collapse",
        "source_face_count": 121520, "target_face_count": 12000, "actual_face_count": 12152,
        "target_error_ratio": 0.0127, "band": "accepted",
    })
    _write(run_dir, "validation_report.json", {
        "status": "accepted", "quad_ratio": 0.0, "triangle_ratio": 1.0,
        "ngon_count": 0, "non_manifold_edge_count": 0,
    })
    _write(run_dir, "shape_report.json", {
        "status": "accepted", "surface_distance_mean_ratio": 0.002,
        "surface_distance_max_ratio": 0.01, "normal_deviation_mean_deg": 4.2,
        "volume_error_ratio": 0.003,
    })
    # Touch artifact files so collect_artifacts finds them.
    for f in ("lowpoly.blend", "lowpoly.fbx", "preview.png"):
        open(os.path.join(run_dir, f), "w").close()

    summary = contract.normalize_summary(run_dir, run_id="run1", object_name="Pot")
    assert summary["schema_version"] == 1
    assert summary["metrics"]["target_faces"] == 12000
    assert summary["metrics"]["actual_faces"] == 12152
    assert summary["metrics"]["source_faces"] == 121520
    assert summary["reports"] == {"generation": "accepted", "validation": "accepted", "shape": "accepted"}
    assert summary["artifacts"]["lowpoly_blend"] == "lowpoly.blend"
    assert summary["artifacts"]["preview"] == "preview.png"
    assert summary["warnings"] == []


def test_normalize_summary_target_actual_always_present(tmp_path):
    # No generation report -> falls back to the explicit target_faces input,
    # and the missing required artifacts surface as warnings (plan §5.2).
    summary = contract.normalize_summary(str(tmp_path), run_id="run2", target_faces=8000)
    assert summary["metrics"]["target_faces"] == 8000
    assert "actual_faces" in summary["metrics"]
    assert any("generation_report.json" in w for w in summary["warnings"])
    assert any("lowpoly.blend" in w for w in summary["warnings"])


def test_collect_artifacts_partial(tmp_path):
    run_dir = str(tmp_path)
    _write(run_dir, "generation_report.json", {"band": "retry"})
    _write(run_dir, "validation_report.json", {"status": "retry"})
    artifacts, warnings = contract.collect_artifacts(run_dir)
    assert "generation_report" in artifacts
    assert "validation_report" in artifacts
    # shape_report + lowpoly.blend are required but absent.
    assert any("shape_report.json" in w for w in warnings)
    assert any("lowpoly.blend" in w for w in warnings)


def test_summary_is_json_serializable(tmp_path):
    summary = contract.normalize_summary(str(tmp_path), run_id="run3", target_faces=1000)
    json.loads(json.dumps(summary))
