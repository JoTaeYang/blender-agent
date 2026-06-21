"""Blender-gated e2e smoke for the MVP 3 generate worker (plan §11 Session G).

Policy (same as the MVP 1/2 smokes): if no Blender executable is found, every
test here is **skipped** so ``pytest`` stays green without Blender. When Blender
is present:

- ``test_missing_seam_spec_smoke`` runs generate with a non-existent spec path AND
  no UV layer and asserts the structured ``needs_input`` / ``missing_seam_source``
  outcome (UV-boundary-fallback revision plan §1 case 3, §4.2).
- ``test_uv_boundary_fallback_generate_smoke`` runs generate with NO spec but the
  sample pottery's ``UVChannel_1`` UV layer and asserts the derived path: a
  ``derived_from_uv_boundary.json`` is written, ``seam_source.type ==
  uv_boundary_derived``, ``auto_added_seams == 0``, and a selected UV is produced
  (revision plan §1 case 2, §6.2).
- ``test_invalid_seam_spec_smoke`` runs generate with an out-of-range edge id and
  asserts ``failed`` / ``invalid_seam_spec`` and that NO selected UV shipped to
  ``work/uv/`` (plan §6 "invalid edge ids가 있으면 selected output을 만들지 않는다").
- ``test_reference_boundary_generate_smoke`` extracts the sample pottery's
  ``UVChannel_1`` island boundaries into a user seam spec (the MVP 2 reference
  path), runs strict user/reference generate + optimize, and asserts the MVP 3
  acceptance: ``auto_added_seams == 0``, ``final_seam_count == user_seam_count``,
  a candidate summary, the six before/after previews, and a re-openable selected
  ``.blend`` (plan §6, §7, §16).

Results are read from the JSON artifacts, never from stdout (plan §4.1).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from artist_uv_agent.user_seams import UserSeamSpec

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


def _run_worker(worker: str, job: dict, tmp_path, timeout: int = 1800) -> subprocess.CompletedProcess:
    job_path = str(tmp_path / f"{worker}_job.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    cmd = [_BLENDER, "--background", "--python",
           os.path.join(_ROOT, "worker", worker), "--", "--job", job_path]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _status(out_dir: str) -> dict:
    with open(os.path.join(out_dir, "status.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _summary(out_dir: str) -> dict:
    with open(os.path.join(out_dir, "uv_generate_summary.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _generate_job(model: str, seam_spec: str, out_dir: str, project: str, **extra) -> dict:
    job = {
        "command": "generate_uv_from_seams",
        "project_id": "e2e",
        "run_id": "uv_run_e2e",
        "model": model,
        "model_rel": os.path.basename(model),
        "object_name": _OBJECT,
        "seam_spec": seam_spec,
        "seam_spec_rel": "user_seam_spec.json",
        "out_dir": out_dir,
        "selected_blend_out": os.path.join(project, "work", "uv", "selected_uv.blend"),
        "selected_blend_out_rel": os.path.join("work", "uv", "selected_uv.blend"),
        "selected_summary_out": os.path.join(project, "work", "uv", "selected_uv_summary.json"),
        # keep the smoke fast: a small candidate sweep + small renders
        "options": {"layout_opt_max_candidates": 6, "render_size_px": 400, "texture_size_px": 512},
    }
    job.update(extra)
    return job


@requires_blender
@requires_sample
def test_missing_seam_spec_smoke(tmp_path):
    # No spec file AND no uv_layer -> needs_input (revision plan §1 case 3, §4.2).
    out_dir = str(tmp_path / "run")
    os.makedirs(out_dir, exist_ok=True)
    job = _generate_job(_SAMPLE, str(tmp_path / "does_not_exist.json"), out_dir, str(tmp_path))
    proc = _run_worker("generate_uv_from_seams.py", job, tmp_path)
    assert proc.returncode in (0, 2), proc.stderr[-2000:]
    status = _status(out_dir)
    assert status["status"] == "needs_input"
    assert status["error"]["code"] == "missing_seam_source"
    # No selected UV shipped (plan §6).
    assert not os.path.exists(os.path.join(str(tmp_path), "work", "uv", "selected_uv.blend"))


@requires_blender
@requires_sample
def test_uv_boundary_fallback_generate_smoke(tmp_path):
    # No spec, but the sample carries UVChannel_1 -> derive a seam spec from its UV
    # island boundary and run strict generate + optimize (revision plan §1 case 2).
    project = str(tmp_path)
    out_dir = str(tmp_path / "run")
    os.makedirs(out_dir, exist_ok=True)
    derived_out = os.path.join(project, "work", "seams", "derived_from_uv_boundary.json")
    job = _generate_job(
        _SAMPLE, None, out_dir, project,
        uv_layer=_UV_LAYER, seam_source_policy="prefer_spec_then_uv_boundary",
        derived_seam_spec_out=derived_out,
        derived_seam_spec_out_rel=os.path.join("work", "seams", "derived_from_uv_boundary.json"))
    proc = _run_worker("generate_uv_from_seams.py", job, tmp_path)
    assert proc.returncode == 0, proc.stderr[-3000:]

    status = _status(out_dir)
    assert status["status"] in ("accepted", "needs_user_review"), status
    summary = _summary(out_dir)

    # Derived seam source recorded (revision plan §6.2).
    assert os.path.exists(derived_out), "derived_from_uv_boundary.json was written"
    assert os.path.exists(os.path.join(out_dir, "derived_from_uv_boundary.json"))
    src = summary["seam_source"]
    assert src["type"] == "uv_boundary_derived"
    assert src["derived"] is True and src["user_confirmed"] is False
    assert src["uv_layer"] == _UV_LAYER

    # Seam integrity holds on the derived path (revision plan §7).
    si = summary["seam_integrity"]
    assert si["auto_added_seams"] == 0
    assert si["final_seam_count"] == si["user_seam_count"] > 0

    # The selected UV output is still generated (revision plan §6.2).
    cand = json.load(open(os.path.join(out_dir, "candidate_summary.json"), encoding="utf-8"))
    assert cand["candidates"], "a candidate sweep was recorded"
    for name in ("baseline_uv_layout.png", "selected_uv_layout.png"):
        assert os.path.exists(os.path.join(out_dir, name)), name
    if status["status"] == "accepted":
        assert os.path.exists(os.path.join(project, "work", "uv", "selected_uv.blend"))


@requires_blender
@requires_sample
def test_invalid_seam_spec_smoke(tmp_path):
    project = str(tmp_path)
    out_dir = str(tmp_path / "run")
    os.makedirs(out_dir, exist_ok=True)
    spec_path = str(tmp_path / "invalid_spec.json")
    # object empty -> no mismatch; one absurd edge id -> invalid_seam_spec.
    with open(spec_path, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "object": "", "mode": "user_seams",
                   "user_seam_edges": [999999999], "user_protected_edges": [],
                   "chapters": []}, fh)
    job = _generate_job(_SAMPLE, spec_path, out_dir, project)
    proc = _run_worker("generate_uv_from_seams.py", job, tmp_path)
    assert proc.returncode in (0, 2), proc.stderr[-2000:]
    status = _status(out_dir)
    assert status["status"] == "failed"
    assert status["error"]["code"] == "invalid_seam_spec"
    assert status["error"]["details"]["invalid_edges"] == [999999999]
    # No selected UV shipped to work/uv (plan §6).
    assert not os.path.exists(os.path.join(project, "work", "uv", "selected_uv.blend"))


@requires_blender
@requires_sample
def test_reference_boundary_generate_smoke(tmp_path):
    project = str(tmp_path)
    # 1. Extract the pottery's UV island boundaries into a user seam spec (MVP 2).
    seam_out = str(tmp_path / "seam_run")
    os.makedirs(seam_out, exist_ok=True)
    spec_path = str(tmp_path / "reference_boundary_seam_spec.json")
    seam_job = {
        "command": "extract_uv_boundary_as_seams", "project_id": "e2e", "run_id": "seam_run_e2e",
        "model": _SAMPLE, "model_rel": os.path.basename(_SAMPLE), "object_name": _OBJECT,
        "uv_layer": _UV_LAYER, "out_dir": seam_out, "out_path": spec_path,
        "out_path_rel": "reference_boundary_seam_spec.json",
    }
    proc = _run_worker("seam_editor_worker.py", seam_job, tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert os.path.exists(spec_path), "boundary spec was written"
    spec = UserSeamSpec.from_dict(json.load(open(spec_path, encoding="utf-8")))
    user_seam_count = len(spec.user_seam_edges)
    assert user_seam_count > 0, "reference boundary produced seams"

    # 2. Strict user/reference generate + optimize over that fixed seam set.
    out_dir = str(tmp_path / "run")
    os.makedirs(out_dir, exist_ok=True)
    job = _generate_job(_SAMPLE, spec_path, out_dir, project)
    proc = _run_worker("generate_uv_from_seams.py", job, tmp_path)
    assert proc.returncode == 0, proc.stderr[-3000:]

    status = _status(out_dir)
    assert status["status"] in ("accepted", "needs_user_review"), status
    summary = _summary(out_dir)

    # 3. Seam integrity is the MVP 3 hard acceptance (plan §6).
    si = summary["seam_integrity"]
    assert si["auto_added_seams"] == 0, "strict mode added no seams"
    assert si["final_seam_count"] == si["user_seam_count"] == user_seam_count
    assert si["mandatory_rule_enabled"] is False
    assert si["mandatory_gate_enabled"] is False

    # 4. Candidate summary + the six before/after previews (plan §5, §7).
    cand = json.load(open(os.path.join(out_dir, "candidate_summary.json"), encoding="utf-8"))
    assert cand["candidates"], "a candidate sweep was recorded"
    assert cand["selected_candidate_id"] == summary["selected_candidate_id"]
    for name in ("baseline_uv_layout.png", "baseline_checker_front.png",
                 "baseline_checker_side.png", "selected_uv_layout.png",
                 "selected_checker_front.png", "selected_checker_side.png"):
        assert os.path.exists(os.path.join(out_dir, name)), name

    # 5. On an accepted run the selected UV ships to work/uv and re-opens with a UV
    #    layer (plan §6, §9); a needs_user_review run must NOT ship.
    selected_blend = os.path.join(project, "work", "uv", "selected_uv.blend")
    if status["status"] == "accepted":
        assert os.path.exists(selected_blend), "accepted run ships selected_uv.blend"
        assert os.path.exists(os.path.join(project, "work", "uv", "selected_uv_summary.json"))
        assert summary["selected_uv_model"] == os.path.join("work", "uv", "selected_uv.blend")
    else:
        assert not os.path.exists(selected_blend), "needs_user_review must not ship"
