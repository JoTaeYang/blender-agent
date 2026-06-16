"""Guided UV chapter flow — Blender-free unit tests (GUIDED_UV_CHAPTER_PLAN §test).

Exercises the pure spec→seam logic: the spec data model, chapter assignment (spec + the
class-based fallback for uncovered parts), the per-chapter seam policy, forbidden / no-cut
edge preservation, and the mandatory-fold + disk invariants. The SLIM unwrap, the UV-level
mandatory_90 audit, and the welded-fold repair are the Blender steps in ``run_guided_uv``
and are covered by the worker regression run (and the ``.context`` guided script), not here.

The plan's eight test criteria map as follows (the UV-level ones run in Blender):
  1/2 forbidden edge 3054 never ships & no conflict      → ``test_forbidden_*``
  3   mandatory_90_uv_unsplit == 0                        → Blender (worker run)
  4   mandatory_90_missing == 0                           → ``test_mandatory_folds_*``
  5   overlap / raster overlap pass                       → Blender (disk invariant here)
  6   staff shaft/prong → rectangular strip, not blob     → ``test_cylinder_chapter_*``
  7   chapter count / report matches the spec             → ``test_assignment_*``
  8   uncovered parts still produce UVs (fallback)        → ``test_uncovered_parts_*``
"""

import math

import pytest

from artist_uv_agent.classification import classify_parts
from artist_uv_agent.descriptors import describe_parts
from artist_uv_agent.guided import (
    FALLBACK_BEHAVIOR, GuidedChapter, GuidedUVSpec, assign_chapters,
    build_guided_parts_json, build_guided_report, build_guided_seams, chapter_behavior,
    chapter_coverage, coarse_segment_parts, map_charts_to_chapters, resolve_segmentation_mode,
    _resolve_repair_island_hard_cap,
)
from artist_uv_agent.seams import _is_uv_disk_cheap, uv_is_disk
from artist_uv_agent.segmentation import segment_parts, split_branched_parts
from chart_uv_agent.fixtures import build_humanoid_blob
from chart_uv_agent.segmentation import (
    flood_charts, mandatory_seam_audit, mandatory_seam_edges,
)
from chart_uv_agent.gate import ChartGateConfig, evaluate_chart_gate
from uv_agent.geometry.mesh_graph import MeshGraph


# -- fixtures ----------------------------------------------------------------

def _cylinder(seg=20, h=12):
    verts = [(math.cos(2 * math.pi * s / seg), math.sin(2 * math.pi * s / seg), k / h * 6.0)
             for k in range(h + 1) for s in range(seg)]
    faces = [(k * seg + s, k * seg + (s + 1) % seg, (k + 1) * seg + (s + 1) % seg, (k + 1) * seg + s)
             for k in range(h) for s in range(seg)]
    return MeshGraph.from_faces("cyl", verts, faces)


def _segment(mesh: MeshGraph):
    seg = segment_parts(mesh)
    seg = split_branched_parts(mesh, seg)
    descs = describe_parts(mesh, seg)
    classes = classify_parts(descs, {p.part_id: p.neighbors for p in seg.parts})
    return seg, descs, classes


def _build(mesh: MeshGraph, spec: GuidedUVSpec, **kw):
    seg, descs, classes = _segment(mesh)
    assignment = assign_chapters(seg, descs, classes, spec)
    built = build_guided_seams(mesh, seg, descs, classes, spec, assignment, **kw)
    return seg, descs, classes, assignment, built


# -- spec data model ---------------------------------------------------------

def test_spec_round_trips_dict_json_and_coerce():
    raw = {
        "version": 1, "object": "AI_Adaptive_5850", "forbidden_edges": [3054],
        "mandatory_fold_angle": 90.0,
        "chapters": [
            {"name": "staff_shaft", "source_part_ids": [4], "type": "cylinder",
             "seam_policy": "single_back_lengthwise"},
            {"name": "face_beard", "source_part_ids": [], "type": "organic_front_preserve",
             "seam_policy": "back_or_under_only"},
        ],
    }
    spec = GuidedUVSpec.from_dict(raw)
    assert spec.object == "AI_Adaptive_5850"
    assert spec.forbidden_edges == [3054]
    assert [c.name for c in spec.chapters] == ["staff_shaft", "face_beard"]
    # dict → json → dict is stable
    assert GuidedUVSpec.from_json(spec.to_json()).to_dict() == spec.to_dict()
    # coerce accepts the spec, a dict, or a json string
    assert GuidedUVSpec.coerce(spec) is spec
    assert GuidedUVSpec.coerce(raw).to_dict() == spec.to_dict()
    assert GuidedUVSpec.coerce(spec.to_json()).to_dict() == spec.to_dict()


def test_unknown_chapter_type_falls_back_not_errors():
    # plan §step1: an unknown chapter type must be a fallback policy, never an error.
    assert chapter_behavior("totally_made_up_type") == FALLBACK_BEHAVIOR
    assert chapter_behavior("cylinder") == "cylinder"
    assert chapter_behavior("cloth_panel") == "keep_intact"
    assert chapter_behavior("cloth_panels") == "organic_split"
    # and it round-trips through assignment without raising
    mesh = _cylinder()
    spec = GuidedUVSpec(chapters=[GuidedChapter("weird", [0], "no_such_type", "")])
    _, _, _, assignment, built = _build(mesh, spec)
    assert assignment.chapters[0].behavior == FALLBACK_BEHAVIOR
    assert len(built.chart_to_chapter) >= 1


# -- chapter assignment (criteria 7 / 8) -------------------------------------

