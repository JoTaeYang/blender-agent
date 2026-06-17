"""Pure-Python tests for the app<->worker JSON contract (plan §5, Session A).

These run without Blender; the contract module never imports ``bpy``.
"""

import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_contract():
    path = os.path.join(_ROOT, "worker", "app_job_contract.py")
    spec = importlib.util.spec_from_file_location("app_job_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contract = _load_contract()


def test_worker_scripts_do_not_import_bpy_at_module_scope():
    # The contract helper must be importable outside Blender.
    assert contract.SCHEMA_VERSION == 1
    assert set(contract.COMMANDS) == {
        "inspect_model", "generate_lowpoly", "approve_lowpoly"}


@pytest.mark.parametrize(
    "faces,expected",
    [
        (0, contract.ROLE_UNKNOWN),
        (1200, contract.ROLE_LOWPOLY),
        (contract.LOWPOLY_FACE_THRESHOLD, contract.ROLE_LOWPOLY),
        (contract.LOWPOLY_FACE_THRESHOLD + 1, contract.ROLE_UNKNOWN),
        (contract.HIGHPOLY_FACE_THRESHOLD - 1, contract.ROLE_UNKNOWN),
        (contract.HIGHPOLY_FACE_THRESHOLD, contract.ROLE_HIGHPOLY),
        (500_000, contract.ROLE_HIGHPOLY),
    ],
)
def test_role_hint(faces, expected):
    assert contract.role_hint(faces) == expected


def test_recommended_next_step():
    assert contract.recommended_next_step([]) == contract.NEXT_INSPECT_MANUALLY
    assert contract.recommended_next_step(
        [{"mesh_role_hint": "highpoly"}, {"mesh_role_hint": "lowpoly"}]
    ) == contract.NEXT_GENERATE
    assert contract.recommended_next_step(
        [{"mesh_role_hint": "lowpoly"}, {"mesh_role_hint": "lowpoly"}]
    ) == contract.NEXT_APPROVE_EXISTING
    assert contract.recommended_next_step(
        [{"mesh_role_hint": "unknown"}]
    ) == contract.NEXT_INSPECT_MANUALLY


def test_status_lifecycle():
    st = contract.new_status(run_id="run_x", input={"target_faces": 12000})
    assert st["status"] == contract.STATUS_QUEUED
    assert st["finished_at"] is None
    assert st["run_id"] == "run_x"
    contract.finalize_status(st, status=contract.STATUS_ACCEPTED, artifacts={"a": "b"})
    assert st["status"] == contract.STATUS_ACCEPTED
    assert st["finished_at"] is not None
    assert st["artifacts"] == {"a": "b"}


def test_error_envelope_is_json_serializable():
    import json

    env = contract.error_envelope("inspect_model", "boom", code="import_failed", path="/x")
    assert env["status"] == contract.STATUS_FAILED
    assert env["error"]["code"] == "import_failed"
    assert env["path"] == "/x"
    json.loads(json.dumps(env))  # round-trips


def test_supported_formats():
    for ext in (".fbx", ".obj", ".glb", ".gltf"):
        assert ext in contract.SUPPORTED_IMPORT_EXTS
