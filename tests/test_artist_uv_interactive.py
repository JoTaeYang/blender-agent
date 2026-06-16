"""Interactive chapter seam planning — Blender-free unit tests (GUIDED_UV_CHAPTER_PLAN
interactive front-end §테스트 계획).

Covers the five plan success criteria as a per-part planning LOOP (the SLIM unwrap is the
only Blender step and is exercised by the ``.context`` runner / worker, not here):

  1 observe a chapter                         → ``test_observe_*``
  2 draft a face seam plan                     → ``test_draft_face_plan_*``
  3 save an approved plan to JSON              → ``test_interactive_plan_roundtrip`` / upsert
  4 only APPROVED chapters export to the spec  → ``test_interactive_plan_exports_only_approved``
  5 report whether the result kept the rules   → ``test_interactive_constraints_report_*``
"""

import math

import pytest

from artist_uv_agent.guided import (
    assign_chapters, build_guided_assignment, build_guided_report, build_guided_seams,
    coarse_segment_parts, map_charts_to_chapters,
)
from artist_uv_agent.interactive_plan import (
    ChapterConstraints, ChapterIntent, ChapterSource, InteractiveChapterPlan,
    InteractiveUVPlan, ObservationSummary, describe_plan_for_approval, draft_seam_plan,
    evaluate_interactive_constraints, observe_chapter,
)
from chart_uv_agent.fixtures import build_humanoid_blob
from chart_uv_agent.gate import ChartGateConfig, evaluate_chart_gate
from chart_uv_agent.segmentation import flood_charts
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_grid_plane


# -- fixtures ----------------------------------------------------------------

def _cylinder(seg=20, h=12):
    verts = [(math.cos(2 * math.pi * s / seg), math.sin(2 * math.pi * s / seg), k / h * 6.0)
             for k in range(h + 1) for s in range(seg)]
    faces = [(k * seg + s, k * seg + (s + 1) % seg, (k + 1) * seg + (s + 1) % seg, (k + 1) * seg + s)
             for k in range(h) for s in range(seg)]
    return MeshGraph.from_faces("cyl", verts, faces)


def _coarse_classes(seg):
    return [type("C", (), {"part_id": p.part_id, "type": "coarse"})() for p in seg.parts]


def _pure_backend(mesh, spec):
    """Run the guided BACK-END (pure / no Blender) via the SAME preparation as run_guided_uv
    (segment + face-set/selector carve + assign) and return the pieces the interactive
    constraint check needs: ``(assignment, chapter_charts, seams)``."""
    seg, descs, classes, spec2, assignment = build_guided_assignment(mesh, spec)
    built = build_guided_seams(mesh, seg, descs, classes, spec2, assignment)
    _, _, cc = map_charts_to_chapters(mesh, built.seams, seg.face_part, assignment.part_chapter)
    return assignment, cc, built.seams


def _biggest_part(mesh):
    seg = coarse_segment_parts(mesh)
    return max(seg.parts, key=lambda p: len(p.face_ids)).part_id


# -- criterion 1: observe ----------------------------------------------------

def test_observe_chapter_reports_pure_mesh_stats():
    mesh = build_humanoid_blob(segments=16, rings=16)
    src = ChapterSource(part_ids=[_biggest_part(mesh)])
    obs = observe_chapter(mesh, None, "body", src, front_axis="+Z", up_axis="+Y",
                          threshold=0.4, max_dihedral=45.0)
    assert obs.chapter == "body"
    assert obs.face_count > 0
    assert obs.face_count == len(src.resolve_faces(mesh))
    assert set(obs.bbox) == {"min", "max"}
    assert obs.boundary_loop_count >= 0
    assert obs.front_smooth_edge_count >= 0
    assert sum(obs.normal_axis_histogram.values()) == obs.face_count   # every face binned once
    # round-trips through dict
    assert ObservationSummary.from_dict(obs.to_dict()).to_dict() == obs.to_dict()