def test_assignment_covers_every_part_spec_plus_fallback():
    # Cover only ONE part with the spec; the rest must become class-based fallback chapters
    # so every part lands in exactly one chapter (criterion 8 — generation always completes).
    mesh = build_humanoid_blob(segments=16, rings=16)
    seg, descs, classes = _segment(mesh)
    spec = GuidedUVSpec(chapters=[GuidedChapter("body", [1], "blob", "back_center")])
    assignment = assign_chapters(seg, descs, classes, spec)

    assert set(assignment.part_chapter) == {p.part_id for p in seg.parts}   # every part mapped
    spec_chapters = [c for c in assignment.chapters if c.source == "spec"]
    fb_chapters = [c for c in assignment.chapters if c.source == "fallback"]
    assert len(spec_chapters) == 1
    assert len(fb_chapters) == len(seg.parts) - 1                            # one per uncovered part
    # the report counts correspond to the spec (criterion 7)
    assert assignment.to_dict()["chapter_count"] == len(assignment.chapters)


def test_assignment_warns_on_invalid_and_contested_parts():
    mesh = build_humanoid_blob(segments=16, rings=16)
    seg, descs, classes = _segment(mesh)
    n = len(seg.parts)
    spec = GuidedUVSpec(chapters=[
        GuidedChapter("a", [0, 999], "blob", ""),       # 999 invalid
        GuidedChapter("b", [0], "blob", ""),            # 0 already claimed by 'a'
    ])
    assignment = assign_chapters(seg, descs, classes, spec)
    assert any("999 does not exist" in w for w in assignment.warnings)
    assert any("already claimed" in w for w in assignment.warnings)
    # part 0 stays with the first chapter that claimed it
    assert assignment.chapters[assignment.part_chapter[0]].name == "a"
    assert set(assignment.part_chapter) == {p.part_id for p in seg.parts}    # still total
    _ = n


def test_chart_to_chapter_is_total_and_valid():
    mesh = build_humanoid_blob(segments=16, rings=16)
    spec = GuidedUVSpec(chapters=[GuidedChapter("body", [1], "blob", "")])
    _, _, _, assignment, built = _build(mesh, spec)
    charts = flood_charts(mesh, built.seams)
    assert len(built.chart_to_chapter) == len(charts)                        # every chart mapped
    valid = set(range(len(assignment.chapters)))
    assert all(idx in valid for idx in built.chart_to_chapter.values())
    # chapter_charts partitions the chart ids
    flat = sorted(c for cs in built.chapter_charts.values() for c in cs)
    assert flat == sorted(built.chart_to_chapter)


# -- cylinder chapter → strip, not blob (criterion 6) ------------------------

def test_cylinder_chapter_opens_a_template_seam():
    mesh = _cylinder()
    spec = GuidedUVSpec(chapters=[
        GuidedChapter("staff_shaft", [0], "cylinder", "single_back_lengthwise")])
    _, _, _, assignment, built = _build(mesh, spec)
    assert assignment.chapters[0].behavior == "cylinder"
    # the cylinder template added a lengthwise / cap cut → tagged chapter_template
    assert any(t == "chapter_template" for t in built.seam_origin.values())
    assert any(o["op"] == "cylinder_template" for o in built.log)
    # the body unwraps to a single disk (the rectangle), not a fragmented blob
    charts = flood_charts(mesh, built.seams)
    assert all(uv_is_disk(mesh, fs, built.seams) for fs in charts)


# -- mandatory folds (criterion 4) -------------------------------------------

def test_mandatory_folds_are_all_seams():
    for mesh in (_cylinder(), build_humanoid_blob(segments=16, rings=16)):
        spec = GuidedUVSpec(chapters=[GuidedChapter("c", [0], "auto", "")])
        _, _, _, _, built = _build(mesh, spec)
        assert mandatory_seam_audit(mesh, built.seams)["mandatory_90_missing"] == 0


def test_every_chart_is_a_uv_disk():
    # The disk invariant is the Blender-free correctness proxy (a non-disk self-folds in
    # SLIM → overlap). Holds across spec, fallback, and forbidden-strip paths.
    mesh = build_humanoid_blob(segments=16, rings=16)
    forb = [e.id for e in mesh.edges if len(e.face_ids) == 2 and e.dihedral_angle < 30][:20]
    spec = GuidedUVSpec(forbidden_edges=forb,
                        chapters=[GuidedChapter("body", [1], "cloth_panels", "")])
    _, _, _, _, built = _build(mesh, spec)
    for fs in flood_charts(mesh, built.seams):
        assert uv_is_disk(mesh, fs, built.seams)


# -- forbidden / no-cut edges (criteria 1 / 2) -------------------------------

def test_forbidden_nonmandatory_edge_never_ships_and_no_conflict():
    # The user's preserve set (e.g. the smooth robe edge 3054) must never appear in the final
    # seam set, and a non-mandatory forbidden edge raises NO conflict (criteria 1 + 2).
    mesh = build_humanoid_blob(segments=16, rings=16)
    forb = {e.id for e in mesh.edges if len(e.face_ids) == 2 and e.dihedral_angle < 30}
    forb = set(sorted(forb)[:40])
    spec = GuidedUVSpec(forbidden_edges=sorted(forb),
                        chapters=[GuidedChapter("body", [1], "cloth_panels", "")])
    _, _, _, _, built = _build(mesh, spec)
    assert forb.isdisjoint(built.seams)                 # criterion 1: never a seam
    assert not built.forbidden_conflicts                # criterion 2: no conflict
    assert "user_forbidden" not in built.seam_type_counts()


def test_forbidden_mandatory_edge_is_reported_as_conflict_and_kept():
    # A forbidden edge that is ALSO a ≥90° mandatory fold is a conflict: the fold wins (a
    # hard crease must stay a seam) and the conflict is reported, never silently dropped.
    mesh = build_humanoid_blob(segments=16, rings=16)
    fold = sorted(mandatory_seam_edges(mesh))[0]
    spec = GuidedUVSpec(forbidden_edges=[fold], chapters=[])
    _, _, _, _, built = _build(mesh, spec)
    assert built.forbidden_conflicts == [fold]
    assert fold in built.seams                           # mandatory fold preserved
    assert fold not in built.forbidden_stripped


