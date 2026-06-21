"""Blender-gated e2e smoke for the MVP 1 UV review worker (plan §12, Session F).

Policy: if no Blender executable is found (env ``BLENDER`` or a common install
path), every test here is **skipped** so ``pytest`` stays green without Blender
(plan §12 acceptance). When Blender is present:

- ``test_inspect_uv_layers_smoke`` imports the sample pottery FBX and asserts a
  contract-shaped ``inspect_uv_layers`` result with ``UVChannel_1``.
- ``test_review_existing_uv_smoke`` reviews ``UVChannel_1`` and asserts the
  ``status.json`` / ``uv_review_summary.json`` contract + the required image
  artifacts (``uv_layout.png`` / ``checker_front.png`` / ``checker_side.png``).
- ``test_no_uv_review_smoke`` reviews a generated UV-less OBJ and asserts the
  ``no_uv`` outcome with no image artifacts.

Results are read from the JSON artifacts, never from stdout (plan §4). The no-UV
fixture is generated on the fly because ``*.obj`` is gitignored (plan §14).
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

# Required metric keys from the contract (plan §6).
_REQUIRED_METRICS = (
    "stretch_score", "worst_island_distortion", "overlap_ratio",
    "raster_overlap_ratio", "self_overlap_ratio", "cross_overlap_ratio",
    "texel_density_variance", "packing_efficiency",
)


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


def _run_worker(job: dict, tmp_path, timeout: int = 600) -> subprocess.CompletedProcess:
    job_path = str(tmp_path / "job.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    cmd = [_BLENDER, "--background", "--python",
           os.path.join(_ROOT, "worker", "review_existing_uv.py"), "--", "--job", job_path]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _write_no_uv_obj(path: str) -> None:
    """A genuine UV-less cube (v/f only, no ``vt``)."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "o cube_no_uv\n"
            "v -1 -1 -1\nv -1 -1 1\nv -1 1 -1\nv -1 1 1\n"
            "v 1 -1 -1\nv 1 -1 1\nv 1 1 -1\nv 1 1 1\n"
            "f 1 2 4 3\nf 5 7 8 6\nf 1 5 6 2\nf 3 4 8 7\nf 1 3 7 5\nf 2 6 8 4\n"
        )


@requires_blender
@requires_sample
def test_inspect_uv_layers_smoke(tmp_path):
    out = str(tmp_path / "inspect.json")
    job = {
        "command": "inspect_uv_layers",
        "project_id": "p_e2e",
        "model": _SAMPLE,
        "model_rel": "sample/SM_Test_Pottery_a_02.fbx",
        "out": out,
    }
    proc = _run_worker(job, tmp_path)
    assert os.path.exists(out), f"no inspect output\nstdout={proc.stdout}\nstderr={proc.stderr}"
    result = json.loads(open(out, encoding="utf-8").read())
    assert result["status"] == "accepted", result
    assert result["schema_version"] == 1
    assert result["recommended_next_step"] == "review_existing_uv"
    obj = result["objects"][0]
    assert obj["has_uv"] is True
    assert obj["active_uv_layer"] == "UVChannel_1"
    assert any(lyr["name"] == "UVChannel_1" for lyr in obj["uv_layers"])


@requires_blender
@requires_sample
def test_review_existing_uv_smoke(tmp_path):
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir, exist_ok=True)
    job = {
        "command": "review_existing_uv",
        "project_id": "p_e2e",
        "run_id": "review_e2e",
        "model": _SAMPLE,
        "model_rel": "sample/SM_Test_Pottery_a_02.fbx",
        "object_name": "SM_Test_Pottery_a_02",
        "uv_layer": "UVChannel_1",
        "options": {"render_size_px": 400, "raster_overlap_resolution": 512},
        "out_dir": run_dir,
    }
    proc = _run_worker(job, tmp_path)

    status_path = os.path.join(run_dir, "status.json")
    assert os.path.exists(status_path), f"no status.json\nstdout={proc.stdout}\nstderr={proc.stderr}"
    status = json.loads(open(status_path, encoding="utf-8").read())
    assert status["status"] == "accepted", status

    summary = json.loads(open(os.path.join(run_dir, "uv_review_summary.json"), encoding="utf-8").read())
    assert summary["status"] == "accepted"
    assert summary["uv_layer"] == "UVChannel_1"
    assert summary["mesh"]["loops"] > 0
    for key in _REQUIRED_METRICS:
        assert key in summary["metrics"], f"missing metric {key}"
    assert summary["uv"]["island_count"] >= 1
    assert summary["review_status"] in (
        "clean", "has_overlap", "high_stretch", "density_variance", "out_of_bounds")

    # Required image artifacts exist and are non-empty (plan §7, §12).
    for name in ("uv_layout.png", "checker_front.png", "checker_side.png"):
        p = os.path.join(run_dir, name)
        assert os.path.exists(p) and os.path.getsize(p) > 0, f"missing/empty {name}"
    # Artifact paths recorded in the summary are run-relative (plan §4, §9).
    assert summary["artifacts"]["uv_layout"] == "uv_layout.png"
    assert summary["artifacts"]["checker_front"] == "checker_front.png"


@requires_blender
def test_no_uv_review_smoke(tmp_path):
    model = str(tmp_path / "cube_no_uv.obj")
    _write_no_uv_obj(model)
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir, exist_ok=True)
    job = {
        "command": "review_existing_uv",
        "project_id": "p_e2e",
        "run_id": "review_nouv_e2e",
        "model": model,
        "model_rel": "cube_no_uv.obj",
        "object_name": "cube_no_uv",
        "out_dir": run_dir,
    }
    proc = _run_worker(job, tmp_path)

    status_path = os.path.join(run_dir, "status.json")
    assert os.path.exists(status_path), f"no status.json\nstdout={proc.stdout}\nstderr={proc.stderr}"
    status = json.loads(open(status_path, encoding="utf-8").read())
    assert status["status"] == "no_uv", status

    summary = json.loads(open(os.path.join(run_dir, "uv_review_summary.json"), encoding="utf-8").read())
    assert summary["status"] == "no_uv"
    assert summary["metrics"] is None
    assert summary["uv_layer"] is None
    # No image artifacts are produced for a no-UV model.
    assert not os.path.exists(os.path.join(run_dir, "uv_layout.png"))
    assert not os.path.exists(os.path.join(run_dir, "checker_front.png"))
