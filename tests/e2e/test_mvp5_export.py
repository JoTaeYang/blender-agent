"""Blender-gated e2e smoke for the MVP 5 export worker (plan §12 Session G).

Policy (same as the MVP 1/2/3 smokes): if no Blender executable is found, every
test here is **skipped** so ``pytest`` stays green without Blender. When Blender
is present:

- ``test_export_smoke`` exports the sample pottery (standing in for an MVP 3
  ``selected_uv.blend``) to FBX + OBJ + GLB, then asserts the MVP 5 acceptance:
  ``status == accepted``, an ``export_manifest.json`` linking the source UV run +
  metrics + files, each exported file present on disk, and a
  ``validation_report.json`` whose every format re-opened WITH a UV layer (plan
  §5, §6, §7).
- ``test_export_missing_model_smoke`` points at a non-existent selected UV model
  and asserts the hard failure (``failed`` / ``missing_selected_uv_model``) with
  NO manifest written — an all-fail export never ships a manifest (plan §5, §14).
- ``test_readiness_smoke`` runs ``check_export_readiness`` against an accepted
  summary (ready) and a missing model (``needs_input`` /
  ``missing_selected_uv_model``) (plan §4).

History + rollback are pure-Node (``project-service.ts``) and are covered by
``app/test/integration.test.ts``; this file is the Blender-backed export half.
Results are read from the JSON artifacts, never from stdout (plan §5).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SAMPLE = os.path.join(_ROOT, "sample", "SM_Test_Pottery_a_02.fbx")
_OBJECT = "SM_Test_Pottery_a_02"
_UV_LAYER = "UVChannel_1"

_COMMON_BLENDER = [
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "/usr/bin/blender",
    "/usr/local/bin/blender",
]


def _find_blender() -> str | None:
    env = os.environ.get("BLENDER")
    if env and os.path.exists(env):
        return env
    found = shutil.which("blender")
    if found:
        return found
    for p in _COMMON_BLENDER:
        if os.path.exists(p):
            return p
    return None


_BLENDER = _find_blender()
requires_blender = pytest.mark.skipif(_BLENDER is None, reason="Blender not installed")
requires_sample = pytest.mark.skipif(
    not os.path.exists(_SAMPLE), reason="sample pottery FBX not present"
)


def _run_worker(job: dict, tmp_path, timeout: int = 900) -> subprocess.CompletedProcess:
    job_path = str(tmp_path / "export_job.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    cmd = [_BLENDER, "--background", "--python",
           os.path.join(_ROOT, "worker", "export_production_asset.py"), "--", "--job", job_path]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _export_job(model: str, out_dir: str, formats: list[str], **extra) -> dict:
    job = {
        "command": "export_production_asset",
        "project_id": "e2e",
        "export_id": "export_e2e",
        "selected_uv_model": model,
        "selected_uv_model_rel": os.path.join("work", "uv", "selected_uv.blend"),
        "selected_uv_summary_rel": os.path.join("work", "uv", "selected_uv_summary.json"),
        "object_name": _OBJECT,
        "formats": formats,
        "out_dir": out_dir,
        "out_dir_rel": os.path.join("exports", "export_e2e"),
        "uv_generate_run_id": "uv_run_e2e",
        "seam_spec_rel": os.path.join("work", "seams", "user_seam_spec.json"),
        "candidate_summary_rel": os.path.join("runs", "uv_run_e2e", "candidate_summary.json"),
        # keep the smoke fast: small previews
        "options": {"selected_uv_layer": _UV_LAYER, "render_size_px": 320, "texture_size_px": 384},
        "out": os.path.join(out_dir, "export_result.json"),
    }
    job.update(extra)
    return job


def _accepted_summary(path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "schema_version": 1, "status": "accepted",
            "selected_candidate_id": "slim_concave_m002",
            "selected_uv_model": "work/uv/selected_uv.blend",
            "metrics": {"stretch_score": 0.0686, "worst_island_distortion": 0.203,
                        "raster_overlap_ratio": 0.0, "texel_density_variance": 0.000002,
                        "packing_efficiency": 0.5912, "uv_bounds_ok": True},
            "seam_integrity": {"valid": True, "user_seam_count": 724,
                               "final_seam_count": 724, "auto_added_seams": 0},
        }, fh)


@requires_blender
@requires_sample
def test_export_smoke(tmp_path):
    out_dir = str(tmp_path / "exports" / "export_e2e")
    os.makedirs(out_dir, exist_ok=True)
    summary_path = str(tmp_path / "selected_uv_summary.json")
    _accepted_summary(summary_path)
    job = _export_job(_SAMPLE, out_dir, ["fbx", "obj", "glb"], selected_uv_summary=summary_path)
    proc = _run_worker(job, tmp_path)
    assert proc.returncode == 0, proc.stderr[-3000:]

    status = _read(os.path.join(out_dir, "status.json"))
    assert status["status"] == "accepted", status

    # 1. manifest links the source UV run + metrics + files (plan §6).
    manifest = _read(os.path.join(out_dir, "export_manifest.json"))
    assert manifest["status"] == "accepted"
    assert set(manifest["formats"]) == {"fbx", "obj", "glb"}
    assert manifest["source"]["uv_generate_run_id"] == "uv_run_e2e"
    assert manifest["source"]["ai_review_skipped"] is True
    assert "packing_efficiency" in manifest["metrics"]
    for fmt in ("fbx", "obj", "glb"):
        assert fmt in manifest["files"], fmt
        assert os.path.exists(os.path.join(out_dir, manifest["files"][fmt])), fmt

    # 2. validation re-opened every format WITH a UV layer (plan §7).
    validation = _read(os.path.join(out_dir, "validation_report.json"))
    assert validation["status"] == "accepted"
    for fmt in ("fbx", "obj", "glb"):
        fv = validation["formats"][fmt]
        assert fv["reopen_ok"] is True, fmt
        assert fv["has_uv"] is True, fmt
        assert fv["faces"] > 0 and fv["vertices"] > 0

    # 3. best-effort previews of the exported result (plan §7 step 7).
    for png in ("uv_layout.png", "checker_front.png", "checker_side.png"):
        assert os.path.exists(os.path.join(out_dir, png)), png

    # 4. structured result carries the project-relative export paths (plan §5.1).
    result = _read(os.path.join(out_dir, "export_result.json"))
    assert result["status"] == "accepted"
    assert set(result["exports"].keys()) == {"fbx", "obj", "glb"}


@requires_blender
def test_export_missing_model_smoke(tmp_path):
    out_dir = str(tmp_path / "exports" / "export_missing")
    os.makedirs(out_dir, exist_ok=True)
    job = _export_job(str(tmp_path / "does_not_exist.blend"), out_dir, ["obj"],
                      options={"render_previews": False})
    proc = _run_worker(job, tmp_path)
    assert proc.returncode in (0, 2), proc.stderr[-2000:]
    status = _read(os.path.join(out_dir, "status.json"))
    assert status["status"] == "failed"
    assert status["error"]["code"] == "missing_selected_uv_model"
    # Nothing shipped: no manifest written for an all-fail export (plan §5, §14).
    assert not os.path.exists(os.path.join(out_dir, "export_manifest.json"))


@requires_blender
@requires_sample
def test_readiness_smoke(tmp_path):
    out_dir = str(tmp_path / "readiness")
    os.makedirs(out_dir, exist_ok=True)
    summary_path = str(tmp_path / "selected_uv_summary.json")
    _accepted_summary(summary_path)

    # Accepted selected UV (model re-opens, summary accepted) -> ready (plan §4).
    job = {
        "command": "check_export_readiness", "project_id": "e2e", "export_id": "rdy",
        "selected_uv_model": _SAMPLE,
        "selected_uv_model_rel": os.path.join("work", "uv", "selected_uv.blend"),
        "selected_uv_summary": summary_path, "uv_generate_run_id": "uv_run_e2e",
        "out_dir": out_dir, "out": os.path.join(out_dir, "readiness.json"),
    }
    proc = _run_worker(job, tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    ready = _read(os.path.join(out_dir, "readiness.json"))
    assert ready["status"] == "accepted"
    assert ready["ready"] is True
    assert ready["checks"]["ai_review_skipped"] is True
    assert any("AI Review" in w for w in ready["warnings"])

    # Missing selected UV -> needs_input (plan §4.2).
    out_dir2 = str(tmp_path / "readiness_missing")
    os.makedirs(out_dir2, exist_ok=True)
    job2 = {
        "command": "check_export_readiness", "project_id": "e2e", "export_id": "rdy2",
        "selected_uv_model": str(tmp_path / "nope.blend"),
        "out_dir": out_dir2, "out": os.path.join(out_dir2, "readiness.json"),
    }
    proc2 = _run_worker(job2, tmp_path)
    assert proc2.returncode == 0, proc2.stderr[-2000:]
    ready2 = _read(os.path.join(out_dir2, "readiness.json"))
    assert ready2["status"] == "needs_input"
    assert ready2["ready"] is False
    assert any(i["code"] == "missing_selected_uv_model" for i in ready2["blocking_issues"])