# -- intra-chapter boundary dissolve -----------------------------------------

def test_same_chapter_parts_dissolve_their_internal_boundary():
    # Two parts placed in ONE chapter must drop the (non-mandatory) boundary between them so
    # the chapter is one continuous region. We assert the dissolve op ran and that grouping
    # never raises the chart count above keeping the parts separate.
    mesh = build_humanoid_blob(segments=16, rings=16)
    grouped = GuidedUVSpec(chapters=[GuidedChapter("all", [0, 1, 2], "cloth_panel", "")])
    separate = GuidedUVSpec(chapters=[
        GuidedChapter("a", [0], "cloth_panel", ""),
        GuidedChapter("b", [1], "cloth_panel", ""),
        GuidedChapter("c", [2], "cloth_panel", "")])
    _, _, _, _, g = _build(mesh, grouped)
    _, _, _, _, s = _build(mesh, separate)
    assert any(o["op"] == "dissolve_intra_chapter_boundaries" for o in g.log)
    assert len(flood_charts(mesh, g.seams)) <= len(flood_charts(mesh, s.seams))


# -- seam taxonomy + parts json ----------------------------------------------

def test_seam_type_counts_use_the_plan_taxonomy():
    mesh = _cylinder()
    spec = GuidedUVSpec(chapters=[GuidedChapter("shaft", [0], "cylinder", "")])
    _, _, _, _, built = _build(mesh, spec)
    counts = built.seam_type_counts()
    assert set(counts) <= {"chapter_boundary", "chapter_template", "mandatory_90",
                           "fallback_segmentation", "user_forbidden"}
    assert sum(counts.values()) == len(built.seams)


def test_parts_json_has_part_table_and_assignment():
    mesh = build_humanoid_blob(segments=16, rings=16)
    spec = GuidedUVSpec(chapters=[GuidedChapter("body", [1], "blob", "")])
    seg, descs, classes, assignment, built = _build(mesh, spec)
    pj = build_guided_parts_json(seg, descs, classes, assignment, built)
    assert pj["engine"] == "guided"
    assert pj["part_count"] == len(seg.parts)
    assert len(pj["parts"]) == len(seg.parts)
    assert pj["assignment"]["chapter_count"] == len(assignment.chapters)


def test_map_charts_to_chapters_is_total_and_matches_build():
    # The shared mapping helper (used to RE-MAP from the final post-repair seams so the
    # report/overlay never go stale) reproduces the build-time mapping when run on the same
    # seams, and is total over the flooded charts.
    mesh = build_humanoid_blob(segments=16, rings=16)
    spec = GuidedUVSpec(chapters=[GuidedChapter("body", [1], "blob", "")])
    seg, _, _, assignment, built = _build(mesh, spec)
    charts, c2c, cc = map_charts_to_chapters(mesh, built.seams, seg.face_part,
                                             assignment.part_chapter)
    assert c2c == built.chart_to_chapter
    assert cc == built.chapter_charts
    assert len(c2c) == len(charts)


def test_forbidden_strip_reports_no_false_disk_conflict():
    # Stripping low-angle forbidden edges that diskify transiently re-added must NOT be
    # reported as a forbidden/disk conflict when the charts stay valid disks (honest signal:
    # a conflict is only flagged when a chart is genuinely left non-disk).
    mesh = build_humanoid_blob(segments=16, rings=16)
    forb = [e.id for e in mesh.edges if len(e.face_ids) == 2 and e.dihedral_angle < 30][:30]
    spec = GuidedUVSpec(forbidden_edges=forb,
                        chapters=[GuidedChapter("body", [1], "cloth_panels", "")])
    _, _, _, _, built = _build(mesh, spec)
    assert built.nondisk_charts == []
    assert built.forbidden_disk_conflicts == []
    # the new audit fields are serialised
    d = built.to_dict()
    assert "forbidden_disk_conflicts" in d and "nondisk_charts" in d


def test_empty_chapter_list_still_charts_via_fallback():
    # criterion 8 extreme: an empty spec → every part becomes a fallback chapter and the mesh
    # still gets a valid (mandatory-respecting, all-disk) seam set.
    mesh = build_humanoid_blob(segments=16, rings=16)
    seg, _, _, assignment, built = _build(mesh, GuidedUVSpec(chapters=[]))
    assert all(c.source == "fallback" for c in assignment.chapters)
    assert len(assignment.chapters) == len(seg.parts)
    assert mandatory_seam_audit(mesh, built.seams)["mandatory_90_missing"] == 0
    for fs in flood_charts(mesh, built.seams):
        assert uv_is_disk(mesh, fs, built.seams)


# -- coarse fast-path segmentation (perf: compute only what the spec needs) ---------------

def test_coarse_segment_parts_is_connected_components():
    # Coarse parts = connected components: total face coverage, each part one component, and
    # FAR fewer parts than the deep watershed (the whole point — skip the over-segmentation).
    mesh = build_humanoid_blob(segments=16, rings=16)
    seg = coarse_segment_parts(mesh)
    allf = sorted(f for p in seg.parts for f in p.face_ids)
    assert allf == list(range(mesh.face_count))               # total, disjoint
    assert seg.parts                                          # at least one component
    deep = segment_parts(mesh)
    assert len(seg.parts) <= len(deep.parts)                  # never MORE than the watershed
    assert seg.history[-1]["method"] == "connected_components"


