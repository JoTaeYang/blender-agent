"""Tests for the MVP 3 artifact registry + handoff contract (plan §7, §9, Session D).

Pure-Python: the required six preview artifacts (plan §7) become entries when
present and warnings when missing; the summary always names itself; and the
project-relative handoff paths are pinned (plan §9).
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

_REQUIRED_PREVIEW_KEYS = (
    "baseline_uv_layout", "baseline_checker_front", "baseline_checker_side",
    "selected_uv_layout", "selected_checker_front", "selected_checker_side",
)


def _touch(d, *names):
    for name in names:
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write("x")


def test_required_six_previews_are_registered(tmp_path):
    d = str(tmp_path)
    _touch(d, *contract.REQUIRED_PREVIEWS)
    artifacts, warnings = contract.collect_generate_artifacts(d)
    for key in _REQUIRED_PREVIEW_KEYS:
        assert key in artifacts, key
    # no required preview missing -> no preview warnings
    assert not [w for w in warnings if "missing artifact" in w]


def test_summary_always_self_referenced(tmp_path):
    artifacts, _ = contract.collect_generate_artifacts(str(tmp_path))
    assert artifacts["summary"] == contract.SUMMARY_FILE


def test_missing_required_preview_becomes_warning(tmp_path):
    d = str(tmp_path)
    _touch(d, "baseline_uv_layout.png", "selected_uv_layout.png")
    artifacts, warnings = contract.collect_generate_artifacts(d)
    assert "baseline_uv_layout" in artifacts
    # the four missing required checker previews each warn
    missing = [w for w in warnings if "missing artifact" in w]
    assert len(missing) == 4


def test_optional_reports_and_blend_registered_when_present(tmp_path):
    d = str(tmp_path)
    _touch(d, contract.P5_GATE_FILE, contract.SEAM_REPORT_FILE,
           contract.CANDIDATE_SUMMARY_FILE, contract.SELECTED_BLEND_FILE)
    artifacts, _ = contract.collect_generate_artifacts(d)
    assert artifacts["p5_gate"] == contract.P5_GATE_FILE
    assert artifacts["seam_report"] == contract.SEAM_REPORT_FILE
    assert artifacts["candidate_summary"] == contract.CANDIDATE_SUMMARY_FILE
    assert artifacts["selected_blend"] == contract.SELECTED_BLEND_FILE


def test_handoff_paths_are_project_relative(tmp_path):
    # The selected UV ships to work/uv (plan §2, §9). Pinned so MVP 4/5 can find it.
    assert contract.SELECTED_UV_BLEND_REL == os.path.join("work", "uv", "selected_uv.blend")
    assert contract.SELECTED_UV_SUMMARY_REL == os.path.join("work", "uv", "selected_uv_summary.json")


def test_empty_run_dir_warns_for_all_required_previews(tmp_path):
    artifacts, warnings = contract.collect_generate_artifacts(str(tmp_path))
    # only the self-referenced summary; all six previews warn.
    assert list(artifacts.keys()) == ["summary"]
    assert len([w for w in warnings if "missing artifact" in w]) == 6