def test_observe_explicit_face_ids_override_parts():
    mesh = _cylinder()
    faces = [0, 1, 2, 3]
    obs = observe_chapter(mesh, None, "patch", ChapterSource(face_ids=faces), front_axis="+Z")
    assert obs.face_count == 4


def test_observe_closed_cap_flags_risk():
    # a closed surface (no boundary loop) → the disk-cut risk flag fires.
    from chart_uv_agent.fixtures import build_humanoid_blob as blob
    mesh = blob(segments=14, rings=14)
    src = ChapterSource(face_ids=[f.id for f in mesh.faces])     # whole closed mesh
    obs = observe_chapter(mesh, None, "whole", src, front_axis="+Z")
    assert obs.boundary_loop_count == 0
    assert any("closed_cap" in r for r in obs.risk_flags)


# -- criterion 2: draft a face plan ------------------------------------------

def test_draft_face_plan_contains_required_constraints():
    mesh = build_humanoid_blob(segments=16, rings=16)
    obs = observe_chapter(mesh, None, "face", ChapterSource(part_ids=[_biggest_part(mesh)]),
                          front_axis="+Z", up_axis="+Y")
    draft = draft_seam_plan(obs, "face")
    assert draft.status == "draft"
    assert draft.kind == "face"
    assert draft.guided_type == "face_front_preserve"
    c = draft.constraints.to_dict()
    assert c["max_front_smooth_seams"] == 0
    assert c["mandatory_folds_must_split"] is True
    assert "face" in draft.intent.summary.lower()
    assert "face_front_center" in draft.intent.preserve_zones
    # the source carried by the observation flows into the draft (so approve→export works)
    assert draft.source.part_ids == [_biggest_part(mesh)]
    # the human-readable summary mentions the goal + the hard rule
    text = describe_plan_for_approval(draft)
    assert "max_front_smooth_seams=0" in text


def test_draft_respects_artist_preferences_override():
    mesh = build_humanoid_blob(segments=12, rings=12)
    obs = observe_chapter(mesh, None, "face", ChapterSource(part_ids=[0]), front_axis="+Z")
    draft = draft_seam_plan(obs, "face",
                            artist_preferences={"constraints": {"allow_beard_island": False},
                                                "seam_policy": "custom_policy"})
    assert draft.constraints.get("allow_beard_island") is False
    assert draft.constraints.get("max_front_smooth_seams") == 0   # template default kept
    assert draft.seam_policy == "custom_policy"


def test_draft_lower_robe_has_panel_bounds():
    mesh = build_humanoid_blob(segments=12, rings=12)
    obs = observe_chapter(mesh, None, "lower_robe", ChapterSource(part_ids=[0]))
    draft = draft_seam_plan(obs, "lower_robe")
    assert draft.guided_type == "cloth_panels"
    assert draft.constraints.get("min_panel_count") == 3
    assert draft.constraints.get("max_panel_count") == 6


# -- criterion 3: save / round-trip / revision -------------------------------

def test_interactive_plan_roundtrip(tmp_path):
    plan = InteractiveUVPlan(object="humanstatue_low", front_axis="+Z", up_axis="+Y",
                             forbidden_edges=[3054], segmentation_mode="coarse")
    plan.upsert_chapter(InteractiveChapterPlan(
        name="face", kind="face", status="approved", guided_type="face_front_preserve",
        seam_policy="single_front_island",
        source=ChapterSource(selection_type="face_set", part_ids=[2, 3]),
        intent=ChapterIntent(summary="Keep the face front as one island.",
                             preserve_zones=["nose"], preferred_seam_zones=["under_chin"]),
        constraints=ChapterConstraints({"max_front_smooth_seams": 0}),
        selector={"normal_axis": "+Z", "threshold": 0.3}, user_notes="hi"))
    # dict / json round-trips are stable
    assert InteractiveUVPlan.from_dict(plan.to_dict()).to_dict() == plan.to_dict()
    assert InteractiveUVPlan.from_json(plan.to_json()).to_dict() == plan.to_dict()
    # save → load on disk
    path = tmp_path / "interactive_uv_plan.json"
    plan.save(str(path))
    assert InteractiveUVPlan.load(str(path)).to_dict() == plan.to_dict()
    # coerce accepts plan / dict / json / path
    assert InteractiveUVPlan.coerce(plan) is plan
    assert InteractiveUVPlan.coerce(plan.to_dict()).to_dict() == plan.to_dict()
    assert InteractiveUVPlan.coerce(plan.to_json()).to_dict() == plan.to_dict()
    assert InteractiveUVPlan.coerce(str(path)).to_dict() == plan.to_dict()