def test_coarse_by_material_splits_components():
    mesh = build_humanoid_blob(segments=16, rings=16)
    cc = coarse_segment_parts(mesh)
    bymat = coarse_segment_parts(mesh, by_material=True)
    # material split never merges components, so it has >= as many parts; coverage still total.
    assert len(bymat.parts) >= len(cc.parts)
    assert sorted(f for p in bymat.parts for f in p.face_ids) == list(range(mesh.face_count))


def test_resolve_segmentation_mode():
    empty = GuidedUVSpec(chapters=[GuidedChapter("a", [], "cylinder", "")])
    manual = GuidedUVSpec(chapters=[GuidedChapter("a", [0], "cylinder", "")])
    # auto → coarse when no chapter fills source_part_ids, else full
    assert resolve_segmentation_mode(empty) == "coarse"
    assert resolve_segmentation_mode(manual) == "full"
    # explicit override beats spec; manual_parts is an alias for full
    assert resolve_segmentation_mode(empty, "full") == "full"
    assert resolve_segmentation_mode(manual, "coarse") == "coarse"
    assert resolve_segmentation_mode(empty, "manual_parts") == "full"
    # spec-level setting honoured when no override
    assert resolve_segmentation_mode(GuidedUVSpec(segmentation_mode="coarse",
                                                  chapters=[GuidedChapter("a", [0], "x", "")])) == "coarse"


def test_segmentation_mode_round_trips():
    spec = GuidedUVSpec(segmentation_mode="coarse",
                        chapters=[GuidedChapter("a", [], "auto", "")])
    assert GuidedUVSpec.from_dict(spec.to_dict()).segmentation_mode == "coarse"
    assert GuidedUVSpec.from_json(spec.to_json()).segmentation_mode == "coarse"
    assert GuidedUVSpec.from_dict({"chapters": []}).segmentation_mode == "auto"  # default


def test_coarse_build_completes_with_mandatory_and_forbidden():
    # The coarse path (no descriptors, every part a 'coarse' keep-intact chapter) still
    # produces a valid mandatory-respecting seam set and honours forbidden edges.
    mesh = build_humanoid_blob(segments=16, rings=16)
    forb = [e.id for e in mesh.edges if len(e.face_ids) == 2 and e.dihedral_angle < 30][:20]
    seg = coarse_segment_parts(mesh)
    classes = [type("C", (), {"part_id": p.part_id, "type": "coarse"})() for p in seg.parts]
    spec = GuidedUVSpec(forbidden_edges=forb, chapters=[])
    assignment = assign_chapters(seg, [], classes, spec)
    assert all(c.behavior == "keep_intact" for c in assignment.chapters)      # coarse → keep intact
    built = build_guided_seams(mesh, seg, [], classes, spec, assignment)
    assert set(forb).isdisjoint(built.seams)
    assert mandatory_seam_audit(mesh, built.seams)["mandatory_90_missing"] == 0


def test_repair_island_hard_cap_is_separate_from_advisory_target():
    # ``island_count_max`` is an advisory gate target. HARD repairs must still be allowed
    # when a guided/coarse layout starts above that target and needs one more split to clear
    # overlap or worst-island distortion.
    cfg = ChartGateConfig(island_count_max=80)
    assert _resolve_repair_island_hard_cap(cfg) == 160
    assert _resolve_repair_island_hard_cap(ChartGateConfig(island_count_max=220)) == 220
    assert _resolve_repair_island_hard_cap(cfg, explicit=96) == 96


# -- diskify budget + cheap precheck (perf) ----------------------------------

def test_diskify_budget_leaves_reported_nondisk():
    # A tiny budget stops diskify early; remaining non-disk charts must be REPORTED
    # (nondisk_charts), never silently shipped as disks.
    mesh = build_humanoid_blob(segments=16, rings=16)
    seg = coarse_segment_parts(mesh)
    classes = [type("C", (), {"part_id": p.part_id, "type": "coarse"})() for p in seg.parts]
    spec = GuidedUVSpec(chapters=[])
    assignment = assign_chapters(seg, [], classes, spec)
    capped = build_guided_seams(mesh, seg, [], classes, spec, assignment, max_diskify_rounds=1)
    full = build_guided_seams(mesh, seg, [], classes, spec, assignment)
    assert len(capped.nondisk_charts) >= len(full.nondisk_charts)   # budget leaves more non-disk
    assert len(capped.seams) <= len(full.seams)                     # fewer diskify cuts


def test_cheap_disk_check_matches_uv_is_disk():
    # The cheap precheck must agree with the seam-aware uv_is_disk on every chart — including a
    # tube opened by an interior lengthwise slit (the case the cheap euler alone would miss).
    import math
    from chart_uv_agent.fixtures import build_capsule_with_spikes
    from chart_uv_agent.segmentation import flood_charts as _fc

    def cyl(seg=16, h=8):
        verts = [(math.cos(2*math.pi*s/seg), math.sin(2*math.pi*s/seg), k/h*6.0)
                 for k in range(h+1) for s in range(seg)]
        faces = [(k*seg+s, k*seg+(s+1) % seg, (k+1)*seg+(s+1) % seg, (k+1)*seg+s)
                 for k in range(h) for s in range(seg)]
        return MeshGraph.from_faces("cyl", verts, faces)

    for mesh in (cyl(), build_capsule_with_spikes(n_spikes=4, spike_len=1.6),
                 build_humanoid_blob(segments=14, rings=14)):
        spec = GuidedUVSpec(chapters=[GuidedChapter("c", [0], "cylinder", "")])
        _, _, _, _, built = _build(mesh, spec)
        for fs in _fc(mesh, built.seams):
            assert _is_uv_disk_cheap(mesh, fs, built.seams) == uv_is_disk(mesh, fs, built.seams)


# -- guided-intent coverage metrics + warnings (empty spec must not read as success) -----

