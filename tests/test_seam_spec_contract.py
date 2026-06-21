"""Seam spec contract tests (Electron MVP 2 plan §4, §6.2, §6.3, Session C).

Pure-Python: loads ``worker/app_seam_spec_contract.py`` stand-alone (no Blender)
and exercises the spec normalization/validation rules, then asserts the normalized
output round-trips through ``artist_uv_agent.user_seams.UserSeamSpec`` so the saved
``user_seam_spec.json`` is exactly what the MVP 3 pipeline expects (plan §13, §16).
"""

import importlib.util
import json
import os

from artist_uv_agent.user_seams import UserSeamSpec

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load_contract():
    path = os.path.join(_ROOT, "worker", "app_seam_spec_contract.py")
    spec = importlib.util.spec_from_file_location("app_seam_spec_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


contract = _load_contract()


def test_make_seam_spec_round_trips_through_user_seam_spec():
    spec = contract.make_seam_spec(
        object_name="SM_Test_Pottery_a_02",
        user_seam_edges=[113, 16, 138], user_protected_edges=[21, 20],
        notes="Authored in Electron MVP2")
    # Canonical field set + sorted ids.
    assert spec["mode"] == "user_seams"
    assert spec["user_seam_edges"] == [16, 113, 138]
    assert spec["user_protected_edges"] == [20, 21]
    # Loads via the MVP 3 schema (plan §16 done criterion).
    loaded = UserSeamSpec.from_dict(json.loads(json.dumps(spec)))
    assert loaded.object == "SM_Test_Pottery_a_02"
    assert loaded.user_seam_edges == {16, 113, 138}
    assert loaded.user_protected_edges == {20, 21}


def test_validate_plan_example_6_3():
    """The exact §6.3 example: invalid 999999 + a seam/protected conflict on 16."""
    spec = {
        "version": 1, "object": "SM_Test_Pottery_a_02", "mode": "user_seams",
        "user_seam_edges": [16], "user_protected_edges": [16, 999999], "chapters": [],
    }
    v = contract.normalize_and_validate_spec(spec, edge_count=1000,
                                             object_name="SM_Test_Pottery_a_02")
    assert v["valid"] is False
    assert v["object_mismatch"] is False
    assert v["invalid_edges"] == [999999]
    assert v["conflicts"] == [
        {"edge_id": 16, "type": "seam_and_protected", "resolution": "seam_wins"}]
    # seam wins -> protected emptied; invalid dropped.
    norm = v["normalized_spec"]
    assert norm["user_seam_edges"] == [16]
    assert norm["user_protected_edges"] == []
    UserSeamSpec.from_dict(norm)  # normalized spec is loadable


def test_validate_clean_spec_is_valid():
    spec = contract.make_seam_spec(
        object_name="Obj", user_seam_edges=[1, 2, 3], user_protected_edges=[4, 5])
    v = contract.normalize_and_validate_spec(spec, edge_count=100, object_name="Obj")
    assert v["valid"] is True
    assert v["invalid_edges"] == []
    assert v["conflicts"] == []
    assert v["user_seam_count"] == 3
    assert v["user_protected_count"] == 2


def test_object_mismatch_detected():
    spec = contract.make_seam_spec(object_name="OtherObject", user_seam_edges=[1])
    v = contract.normalize_and_validate_spec(spec, edge_count=10, object_name="SelectedObject")
    assert v["object_mismatch"] is True
    assert v["valid"] is False  # mismatch blocks "valid" (apply is gated, plan §4)


def test_invalid_edges_dropped_and_reported():
    spec = contract.make_seam_spec(
        object_name="Obj", user_seam_edges=[2, 50], user_protected_edges=[-1, 7])
    v = contract.normalize_and_validate_spec(spec, edge_count=10, object_name="Obj")
    assert v["invalid_edges"] == [-1, 50]
    assert v["normalized_spec"]["user_seam_edges"] == [2]
    assert v["normalized_spec"]["user_protected_edges"] == [7]


def test_seam_wins_over_protected_for_same_edge():
    spec = contract.make_seam_spec(
        object_name="Obj", user_seam_edges=[9], user_protected_edges=[9])
    v = contract.normalize_and_validate_spec(spec, edge_count=20, object_name="Obj")
    assert v["conflicts"][0]["resolution"] == "seam_wins"
    assert v["normalized_spec"]["user_seam_edges"] == [9]
    assert v["normalized_spec"]["user_protected_edges"] == []


def test_validate_without_edge_count_skips_range_check():
    """When no mesh is available (edge_count=None) nothing is 'invalid' for range."""
    spec = contract.make_seam_spec(object_name="Obj", user_seam_edges=[999999])
    v = contract.normalize_and_validate_spec(spec, edge_count=None, object_name="Obj")
    assert v["invalid_edges"] == []
    assert v["normalized_spec"]["user_seam_edges"] == [999999]


def test_status_and_envelope_helpers():
    status = contract.new_status(run_id="s1", command=contract.CMD_EXPORT_EDGE_GEOMETRY)
    assert status["status"] == contract.STATUS_QUEUED
    contract.finalize_status(status, status=contract.STATUS_ACCEPTED,
                             artifacts={"edge_geometry": "edge_geometry.json"})
    assert status["status"] == contract.STATUS_ACCEPTED
    assert status["finished_at"] is not None

    env = contract.error_envelope(contract.CMD_SAVE_USER_SEAM_SPEC, "boom", code="exception")
    assert env["status"] == contract.STATUS_FAILED
    assert env["error"]["code"] == "exception"
    json.loads(json.dumps(env))  # serializable


def test_chapters_pass_through_untouched():
    spec = {
        "version": 1, "object": "Obj", "mode": "user_seams",
        "user_seam_edges": [1], "user_protected_edges": [],
        "chapters": [{"name": "face", "face_ids": [1, 2], "seam_edges": [], "protected_edges": []}],
    }
    v = contract.normalize_and_validate_spec(spec, edge_count=10, object_name="Obj")
    assert v["normalized_spec"]["chapters"] == spec["chapters"]