def test_interactive_plan_upsert_chapter_revision():
    plan = InteractiveUVPlan(object="o")
    plan.upsert_chapter(InteractiveChapterPlan(name="face", user_notes="artist note"))
    assert plan.get_chapter("face").revision == 1
    # re-draft the SAME chapter → revision bumps; empty user_notes preserves the old note
    plan.upsert_chapter(InteractiveChapterPlan(name="face", seam_policy="v2"))
    ch = plan.get_chapter("face")
    assert ch.revision == 2
    assert ch.seam_policy == "v2"
    assert ch.user_notes == "artist note"          # preserved across the revision
    assert plan.current_chapter == "face"
    assert len(plan.chapters) == 1                  # upsert replaced, not appended
    # status transitions do NOT bump the revision (approve is not a content edit)
    plan.set_status("face", "approved")
    assert plan.get_chapter("face").status == "approved"
    assert plan.get_chapter("face").revision == 2


def test_set_status_validates():
    plan = InteractiveUVPlan(object="o")
    plan.upsert_chapter(InteractiveChapterPlan(name="face"))
    with pytest.raises(ValueError):
        plan.set_status("face", "bogus")
    with pytest.raises(KeyError):
        plan.set_status("missing", "approved")


# -- criterion 4: only approved chapters export ------------------------------

def test_interactive_plan_exports_only_approved_chapters():
    plan = InteractiveUVPlan(object="o", front_axis="+Z", forbidden_edges=[3054],
                             segmentation_mode="coarse")
    plan.upsert_chapter(InteractiveChapterPlan(
        name="face", guided_type="face_front_preserve", status="approved",
        source=ChapterSource(part_ids=[2, 3]), seam_policy="front_island"))
    plan.upsert_chapter(InteractiveChapterPlan(
        name="hands", guided_type="blob", status="draft", source=ChapterSource(part_ids=[9])))
    plan.upsert_chapter(InteractiveChapterPlan(
        name="sleeve", guided_type="cylinder", status="needs_revision",
        source=ChapterSource(part_ids=[7])))
    spec = plan.to_guided_spec()
    assert [c.name for c in spec.chapters] == ["face"]          # only the approved one
    ch = spec.chapters[0]
    assert ch.type == "face_front_preserve"
    assert ch.source_part_ids == [2, 3]
    assert ch.seam_policy == "front_island"
    # asset-level fields flow through
    assert spec.front_preserve_axis == "+Z"
    assert spec.forbidden_edges == [3054]
    assert spec.segmentation_mode == "coarse"
    # not penalised for parts not yet reached (empty expected_intents by default)
    assert spec.expected_intents == []


def test_export_carries_selector():
    plan = InteractiveUVPlan(object="o", front_axis="+Z")
    plan.upsert_chapter(InteractiveChapterPlan(
        name="upper_front_robe", guided_type="robe_front_panel", status="approved",
        source=ChapterSource(part_ids=[5]), selector={"normal_axis": "+Z", "threshold": 0.3}))
    spec = plan.to_guided_spec()
    assert spec.chapters[0].selector == {"normal_axis": "+Z", "threshold": 0.3}