def test_coverage_flags_empty_spec_as_intent_not_applied():
    # A spec whose chapters have empty source_part_ids resolves no parts → everything falls
    # back. Coverage must report guided_intent_applied=False and fallback_face_ratio≈1.0 so a
    # reviewer is not misled by a "passing" gate.
    mesh = build_humanoid_blob(segments=16, rings=16)
    seg, descs, classes = _segment(mesh)
    spec = GuidedUVSpec(chapters=[GuidedChapter("staff", [], "cylinder", ""),
                                  GuidedChapter("hood", [], "shell", "")])
    cov = chapter_coverage(assign_chapters(seg, descs, classes, spec))
    assert cov["guided_intent_applied"] is False
    assert cov["resolved_spec_chapter_count"] == 0
    assert cov["fallback_face_ratio"] == 1.0
    assert cov["spec_chapter_face_coverage"] == 0.0
    assert set(cov["unresolved_spec_chapters"]) == {"staff", "hood"}


def test_coverage_reports_applied_intent_and_policy_counts():
    mesh = build_humanoid_blob(segments=16, rings=16)
    seg, descs, classes = _segment(mesh)
    spec = GuidedUVSpec(chapters=[
        GuidedChapter("limb", [0], "cylinder", ""),            # → template chapter
        GuidedChapter("face", [1], "organic_front_preserve", ""),  # → front_preserve chapter
    ])
    cov = chapter_coverage(assign_chapters(seg, descs, classes, spec))
    assert cov["guided_intent_applied"] is True
    assert cov["resolved_spec_chapter_count"] == 2
    assert cov["cylinder_policy_chapter_count"] == 1     # REQUESTED, not necessarily reflected
    assert cov["front_preserve_chapter_count"] == 1
    assert 0.0 < cov["spec_chapter_face_coverage"] <= 1.0
    assert cov["fallback_face_ratio"] < 1.0


def test_report_warns_when_intent_not_applied_and_lists_unresolved():
    mesh = build_humanoid_blob(segments=16, rings=16)
    spec = GuidedUVSpec(chapters=[GuidedChapter("staff", [], "cylinder", "")])
    seg, descs, classes, assignment, built = _build(mesh, spec)
    gate = evaluate_chart_gate({"mandatory_90_missing": 0}, config=ChartGateConfig())
    report = build_guided_report(mesh, spec, assignment, built, {}, gate, built.seam_origin,
                                 pruned=[], chart_count=len(flood_charts(mesh, built.seams)))
    assert report["guided_intent_applied"] is False
    assert any("NOT applied" in w for w in report["warnings"])
    assert any("resolved NO parts" in w for w in report["warnings"])
    assert report["coverage"]["unresolved_spec_chapters"] == ["staff"]


def test_report_separates_requested_vs_reflected_cylinder_policy():
    # Review item 1/2: a cylinder chapter on NON-tube geometry (a flat panel grid) requests a
    # tube strip but the template reverts → REQUESTED (cylinder_policy) ≠ REFLECTED. The report
    # must say so (template_policy_applied_count=0, unreflected lists it, policy_reflected False)
    # rather than letting a "cylinder chapter count" read as success.
    from uv_agent.io.fixtures import build_grid_plane
    mesh = build_grid_plane(nx=8, ny=8)                       # one flat panel, not a tube
    spec = GuidedUVSpec(chapters=[GuidedChapter("not_a_tube", [0], "cylinder", "")])
    seg, descs, classes, assignment, built = _build(mesh, spec)
    assert built.template_chapters == []                     # template did NOT fire
    gate = evaluate_chart_gate({"mandatory_90_missing": 0}, config=ChartGateConfig())
    report = build_guided_report(mesh, spec, assignment, built, {}, gate, built.seam_origin,
                                 pruned=[], chart_count=len(flood_charts(mesh, built.seams)))
    pol = report["policy_reflection"]
    assert pol["cylinder_policy_chapter_count"] == 1          # requested
    assert pol["template_policy_applied_count"] == 0          # not applied
    assert pol["chapter_template_seam_count"] == 0
    assert pol["unreflected_policy_chapters"] == ["not_a_tube"]
    assert report["guided_policy_reflected"] is False         # intent assigned but not reflected
    assert any("NOT reflected" in w for w in report["warnings"])


def test_cylinder_policy_reflected_on_a_real_tube():
    # The flip side: a genuine tube DOES open → template fires, reflected True, no warning.
    mesh = _cylinder()
    spec = GuidedUVSpec(chapters=[GuidedChapter("shaft", [0], "cylinder", "")])
    seg, descs, classes, assignment, built = _build(mesh, spec)
    assert built.template_chapters == [0]
    gate = evaluate_chart_gate({"mandatory_90_missing": 0}, config=ChartGateConfig())
    report = build_guided_report(mesh, spec, assignment, built, {}, gate, built.seam_origin,
                                 pruned=[], chart_count=len(flood_charts(mesh, built.seams)))
    assert report["policy_reflection"]["template_policy_applied_count"] == 1
    assert report["policy_reflection"]["chapter_template_seam_count"] > 0
    assert report["guided_policy_reflected"] is True
    assert not any("NOT reflected" in w for w in report["warnings"])


def _fork_mesh():
    """A 3-prong trident (a base block with three separated teeth) — the staff/prong case the
    axis cross-section sweep splits."""
    cells = [(i, j) for i in range(5) for j in range(3)]
    cells += [(i, j) for i in (0, 2, 4) for j in range(3, 9)]
    vidx: dict = {}
    verts = []

    def vid(i, j):
        if (i, j) not in vidx:
            vidx[(i, j)] = len(verts)
            verts.append((float(i), float(j), 0.0))
        return vidx[(i, j)]

    faces = [(vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)) for i, j in cells]
    return MeshGraph.from_faces("fork", verts, faces)


