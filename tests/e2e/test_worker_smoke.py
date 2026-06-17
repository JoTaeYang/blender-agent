"""Blender-gated e2e smoke for the app workers (plan Session E).

Policy: if no Blender executable is found (env ``BLENDER`` or a common install
path), every test here is **skipped** so ``pytest`` stays green in CI / dev
machines without Blender. When Blender is present:

- ``test_inspect_smoke`` imports ``sample/SM_Test_Pottery_a_02.fbx`` and asserts a
  contract-shaped inspect result.
- ``test_generate_smoke`` runs a low-poly generation (gated behind
  ``UV_E2E_GENERATE`` since it is heavier) and asserts ``status.json`` /
  ``summary.json`` with target + actual face counts.

Results are read from the JSON artifacts, never from stdout (plan §3).
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


def _run_blender(script: str, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    cmd = [_BLENDER, "--background", "--python", os.path.join(_ROOT, "worker", script), "--", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@requires_blender
@requires_sample
def test_inspect_smoke(tmp_path):
    out = str(tmp_path / "inspect.json")
    proc = _run_blender("inspect_model.py", ["--path", _SAMPLE, "--out", out], timeout=300)
    assert os.path.exists(out), f"no inspect output\nstdout={proc.stdout}\nstderr={proc.stderr}"
    result = json.loads(open(out, encoding="utf-8").read())
    assert result["status"] == "accepted", result
    assert result["schema_version"] == 1
    assert result["objects"], "expected at least one mesh object"
    obj = result["objects"][0]
    for key in ("name", "vertices", "edges", "faces", "uv_layers", "mesh_role_hint"):
        assert key in obj, f"missing {key} in {obj}"
    assert obj["faces"] > 0


@requires_blender
@requires_sample
@pytest.mark.skipif(
    os.environ.get("UV_E2E_GENERATE") != "1",
    reason="set UV_E2E_GENERATE=1 to run the heavier generation smoke",
)
def test_generate_smoke(tmp_path):
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir, exist_ok=True)
    job = {
        "command": "generate_lowpoly",
        "run_id": "run_smoke",
        "source_model": _SAMPLE,
        "object_name": None,  # let the worker pick the first mesh
        "target_faces": 8000,
        "options": {"mode": "decimation_optimize", "render_preview": True},
        "out_dir": run_dir,
    }
    job_path = str(tmp_path / "job.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)

    proc = _run_blender("run_app_retopo_job.py", ["--job", job_path], timeout=900)

    status_path = os.path.join(run_dir, "status.json")
    assert os.path.exists(status_path), f"no status.json\nstdout={proc.stdout}\nstderr={proc.stderr}"
    status = json.loads(open(status_path, encoding="utf-8").read())
    assert status["status"] in ("accepted", "failed"), status

    summary_path = os.path.join(run_dir, "summary.json")
    assert os.path.exists(summary_path), "summary.json must exist for any finished run"
    summary = json.loads(open(summary_path, encoding="utf-8").read())
    assert summary["metrics"]["target_faces"] == 8000
    # actual face count must be reported on an accepted run
    if status["status"] == "accepted":
        assert summary["metrics"]["actual_faces"] is not None
        assert os.path.exists(os.path.join(run_dir, "lowpoly.blend"))