def test_export_empty_when_nothing_approved():
    plan = InteractiveUVPlan(object="o")
    plan.upsert_chapter(InteractiveChapterPlan(name="face", status="draft"))
    assert plan.to_guided_spec().chapters == []


# -- criterion 5: constraint verification (pass + fail) ----------------------

def _plan_with_chapter(name, part_ids, guided_type, constraints, *, front_axis="+Z"):
    plan = InteractiveUVPlan(object="o", front_axis=front_axis, up_axis="+Y",
                             segmentation_mode="coarse")
    plan.upsert_chapter(InteractiveChapterPlan(
        name=name, guided_type=guided_type, status="approved",
        source=ChapterSource(part_ids=list(part_ids)),
        constraints=ChapterConstraints(dict(constraints))))
    return plan


def test_interactive_constraints_report_failure():
    # A single-island flat panel cannot satisfy "at least 5 panels" → a HONEST failure.
    mesh = build_grid_plane(nx=8, ny=8)
    plan = _plan_with_chapter("lower_robe", [0], "cloth_panel", {"min_panel_count": 5})
    assignment, cc, seams = _pure_backend(mesh, plan.to_guided_spec())
    block = evaluate_interactive_constraints(plan, mesh, assignment, cc, seams,
                                             front_axis="+Z", up_axis="+Y")
    assert block["interactive_constraints_passed"] is False
    r = block["constraint_results"]["lower_robe"]["constraints"]["min_panel_count"]
    assert r["checkable"] is True and r["passed"] is False
    assert r["actual"] == 1 and r["expected"] == 5
    assert block["approved_chapters"] == ["lower_robe"]


def test_interactive_constraints_report_pass():
    mesh = build_grid_plane(nx=8, ny=8)
    plan = _plan_with_chapter("front", [0], "face_front_preserve",
                              {"max_front_smooth_seams": 0, "max_panel_count": 10})
    assignment, cc, seams = _pure_backend(mesh, plan.to_guided_spec())
    block = evaluate_interactive_constraints(plan, mesh, assignment, cc, seams,
                                             front_axis="+Z", up_axis="+Y")
    # a flat panel keeps 0 internal seams → no front-smooth seam crosses it, 1 island ≤ 10
    assert block["interactive_constraints_passed"] is True
    cons = block["constraint_results"]["front"]["constraints"]
    assert cons["max_front_smooth_seams"]["actual"] == 0
    assert cons["max_front_smooth_seams"]["passed"] is True
    assert cons["max_panel_count"]["passed"] is True
    assert block["checked_constraint_count"] == 2


def test_advisory_constraint_does_not_gate():
    # An unrecognised rule is reported (checkable=false) but never fails the gate.
    mesh = build_grid_plane(nx=6, ny=6)
    plan = _plan_with_chapter("belt", [0], "strip", {"hide_seam_under_overlap": True})
    assignment, cc, seams = _pure_backend(mesh, plan.to_guided_spec())
    block = evaluate_interactive_constraints(plan, mesh, assignment, cc, seams)
    r = block["constraint_results"]["belt"]["constraints"]["hide_seam_under_overlap"]
    assert r["checkable"] is False
    assert block["checked_constraint_count"] == 0
    assert block["interactive_constraints_passed"] is True      # nothing checkable → no violation


def test_constraint_chapter_not_resolved_is_interactive_failure():
    # An approved chapter whose name does not appear in the guided result is flagged AND fails
    # the interactive gate (it must never read as success — user request).
    mesh = build_grid_plane(nx=6, ny=6)
    plan = _plan_with_chapter("ghost", [999], "auto", {"max_panel_count": 1})
    # build the backend from a DIFFERENT (empty) spec so 'ghost' is absent from the assignment
    from artist_uv_agent.guided import GuidedUVSpec
    assignment, cc, seams = _pure_backend(mesh, GuidedUVSpec(chapters=[]))
    block = evaluate_interactive_constraints(plan, mesh, assignment, cc, seams)
    assert block["constraint_results"]["ghost"]["chapter_resolved"] is False
    assert block["interactive_constraints_passed"] is False
    assert block["unresolved_approved_chapters"] == ["ghost"]