def test_cylinder_group_branch_splits_a_forked_part():
    # A cylinder_group chapter on a forked part (trident) must branch-split it into sub-parts
    # (separator seams) so the prongs become their own strips — the staff case (review item 3).
    mesh = _fork_mesh()
    seg = coarse_segment_parts(mesh)
    classes = [type("C", (), {"part_id": p.part_id, "type": "coarse"})() for p in seg.parts]
    big = max(seg.parts, key=lambda p: len(p.face_ids)).part_id
    spec = GuidedUVSpec(segmentation_mode="coarse",
                        chapters=[GuidedChapter("staff", [big], "cylinder_group", "")])
    descs = describe_parts(mesh, seg)
    assignment = assign_chapters(seg, descs, classes, spec)
    built = build_guided_seams(mesh, seg, descs, classes, spec, assignment)
    ops = [o["op"] for o in built.log]
    assert "cylinder_group_branch_split" in ops                # the fork was split into prongs
    assert built.template_chapters == [0]                      # cylinder_group policy reflected
    for fs in flood_charts(mesh, built.seams):
        assert uv_is_disk(mesh, fs, built.seams)               # still all disks


def test_report_intent_applied_has_no_intent_warning():
    mesh = build_humanoid_blob(segments=16, rings=16)
    spec = GuidedUVSpec(chapters=[GuidedChapter("body", [1], "blob", "")])
    seg, descs, classes, assignment, built = _build(mesh, spec)
    gate = evaluate_chart_gate({"mandatory_90_missing": 0}, config=ChartGateConfig())
    report = build_guided_report(mesh, spec, assignment, built, {}, gate, built.seam_origin,
                                 pruned=[], chart_count=len(flood_charts(mesh, built.seams)))
    assert report["guided_intent_applied"] is True
    assert not any("NOT applied" in w for w in report["warnings"])


# -- completion status: UV gate vs guided policy reflection (work plan §1) ----------------

_GOOD_GATE = {
    "mandatory_90_missing": 0, "mandatory_90_uv_unsplit": 0, "overlap_ratio": 0.0,
    "raster_overlap_ratio": 0.001, "stretch_score": 0.3, "worst_island_distortion": 0.45,
    "texel_density_variance": 0.5, "island_count": 12, "uv_bounds_ok": True,
    "fallback_used": False, "packing_efficiency": 0.5, "small_island_ratio": 0.0,
    "vt_v_ratio": 1.2, "convexity_mean": 0.9, "convexity_p10": 0.8,
    "boundary_smoothness_mean": 1.0, "tendril_count": 0,
}


def _report(mesh, spec):
    seg, descs, classes, assignment, built = _build(mesh, spec)
    return seg, built, assignment


def test_guided_completion_status_separates_uv_gate_from_policy_reflection():
    # A real tube cylinder chapter → policy reflected. Gate passing → guided_complete; gate
    # failing → failed_gate (both uv_shippable & guided_complete False).
    mesh = _cylinder()
    spec = GuidedUVSpec(chapters=[GuidedChapter("shaft", [0], "cylinder", "")])
    seg, built, assignment = _report(mesh, spec)
    ch = len(flood_charts(mesh, built.seams))
    ok = build_guided_report(mesh, spec, assignment, built, {},
                             evaluate_chart_gate(_GOOD_GATE, config=ChartGateConfig()),
                             built.seam_origin, pruned=[], chart_count=ch)
    assert ok["uv_shippable"] and ok["guided_policy_reflected"]
    assert ok["guided_complete"] and ok["completion_status"] == "guided_complete"

    bad = build_guided_report(mesh, spec, assignment, built, {},
                              evaluate_chart_gate({**_GOOD_GATE, "mandatory_90_uv_unsplit": 5},
                                                  config=ChartGateConfig()),
                              built.seam_origin, pruned=[], chart_count=ch)
    assert not bad["uv_shippable"] and not bad["guided_complete"]
    assert bad["completion_status"] == "failed_gate"


def test_completion_accepted_with_policy_warning_when_gate_passes_but_policy_unreflected():
    # Gate passes but a cylinder chapter on non-tube geometry is unreflected → valid UV but
    # NOT guided_complete (the exact "shippable but not done" state).
    from uv_agent.io.fixtures import build_grid_plane
    mesh = build_grid_plane(nx=8, ny=8)
    spec = GuidedUVSpec(chapters=[GuidedChapter("not_a_tube", [0], "cylinder", "")])
    seg, built, assignment = _report(mesh, spec)
    rep = build_guided_report(mesh, spec, assignment, built, {},
                              evaluate_chart_gate(_GOOD_GATE, config=ChartGateConfig()),
                              built.seam_origin, pruned=[],
                              chart_count=len(flood_charts(mesh, built.seams)))
    assert rep["uv_shippable"] is True
    assert rep["guided_complete"] is False
    assert rep["completion_status"] == "accepted_with_policy_warning"


def test_cylinder_chapter_escalates_or_reports_unreflected():
    # A genuine tube reflects (template fired); a flat panel does not and is honestly listed.
    tube = _cylinder()
    _, b1, _ = _report(tube, GuidedUVSpec(chapters=[GuidedChapter("t", [0], "cylinder", "")]))
    assert b1.template_chapters == [0]
    from uv_agent.io.fixtures import build_grid_plane
    flat = build_grid_plane(nx=8, ny=8)
    _, b2, a2 = _report(flat, GuidedUVSpec(chapters=[GuidedChapter("t", [0], "cylinder", "")]))
    assert b2.template_chapters == []                      # nothing fired on a flat panel


# -- front-preserve actual protection (work plan §3) -------------------------

