"""Blender-gated e2e smoke for the MVP 2 seam-editor worker (plan §11 Session G).

Policy (same as the MVP 1 smoke): if no Blender executable is found, every test
here is **skipped** so ``pytest`` stays green without Blender. When Blender is
present:

- ``test_export_edge_geometry_smoke`` exports the sample pottery's edge geometry
  and asserts ``edge_geometry.json`` edge ids are dense and match the exported
  mesh signature (the renderer's only selectable-id source, plan §5).
- ``test_extract_uv_boundary_smoke`` converts ``UVChannel_1``'s island boundaries
  into a seam spec and asserts the spec loads through
  ``artist_uv_agent.user_seams.UserSeamSpec`` (the MVP 3 input, plan §6.4, §16).
- ``test_no_uv_boundary_smoke`` runs boundary extraction on a generated UV-less
  cube and asserts the ``status: no_uv`` outcome (plan §6.4, §13).

Results are read from the JSON artifacts, never from stdout (plan §13). The no-UV
fixture is generated on the fly because ``*.obj`` is gitignored (plan §15).
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


def _run_worker(job: dict, tmp_path, timeout: int = 600) -> subprocess.CompletedProcess:
    job_path = str(tmp_path / "job.json")
    with open(job_path, "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    cmd = [_BLENDER, "--background", "--python",
           os.path.join(_ROOT, "worker", "seam_editor_worker.py"), "--", "--job", job_path]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _write_no_uv_obj(path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "o cube_no_uv\n"
            "v -1 -1 -1\nv -1 -1 1\nv -1 1 -1\nv -1 1 1\n"
            "v 1 -1 -1\nv 1 -1 1\nv 1 1 -1\nv 1 1 1\n"
            "f 1 2 4 3\nf 5 7 8 6\nf 1 5 6 2\nf 3 4 8 7\nf 1 3 7 5\nf 2 6 8 4\n"
        )


@requires_blender
@requires_sample
def test_export_edge_geometry_smoke(tmp_path):
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir, exist_ok=True)
    job = {
        "command": "export_edge_geometry",
        "project_id": "p_e2e",
        "run_id": "seam_export_e2e",
        "model": _SAMPLE,
        "model_rel": "sample/SM_Test_Pottery_a_02.fbx",
        "object_name": "SM_Test_Pottery_a_02",
        "out_dir": run_dir,
    }
    proc = _run_worker(job, tmp_path)

    status_path = os.path.join(run_dir, "status.json")
    assert os.path.exists(status_path), f"no status.json\nstdout={proc.stdout}\nstderr={proc.stderr}"
    status = json.loads(open(status_path, encoding="utf-8").read())
    assert status["status"] == "accepted", status

    geo = json.loads(open(os.path.join(run_dir, "edge_geometry.json"), encoding="utf-8").read())
    result = json.loads(open(os.path.join(run_dir, "export_result.json"), encoding="utf-8").read())

    # Edge ids are dense 0..N-1 and the count matches the mesh signature (plan §5).
    n = len(geo["edges"])
    assert n > 0
    assert [e["id"] for e in geo["edges"]] == list(range(n))
    assert result["mesh_signature"]["edges"] == n
    assert result["mesh_signature"]["vertices"] == len(geo["vertices"])
    assert result["mesh_signature"]["faces"] == len(geo["faces"])
    # Each edge carries the contract fields (plan §5.1).
    e0 = geo["edges"][0]
    for key in ("id", "vertex_ids", "face_ids", "is_boundary", "is_seam", "dihedral_angle"):
        assert key in e0, key


@requires_blender
@requires_sample
def test_extract_uv_boundary_smoke(tmp_path):
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir, exist_ok=True)
    out_spec = str(tmp_path / "reference_boundary_seam_spec.json")
    job = {
        "command": "extract_uv_boundary_as_seams",
        "project_id": "p_e2e",
        "run_id": "seam_boundary_e2e",
        "model": _SAMPLE,
        "model_rel": "sample/SM_Test_Pottery_a_02.fbx",
        "object_name": "SM_Test_Pottery_a_02",
        "uv_layer": "UVChannel_1",
        "out_dir": run_dir,
        "out_path": out_spec,
        "out_path_rel": "work/seams/reference_boundary_seam_spec.json",
    }
    proc = _run_worker(job, tmp_path)

    status_path = os.path.join(run_dir, "status.json")
    assert os.path.exists(status_path), f"no status.json\nstdout={proc.stdout}\nstderr={proc.stderr}"
    status = json.loads(open(status_path, encoding="utf-8").read())
    assert status["status"] == "accepted", status

    report = json.loads(open(os.path.join(run_dir, "boundary_extract_report.json"), encoding="utf-8").read())
    assert report["status"] == "accepted"
    assert report["uv_layer"] == "UVChannel_1"
    assert report["user_seam_count"] >= 1, "pottery UV has island boundaries"
    assert report["report"]["uv_layer_missing"] is False

    # The written spec loads through the MVP 3 schema (plan §16 done criterion).
    assert os.path.exists(out_spec)
    spec = UserSeamSpec.from_dict(json.loads(open(out_spec, encoding="utf-8").read()))
    assert spec.object == "SM_Test_Pottery_a_02"
    assert spec.mode == "user_seams"
    assert len(spec.user_seam_edges) == report["user_seam_count"]
    # MVP 2 never auto-adds protected edges (plan §1, §13).
    assert spec.user_protected_edges == set()


@requires_blender
def test_no_uv_boundary_smoke(tmp_path):
    model = str(tmp_path / "cube_no_uv.obj")
    _write_no_uv_obj(model)
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir, exist_ok=True)
    job = {
        "command": "extract_uv_boundary_as_seams",
        "project_id": "p_e2e",
        "run_id": "seam_nouv_e2e",
        "model": model,
        "model_rel": "cube_no_uv.obj",
        "object_name": "cube_no_uv",
        "uv_layer": "UVChannel_1",
        "out_dir": run_dir,
        "out_path": str(tmp_path / "spec.json"),
    }
    proc = _run_worker(job, tmp_path)

    status_path = os.path.join(run_dir, "status.json")
    assert os.path.exists(status_path), f"no status.json\nstdout={proc.stdout}\nstderr={proc.stderr}"
    status = json.loads(open(status_path, encoding="utf-8").read())
    assert status["status"] == "no_uv", status

    report = json.loads(open(os.path.join(run_dir, "boundary_extract_report.json"), encoding="utf-8").read())
    assert report["status"] == "no_uv"
    assert report["path"] is None
    # No boundary spec is written for a UV-less model.
    assert not os.path.exists(str(tmp_path / "spec.json"))