def test_unresolved_part_ids_is_interactive_failure():
    # The chapter IS in the spec but its part ids don't exist → it resolves no faces → failure.
    mesh = build_grid_plane(nx=6, ny=6)
    plan = _plan_with_chapter("ghost", [999], "cloth_panel", {"max_panel_count": 1})
    assignment, cc, seams = _pure_backend(mesh, plan.to_guided_spec())
    block = evaluate_interactive_constraints(plan, mesh, assignment, cc, seams)
    assert block["constraint_results"]["ghost"]["chapter_resolved"] is False
    assert block["interactive_constraints_passed"] is False
    assert "ghost" in block["unresolved_approved_chapters"]


# -- face_ids selection survives to the guided backend (user request) --------

def test_face_ids_selection_survives_to_backend():
    # A chapter defined by EXPLICIT face ids must reach the part-based backend: those faces are
    # carved into their own part, so the approved chapter resolves with exactly that face set.
    mesh = build_grid_plane(nx=8, ny=8)
    faces = [f.id for f in mesh.faces][:20]
    plan = InteractiveUVPlan(object="o", front_axis="+Z", up_axis="+Y", segmentation_mode="coarse")
    plan.upsert_chapter(InteractiveChapterPlan(
        name="patch", kind="generic", guided_type="cloth_panel", status="approved",
        source=ChapterSource(selection_type="face_ids", face_ids=faces),
        constraints=ChapterConstraints({"max_panel_count": 50})))
    spec = plan.to_guided_spec()
    assert spec.chapters[0].source_face_ids == faces        # face ids carried into the spec
    assignment, cc, seams = _pure_backend(mesh, spec)
    block = evaluate_interactive_constraints(plan, mesh, assignment, cc, seams,
                                             front_axis="+Z", up_axis="+Y")
    res = block["constraint_results"]["patch"]
    assert res["chapter_resolved"] is True
    assert res["face_count"] == 20                          # exactly the selected faces
    assert block["interactive_constraints_passed"] is True


def test_face_selection_carve_creates_own_part():
    # Directly: apply_chapter_face_selection pulls the named faces into a fresh part and rewrites
    # the chapter's source_part_ids to it (face ids consumed).
    from artist_uv_agent.guided import GuidedChapter, GuidedUVSpec, apply_chapter_face_selection
    mesh = build_grid_plane(nx=8, ny=8)
    faces = [f.id for f in mesh.faces][:15]
    seg = coarse_segment_parts(mesh)
    spec = GuidedUVSpec(segmentation_mode="coarse",
                        chapters=[GuidedChapter("patch", [], "cloth_panel", "",
                                                source_face_ids=faces)])
    seg2, spec2 = apply_chapter_face_selection(mesh, seg, spec)
    assert seg2 is not seg
    new_part = spec2.chapters[0].source_part_ids
    assert len(new_part) == 1 and spec2.chapters[0].source_face_ids == []
    carved = [f for f, p in seg2.face_part.items() if p == new_part[0]]
    assert sorted(carved) == sorted(faces)                  # exactly the selected faces, own part


def test_guided_chapter_source_face_ids_round_trip():
    from artist_uv_agent.guided import GuidedChapter, GuidedUVSpec
    spec = GuidedUVSpec(chapters=[GuidedChapter("p", [], "cloth_panel", "",
                                                source_face_ids=[1, 2, 3])])
    assert GuidedUVSpec.from_dict(spec.to_dict()).chapters[0].source_face_ids == [1, 2, 3]
    assert GuidedUVSpec.from_json(spec.to_json()).chapters[0].source_face_ids == [1, 2, 3]