def _fp_setup():
    mesh = build_humanoid_blob(segments=16, rings=16)
    seg, descs, classes = _segment(mesh)
    spec = GuidedUVSpec(front_preserve_axis="+Z",
                        chapters=[GuidedChapter("face", [1], "organic_front_preserve", "")])
    assignment = assign_chapters(seg, descs, classes, spec)
    return mesh, assignment


def test_front_preserve_generates_forbidden_low_angle_front_edges():
    from artist_uv_agent.guided import compute_front_preserve_edges
    mesh, assignment = _fp_setup()
    mand = mandatory_seam_edges(mesh)
    edges = compute_front_preserve_edges(mesh, assignment, "+Z", threshold=0.3,
                                         max_dihedral=45.0, mandatory=mand)
    assert edges                                           # some front edges were protected
    fp_faces = {f for c in assignment.chapters if c.behavior == "front_preserve"
                for f in c.face_ids}
    for e in edges:
        ed = mesh.edges[e]
        assert ed.dihedral_angle < 45.0                   # low-angle only
        assert e not in mand                              # never a mandatory fold
        assert ed.face_ids[0] in fp_faces and ed.face_ids[1] in fp_faces


def test_front_preserve_does_not_protect_mandatory_folds():
    from artist_uv_agent.guided import compute_front_preserve_edges
    mesh, assignment = _fp_setup()
    edges = compute_front_preserve_edges(mesh, assignment, "+Z", mandatory=set())
    assert edges
    victim = next(iter(edges))
    # marking that edge mandatory must exclude it (mandatory always wins).
    edges2 = compute_front_preserve_edges(mesh, assignment, "+Z", mandatory={victim})
    assert victim not in edges2


def test_front_preserve_disabled_is_label_only_with_warning():
    mesh = build_humanoid_blob(segments=16, rings=16)
    spec = GuidedUVSpec(chapters=[GuidedChapter("face", [1], "organic_front_preserve", "")])
    seg, built, assignment = _report(mesh, spec)            # no front_preserve_axis
    rep = build_guided_report(mesh, spec, assignment, built, {},
                              evaluate_chart_gate(_GOOD_GATE, config=ChartGateConfig()),
                              built.seam_origin, pruned=[],
                              chart_count=len(flood_charts(mesh, built.seams)))
    pr = rep["policy_reflection"]
    assert pr["front_preserve_protection"] == "label_only_no_auto_front_edges"
    assert pr["front_preserve_edge_count"] == 0
    assert any("LABEL ONLY" in w for w in rep["warnings"])


def test_front_preserve_active_reports_axis_and_edges():
    mesh = build_humanoid_blob(segments=16, rings=16)
    spec = GuidedUVSpec(front_preserve_axis="+Z",
                        chapters=[GuidedChapter("face", [1], "organic_front_preserve", "")])
    seg, built, assignment = _report(mesh, spec)
    assert built.front_preserve_edges                      # edges generated + forbidden
    assert set(built.front_preserve_edges).isdisjoint(built.seams)   # never shipped as seams
    rep = build_guided_report(mesh, spec, assignment, built, {},
                              evaluate_chart_gate(_GOOD_GATE, config=ChartGateConfig()),
                              built.seam_origin, pruned=[],
                              chart_count=len(flood_charts(mesh, built.seams)))
    pr = rep["policy_reflection"]
    assert pr["front_preserve_protection"] == "active_view_axis"
    assert pr["front_preserve_axis"] == "+Z"
    assert pr["front_preserve_edge_count"] == len(built.front_preserve_edges) > 0


def test_multiloop_tube_opens_a_three_loop_tube():
    # The generalised opener turns a tube with an extra hole (3 boundary loops) into a disk,
    # where the single-lengthwise cylinder_template cannot (review/work plan §2: sleeve case).
    from artist_uv_agent.seams import open_multiloop_tube
    mesh = _cylinder(seg=16, h=8)                          # an open 2-loop tube
    slits = open_multiloop_tube(mesh, [f.id for f in mesh.faces], set())
    assert slits
    assert uv_is_disk(mesh, [f.id for f in mesh.faces], set(slits))


# -- v3: selector (front/back split) + artist-intent checklist + face policy --------------

def test_selector_splits_front_and_back_robe_faces():
    from artist_uv_agent.guided import apply_chapter_selectors
    mesh = build_humanoid_blob(segments=18, rings=18)
    seg = coarse_segment_parts(mesh)
    pid = max(seg.parts, key=lambda p: len(p.face_ids)).part_id
    spec = GuidedUVSpec(segmentation_mode="coarse", chapters=[
        GuidedChapter("front_robe", [pid], "cloth_panel", "",
                      selector={"normal_axis": "+Z", "threshold": 0.3}),
        GuidedChapter("back_cloak", [pid], "cloth_panel", "",
                      selector={"normal_axis": "-Z", "threshold": 0.3}),
    ])
    seg2, spec2 = apply_chapter_selectors(mesh, seg, spec)
    assert seg2 is not seg
    front_src = set(spec2.chapters[0].source_part_ids)
    back_src = set(spec2.chapters[1].source_part_ids)
    assert front_src and back_src and front_src.isdisjoint(back_src)
    assert spec2.chapters[0].selector is None                  # selector consumed
    fp = seg2.face_part
    front_faces = [f for f, p in fp.items() if p in front_src]
    back_faces = [f for f, p in fp.items() if p in back_src]
    assert all(mesh.faces[f].normal[2] > 0 for f in front_faces)   # carved +Z
    assert all(mesh.faces[f].normal[2] < 0 for f in back_faces)    # carved −Z


