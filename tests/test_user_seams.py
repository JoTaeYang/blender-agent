"""User-Guided Seam UV Pipeline — pure-helper tests (USER_GUIDED_SEAM_UV_PIPELINE_PLAN §11.1).

Blender-free. Covers ``artist_uv_agent.user_seams``: spec JSON load/save round-trip, invalid
edge-id detection, the plan §7 precedence (mandatory 90° > user_seam > user_protected, with
mandatory winning a protected/mandatory conflict), the final seam-set assembly, and the
``user_seams`` report block (including chapters merged into the effective edge sets). The real
Blender unwrap/pack is exercised by the worker regression run; here we test the decisions.
"""

import json

from artist_uv_agent.user_seams import (
    UserChapter, UserSeamSpec, build_user_seam_set, load_user_seam_spec,
    save_user_seam_spec,
)
from chart_uv_agent.fixtures import build_folded_planes
from chart_uv_agent.segmentation import mandatory_seam_edges


def _smooth_edge(mesh) -> int:
    """An interior, non-mandatory (<90°) edge id — a valid candidate for a user/protected seam."""
    return next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle < 90.0)


def _mandatory_edge(mesh) -> int:
    return sorted(mandatory_seam_edges(mesh, fold_angle=90.0))[0]


# -- spec load/save -----------------------------------------------------------

def test_spec_round_trips_through_json(tmp_path):
    spec = UserSeamSpec(
        object="humanstatue_low", mandatory_fold_angle=90.0,
        user_seam_edges={123, 456, 789}, user_protected_edges={3054},
        chapters=[UserChapter(name="face", face_ids=[1, 2, 3],
                              seam_edges=[123, 456], protected_edges=[777])],
        notes="User-authored seam plan")
    path = tmp_path / "spec.json"
    save_user_seam_spec(spec, str(path))
    loaded = load_user_seam_spec(str(path))
    assert loaded.object == "humanstatue_low"
    assert loaded.mode == "user_seams"
    assert loaded.user_seam_edges == {123, 456, 789}
    assert loaded.user_protected_edges == {3054}
    assert len(loaded.chapters) == 1
    assert loaded.chapters[0].name == "face"
    assert loaded.chapters[0].protected_edges == [777]
    assert loaded.notes == "User-authored seam plan"


def test_from_dict_accepts_plan_example_schema():
    """The exact §6 example JSON loads without error."""
    raw = {
        "version": 1, "object": "humanstatue_low", "mode": "user_seams",
        "mandatory_fold_angle": 90.0,
        "user_seam_edges": [123, 456, 789], "user_protected_edges": [3054],
        "chapters": [{"name": "face", "face_ids": [1, 2, 3],
                      "seam_edges": [123, 456], "protected_edges": [777]}],
        "notes": "User-authored seam plan",
    }
    spec = UserSeamSpec.from_dict(json.loads(json.dumps(raw)))
    assert spec.version == 1
    assert spec.effective_seam_edges() == {123, 456, 789}      # chapter seams already a subset
    assert spec.effective_protected_edges() == {3054, 777}     # chapter protected merged in


# -- invalid edge detection (§11.1) ------------------------------------------

def test_invalid_edge_ids_are_detected_and_dropped():
    mesh = build_folded_planes(n=6)
    n = mesh.edge_count
    good = _smooth_edge(mesh)
    spec = UserSeamSpec(user_seam_edges={good, n + 100}, user_protected_edges={-5})
    res = build_user_seam_set(mesh, spec)
    assert res.invalid_edges == [-5, n + 100]
    assert good in res.user_seam_edges
    assert (n + 100) not in res.user_seam_edges          # dropped, not shipped
    assert good in res.initial_seams


# -- precedence (§7) ----------------------------------------------------------

def test_mandatory_wins_over_user_protected_and_is_reported():
    """A ≥90° fold the user marked protected still ships as a seam (mandatory wins)."""
    mesh = build_folded_planes(n=6)
    fold = _mandatory_edge(mesh)
    spec = UserSeamSpec(user_protected_edges={fold})
    res = build_user_seam_set(mesh, spec)
    assert fold in res.initial_seams                     # mandatory wins
    assert fold not in res.forbidden_edges               # not forbidden — it must ship
    assert res.conflicts == [{"edge_id": fold, "user_rule": "protected",
                              "engine_rule": "mandatory_90", "resolution": "mandatory_wins"}]


def test_user_seam_edges_are_in_final_seam_set():
    mesh = build_folded_planes(n=6)
    smooth = _smooth_edge(mesh)
    res = build_user_seam_set(mesh, UserSeamSpec(user_seam_edges={smooth}))
    assert smooth in res.initial_seams
    # every mandatory fold is also present (mandatory always seams).
    assert mandatory_seam_edges(mesh, fold_angle=90.0) <= res.initial_seams


def test_non_mandatory_protected_excluded_from_seams_and_forbidden():
    """A smooth protected edge is NOT a seam and IS forbidden (engine must never cut it)."""
    mesh = build_folded_planes(n=6)
    smooth = _smooth_edge(mesh)
    res = build_user_seam_set(mesh, UserSeamSpec(user_protected_edges={smooth}))
    assert smooth not in res.initial_seams
    assert smooth in res.forbidden_edges


def test_user_seam_wins_when_edge_is_both_seam_and_protected():
    """Contradictory input: higher-precedence user_seam wins over user_protected (§7)."""
    mesh = build_folded_planes(n=6)
    smooth = _smooth_edge(mesh)
    res = build_user_seam_set(
        mesh, UserSeamSpec(user_seam_edges={smooth}, user_protected_edges={smooth}))
    assert smooth in res.initial_seams
    assert smooth not in res.forbidden_edges


# -- chapters + report block (§9) --------------------------------------------

def test_chapter_seams_merge_into_final_set():
    mesh = build_folded_planes(n=6)
    smooth = _smooth_edge(mesh)
    spec = UserSeamSpec(chapters=[UserChapter(name="face", seam_edges=[smooth])])
    res = build_user_seam_set(mesh, spec)
    assert smooth in res.user_seam_edges
    assert smooth in res.initial_seams


def test_report_block_counts():
    mesh = build_folded_planes(n=6)
    smooth = _smooth_edge(mesh)
    fold = _mandatory_edge(mesh)
    spec = UserSeamSpec(user_seam_edges={smooth}, user_protected_edges={fold})
    res = build_user_seam_set(mesh, spec)
    block = res.report()
    assert block["user_seam_count"] == 1
    assert block["user_protected_count"] == 1
    assert block["mandatory_90_edges"] == len(mandatory_seam_edges(mesh, fold_angle=90.0))
    assert block["auto_added_seams"] == 0
    assert block["user_seam_edges"] == [smooth]          # actual edge lists, not just counts
    assert block["user_protected_edges"] == [fold]
    assert len(block["conflicts"]) == 1                  # fold protected → mandatory_wins
    assert block["invalid_edges"] == []
    # final_seam_count reflects a supplied shipped set when given.
    shipped = set(res.initial_seams) | {smooth}
    assert res.report(final_seams=shipped)["final_seam_count"] == len(shipped)