def test_mandatory_folds_constraint_passes_on_guided_seams():
    # The back-end re-asserts the mandatory ≥fold union, so a face chapter's folds are all cut.
    mesh = build_humanoid_blob(segments=16, rings=16)
    plan = _plan_with_chapter("face", [_biggest_part(mesh)], "face_front_preserve",
                              {"mandatory_folds_must_split": True})
    assignment, cc, seams = _pure_backend(mesh, plan.to_guided_spec())
    block = evaluate_interactive_constraints(plan, mesh, assignment, cc, seams, front_axis="+Z")
    r = block["constraint_results"]["face"]["constraints"]["mandatory_folds_must_split"]
    assert r["checkable"] is True and r["passed"] is True and r["actual"] == 0


# -- report integration: interactive block tightens guided_complete ----------

_GOOD_GATE = {
    "mandatory_90_missing": 0, "mandatory_90_uv_unsplit": 0, "overlap_ratio": 0.0,
    "raster_overlap_ratio": 0.001, "stretch_score": 0.3, "worst_island_distortion": 0.45,
    "texel_density_variance": 0.5, "island_count": 12, "uv_bounds_ok": True,
    "fallback_used": False, "packing_efficiency": 0.5, "small_island_ratio": 0.0,
    "vt_v_ratio": 1.2, "convexity_mean": 0.9, "convexity_p10": 0.8,
    "boundary_smoothness_mean": 1.0, "tendril_count": 0,
}


def _cyl_report(interactive):
    from artist_uv_agent.guided import GuidedChapter, GuidedUVSpec
    mesh = _cylinder()
    spec = GuidedUVSpec(chapters=[GuidedChapter("shaft", [0], "cylinder", "")])
    seg = coarse_segment_parts(mesh)
    classes = _coarse_classes(seg)
    assignment = assign_chapters(seg, [], classes, spec)
    built = build_guided_seams(mesh, seg, [], classes, spec, assignment)
    ch = len(flood_charts(mesh, built.seams))
    return build_guided_report(mesh, spec, assignment, built, {},
                               evaluate_chart_gate(_GOOD_GATE, config=ChartGateConfig()),
                               built.seam_origin, pruned=[], chart_count=ch,
                               interactive=interactive)


def test_report_embeds_interactive_block_and_blocks_completion_on_failure():
    fail = {"interactive_constraints_passed": False, "approved_chapters": ["shaft"],
            "constraint_results": {"shaft": {"chapter_resolved": True,
            "constraints": {"max_front_smooth_seams": {"checkable": True, "passed": False}}}}}
    rep = _cyl_report(fail)
    assert rep["uv_shippable"] is True                 # the UV itself is fine
    assert rep["guided_complete"] is False             # but an approved rule was broken
    assert rep["completion_status"] == "accepted_with_unmet_interactive_constraints"
    assert rep["interactive_plan"] is fail
    assert rep["interactive_constraints_passed"] is False
    assert any("unmet interactive constraints" in w for w in rep["warnings"])
    assert any("shaft.max_front_smooth_seams" in w for w in rep["warnings"])


def test_report_interactive_pass_allows_completion():
    ok = {"interactive_constraints_passed": True, "approved_chapters": ["shaft"],
          "constraint_results": {"shaft": {"chapter_resolved": True, "constraints": {}}}}
    rep = _cyl_report(ok)
    assert rep["guided_complete"] is True
    assert rep["completion_status"] == "guided_complete"
    assert rep["interactive_constraints_passed"] is True


def test_report_without_interactive_is_unchanged():
    # the interactive arg is opt-in: omitting it leaves the report keys absent and the
    # completion logic identical to the non-interactive guided flow.
    rep = _cyl_report(None)
    assert "interactive_plan" not in rep
    assert rep["guided_complete"] is True               # cylinder policy reflected + gate green


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