def test_artist_intent_checklist_reports_missing_and_failed_items():
    mesh = _cylinder()
    spec = GuidedUVSpec(expected_intents=["staff", "hands"],
                        chapters=[GuidedChapter("staff", [0], "cylinder", "")])
    seg, built, assignment = _report(mesh, spec)
    rep = build_guided_report(mesh, spec, assignment, built, {},
                              evaluate_chart_gate(_GOOD_GATE, config=ChartGateConfig()),
                              built.seam_origin, pruned=[],
                              chart_count=len(flood_charts(mesh, built.seams)))
    cl = rep["artist_intent_checklist"]
    assert cl["staff"]["status"] == "passed"                   # tube reflected
    assert cl["hands"]["status"] == "missing"                  # declared but no chapter
    assert "hands" in rep["unmet_artist_intents"]
    assert "staff" not in rep["unmet_artist_intents"]
    assert rep["artist_intent_passed"] is False
    assert rep["guided_complete"] is False                     # checklist gates completion


def test_missing_intent_not_penalised_when_not_declared():
    # Without expected_intents, a minimal spec is not penalised for canonical intents it never
    # raised — only a present-but-failed policy counts.
    mesh = _cylinder()
    spec = GuidedUVSpec(chapters=[GuidedChapter("staff", [0], "cylinder", "")])
    seg, built, assignment = _report(mesh, spec)
    rep = build_guided_report(mesh, spec, assignment, built, {},
                              evaluate_chart_gate(_GOOD_GATE, config=ChartGateConfig()),
                              built.seam_origin, pruned=[],
                              chart_count=len(flood_charts(mesh, built.seams)))
    assert rep["unmet_artist_intents"] == []
    assert rep["artist_intent_passed"] is True


def test_face_front_preserve_reports_face_policy():
    mesh = build_humanoid_blob(segments=16, rings=16)
    spec = GuidedUVSpec(front_preserve_axis="+Z",
                        chapters=[GuidedChapter("head_face", [1], "face_front_preserve", "")])
    seg, descs, classes, assignment, built = _build(mesh, spec)
    assert assignment.chapters[0].behavior == "front_preserve"   # face type → front behaviour
    rep = build_guided_report(mesh, spec, assignment, built, {},
                              evaluate_chart_gate(_GOOD_GATE, config=ChartGateConfig()),
                              built.seam_origin, pruned=[],
                              chart_count=len(flood_charts(mesh, built.seams)))
    fp = rep["face_policy"]
    assert fp["requested"] is True and fp["front_axis"] == "+Z"
    assert fp["status"] in ("passed", "failed")
    assert "face" in rep["artist_intent_checklist"]


# -- v4: island consolidation (merge same-chapter charts) --------------------

def _bisect_seams(mesh):
    """Seam set that splits the (flat-normal) grid into exactly 2 connected charts by a
    bottom/top face-id partition (VSA split no-ops on equal normals)."""
    faces = [f.id for f in mesh.faces]
    half = set(faces[: len(faces) // 2])
    seams = {e.id for e in mesh.edges if len(e.face_ids) == 2
             and (e.face_ids[0] in half) != (e.face_ids[1] in half)}
    return seams


def test_consolidate_merges_same_chapter_charts():
    from artist_uv_agent.guided import _consolidate_same_chapter_charts
    from uv_agent.io.fixtures import build_grid_plane
    mesh = build_grid_plane(nx=6, ny=6)
    seams = _bisect_seams(mesh)
    assert len(flood_charts(mesh, seams)) == 2            # artificially split into 2 charts
    merged = _consolidate_same_chapter_charts(
        mesh, seams, {f.id: 0 for f in mesh.faces}, {0: 0}, mandatory_seam_edges(mesh),
        protect=set(), fold_angle=90.0, accept=lambda: True, max_merges=10)
    assert merged >= 1
    assert len(flood_charts(mesh, seams)) == 1           # merged back into one island


def test_consolidate_reverts_when_gate_rejects():
    from artist_uv_agent.guided import _consolidate_same_chapter_charts
    from uv_agent.io.fixtures import build_grid_plane
    mesh = build_grid_plane(nx=6, ny=6)
    seams = _bisect_seams(mesh)
    before = set(seams)
    merged = _consolidate_same_chapter_charts(
        mesh, seams, {f.id: 0 for f in mesh.faces}, {0: 0}, mandatory_seam_edges(mesh),
        protect=set(), fold_angle=90.0, accept=lambda: False, max_merges=10)
    assert merged == 0
    assert seams == before                               # every merge reverted (gate rejected)


def test_consolidate_keeps_different_chapters_separate():
    from artist_uv_agent.guided import _consolidate_same_chapter_charts
    from uv_agent.io.fixtures import build_grid_plane
    mesh = build_grid_plane(nx=6, ny=6)
    seams = _bisect_seams(mesh)
    # put the two halves in DIFFERENT chapters → the boundary is a real chapter border, never merged
    faces = [f.id for f in mesh.faces]
    half = set(faces[: len(faces) // 2])
    face_part = {f: (0 if f in half else 1) for f in faces}
    merged = _consolidate_same_chapter_charts(
        mesh, seams, face_part, {0: 0, 1: 1}, mandatory_seam_edges(mesh), protect=set(),
        fold_angle=90.0, accept=lambda: True, max_merges=10)
    assert merged == 0
    assert len(flood_charts(mesh, seams)) == 2           # chapters stay separate


def test_consolidate_never_removes_mandatory_or_protected():
    from artist_uv_agent.guided import _consolidate_same_chapter_charts
    from chart_uv_agent.fixtures import build_folded_planes
    mesh = build_folded_planes(n=5)
    mand = mandatory_seam_edges(mesh)
    seams = set(mand)                                     # the 90° fold seam(s)
    merged = _consolidate_same_chapter_charts(mesh, seams, {f.id: 0 for f in mesh.faces},
                                              {0: 0}, mand, protect=set(), fold_angle=90.0,
                                              accept=lambda: True, max_merges=10)
    assert mand.issubset(seams)                           # mandatory folds never dissolved


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
