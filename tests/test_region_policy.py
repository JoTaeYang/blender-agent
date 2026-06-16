"""Important Region Policy — pure-helper tests (IMPORTANT_REGION_UV_POLICY_PLAN §10.1).

Blender-free. Covers the new ``artist_uv_agent.region_policy`` layer: face_front heuristic
detection, the protected/interior edge sets, the region-spec loader (explicit-wins-over-
heuristic, optional/disabled), the seam-policy precedence (mandatory 90° > region > split),
the post-split reject decision, and the ``regions`` report block. The real Blender unwrap +
post-split reject loop is exercised by the worker regression run; here we test the decisions.
"""

import numpy as np

from artist_uv_agent.region_policy import (
    ImportantRegion, RegionPolicy, RegionPolicyConfig, build_region_report,
    classify_face_regions, detect_face_front, load_region_policy, region_boundary_audit,
    region_edge_cost_multiplier, region_interior_edges, region_protected_edges,
    region_protected_merge,
)
from artist_uv_agent.seam_policy import decide_edge, decide_edge_seams
from chart_uv_agent.fixtures import build_folded_planes, build_humanoid_blob
from chart_uv_agent.segmentation import edge_cut_cost, flood_charts
from uv_agent.io.fixtures import build_cylinder


# -- face_front heuristic (§5.2) ---------------------------------------------

def test_detect_face_front_produces_faces_and_protected_edges():
    mesh = build_humanoid_blob()
    cfg = RegionPolicyConfig(front_axis="+X", up_axis="+Z")
    region = detect_face_front(mesh, cfg)
    assert region is not None
    assert region.kind == "face_front"
    assert region.detection == "heuristic"
    assert region.face_ids                       # some front-facing upper faces selected
    # The protected set is the region's SMOOTH interior edges only (mandatory excluded).
    assert region.protected_edges <= region_interior_edges(mesh, region.face_ids)
    assert all(mesh.edges[e].dihedral_angle < 90.0 for e in region.protected_edges)
    # Front faces really do face +X (the heuristic isn't selecting the back).
    assert all(np.asarray(mesh.faces[f].normal)[0] > 0 for f in region.face_ids)


def test_detect_face_front_returns_none_without_axis():
    """Axis is never guessed (§11.7): no front/up axis → no heuristic region."""
    mesh = build_humanoid_blob()
    assert detect_face_front(mesh, RegionPolicyConfig()) is None
    assert detect_face_front(mesh, RegionPolicyConfig(front_axis="+X")) is None


# -- edge sets ----------------------------------------------------------------

def test_region_protected_edges_excludes_mandatory_fold():
    """A ≥90° fold inside a region is NEVER protected (mandatory must win, §5.3)."""
    mesh = build_folded_planes(n=6)
    all_faces = {f.id for f in mesh.faces}
    protected = region_protected_edges(mesh, all_faces)
    interior = region_interior_edges(mesh, all_faces)
    fold = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)
    assert fold in interior
    assert fold not in protected                 # excluded — it's a mandatory fold
    assert all(mesh.edges[e].dihedral_angle < 90.0 for e in protected)


# -- seam-policy precedence (§5.3) -------------------------------------------

def test_mandatory_fold_in_protected_region_stays_mandatory():
    """Feeding region protected edges as seam-policy ``forbidden`` must NOT demote a fold."""
    mesh = build_folded_planes(n=6)
    fold = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)
    d = decide_edge(mesh, fold, forbidden_edges={fold})
    assert d.decision == "mandatory"
    assert any(r.startswith("conflict:") for r in d.reasons)


def test_smooth_protected_edge_is_forbidden():
    mesh = build_folded_planes(n=6)
    flat = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle < 10.0)
    assert decide_edge(mesh, flat, forbidden_edges={flat}).decision == "forbidden"


def test_seam_policy_constraints_round_trips_through_decide_edge_seams():
    mesh = build_folded_planes(n=6)
    all_faces = {f.id for f in mesh.faces}
    region = ImportantRegion("face", "face_front", face_ids=all_faces,
                             protected_edges=region_protected_edges(mesh, all_faces))
    policy = RegionPolicy([region])
    forbidden, preferred = policy.seam_policy_constraints()
    decisions = decide_edge_seams(mesh, forbidden_edges=forbidden, preferred_edges=preferred)
    by_id = {d.edge_id: d for d in decisions}
    # Every protected smooth edge is forbidden; the mandatory fold is still mandatory.
    for e in forbidden:
        assert by_id[e].decision == "forbidden"
    fold = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)
    assert by_id[fold].decision == "mandatory"


# -- post-split reject decision (§5.4) ---------------------------------------

def test_protected_cut_flags_smooth_split_edges():
    mesh = build_folded_planes(n=6)
    all_faces = {f.id for f in mesh.faces}
    protected = region_protected_edges(mesh, all_faces)
    policy = RegionPolicy([ImportantRegion("face", "face_front", face_ids=all_faces,
                                           protected_edges=protected)])
    some = sorted(protected)[:3]
    fold = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)
    cut = policy.protected_cut(some + [fold])
    assert cut == set(some)                       # fold is not protected, smooth edges are
    assert policy.region_names_for(cut) == ["face"]


# -- region-spec loader (§5.6) -----------------------------------------------

def test_load_region_policy_none_or_disabled_is_baseline():
    mesh = build_humanoid_blob()
    assert load_region_policy(None, mesh) is None
    assert load_region_policy({"enabled": False, "regions": []}, mesh) is None
    assert load_region_policy({"enabled": True, "regions": []}, mesh) is None


def test_load_region_policy_explicit_faces_win():
    mesh = build_folded_planes(n=6)
    faces = sorted({f.id for f in mesh.faces})[:10]
    spec = {"version": 1, "enabled": True, "front_axis": "-Y", "up_axis": "+Z",
            "regions": [{"name": "face_front", "kind": "face_front", "face_ids": faces}]}
    policy = load_region_policy(spec, mesh)
    assert policy is not None
    region = policy.regions[0]
    assert region.detection == "explicit"
    assert region.face_ids == set(faces)
    # protected_edges auto-derived from the explicit faces (smooth interior only).
    assert region.protected_edges == region_protected_edges(mesh, faces)


def test_load_region_policy_heuristic_face_front():
    mesh = build_humanoid_blob()
    spec = {"enabled": True, "front_axis": "+X", "up_axis": "+Z",
            "regions": [{"name": "face_front", "kind": "face_front"}]}
    policy = load_region_policy(spec, mesh)
    assert policy is not None
    assert policy.regions[0].detection == "heuristic"
    assert policy.regions[0].face_ids


# -- region report (§5.5) -----------------------------------------------------

def test_region_report_separates_mandatory_and_smooth_seams():
    mesh = build_folded_planes(n=6)
    all_faces = {f.id for f in mesh.faces}
    protected = region_protected_edges(mesh, all_faces)
    region = ImportantRegion("face_front", "face_front", face_ids=all_faces,
                             protected_edges=protected, detection="heuristic", confidence="low")
    policy = RegionPolicy([region])
    fold = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)
    smooth = sorted(protected)[0]
    # A shipped seam set with the mandatory fold + one smooth seam inside the region.
    final_seams = {fold, smooth}
    history = [{"action": "reject", "reason": "protected_region_reject", "region": "face_front",
                "protected_edges_cut": [smooth, sorted(protected)[1]], "split_island": 4}]
    rep = build_region_report(policy, mesh, final_seams, history)
    assert len(rep) == 1
    r = rep[0]
    assert r["mandatory_seams_in_region"] == 1     # the fold
    assert r["smooth_seams_in_region"] == 1        # the one smooth seam
    assert r["rejected_splits"] == 1
    assert smooth in r["rejected_protected_edges"]
    assert r["confidence"] == "low"
    assert r["status"] == "protected_with_mandatory_conflicts"


def test_region_report_fully_protected_when_no_interior_seams():
    mesh = build_folded_planes(n=6)
    all_faces = {f.id for f in mesh.faces}
    policy = RegionPolicy([ImportantRegion("face", "face_front", face_ids=all_faces,
                          protected_edges=region_protected_edges(mesh, all_faces))])
    rep = build_region_report(policy, mesh, final_seams=set(), history=[])
    assert rep[0]["smooth_seams_in_region"] == 0
    assert rep[0]["mandatory_seams_in_region"] == 0
    assert rep[0]["rejected_splits"] == 0
    assert rep[0]["status"] == "fully_protected"


# == v2 face recovery (REGION_AWARE_FACE_UV_RECOVERY_PLAN) ====================

def _shared_edge(mesh, a, b):
    return next(e.id for e in mesh.edges if len(e.face_ids) == 2 and set(e.face_ids) == {a, b})


# -- 3-zone classification (§5.1) --------------------------------------------

# The synthetic blob is rounded (its front bulges laterally), so the centre-radius / facing
# filters — tuned for an elongated statue whose face sits near the vertical axis — need
# loosening to surface a core on the fixture. The real statue produces a core at the defaults.
_BLOB_CFG = dict(front_axis="+X", up_axis="+Z", core_facing_min=0.2, face_center_radius_frac=0.6)


def test_classify_face_regions_three_disjoint_zones():
    mesh = build_humanoid_blob()
    cfg = RegionPolicyConfig(**_BLOB_CFG)
    zones = classify_face_regions(mesh, cfg)
    kinds = {z.kind for z in zones}
    assert "face_front_core" in kinds            # the head front is detected
    # Zones partition into DISJOINT face sets — never one giant protected island (§5.4).
    seen = set()
    for z in zones:
        assert not (z.face_ids & seen)
        seen |= z.face_ids
    core = next(z for z in zones if z.kind == "face_front_core")
    assert core.smooth_seam_cost > 1.0           # core discourages cuts
    back = next((z for z in zones if z.kind == "head_back_neck_preferred"), None)
    if back is not None:
        assert back.smooth_seam_cost < 1.0       # back/neck invites cuts


def test_classify_returns_empty_without_axis():
    mesh = build_humanoid_blob()
    assert classify_face_regions(mesh, RegionPolicyConfig()) == []


# -- region edge cost (§6.2, §9.1) -------------------------------------------

def test_region_edge_cost_core_up_back_down_mandatory_unchanged():
    mesh = build_folded_planes(n=6)
    gridA = set(range(36))           # build_folded_planes: grid A faces first, then grid B
    gridB = set(range(36, 72))
    core = ImportantRegion("core", "face_front_core", face_ids=gridA,
                           protected_edges=region_protected_edges(mesh, gridA), smooth_seam_cost=50.0)
    back = ImportantRegion("back", "head_back_neck_preferred", face_ids=gridB,
                           preferred_edges=region_interior_edges(mesh, gridB), smooth_seam_cost=0.25)
    policy = RegionPolicy([core, back])

    smooth_core = next(iter(core.protected_edges))
    smooth_back = next(e for e in region_interior_edges(mesh, gridB)
                       if mesh.edges[e].dihedral_angle < 90.0)
    fold = next(e.id for e in mesh.edges
                if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0)

    base_core = edge_cut_cost(mesh, smooth_core)
    base_back = edge_cut_cost(mesh, smooth_back)
    # Core smooth edge gets MORE expensive; back/neck edge gets cheaper.
    assert edge_cut_cost(mesh, smooth_core, region_policy=policy) > base_core
    assert edge_cut_cost(mesh, smooth_back, region_policy=policy) < base_back
    # A ≥90° mandatory edge is never multiplied — region cost can't beat mandatory (§6.2).
    assert edge_cut_cost(mesh, fold, region_policy=policy) == edge_cut_cost(mesh, fold)
    assert region_edge_cost_multiplier(fold, [core, back], mesh) == 1.0
    assert region_edge_cost_multiplier(smooth_core, [core, back], mesh) == 50.0


# -- post-segmentation protected merge (§6.3, §9.1) --------------------------

def test_protected_merge_removes_low_angle_face_seam():
    """A flat grid split by two smooth interior seams re-merges (union is a flat disk)."""
    mesh = build_folded_planes(n=4)
    gridA = list(range(16))          # grid A is flat (z=0): merging its halves is safe
    # Cut grid A into two charts along a smooth interior line.
    seam_edges = {_shared_edge(mesh, 0, 4), _shared_edge(mesh, 1, 5),
                  _shared_edge(mesh, 2, 6), _shared_edge(mesh, 3, 7)}
    # (folded_planes grid A is a 4x4 quad block: faces 0..15, row-major.)
    seams = set(seam_edges)
    core = ImportantRegion("core", "face_front_core", face_ids=set(gridA),
                           protected_edges=region_protected_edges(mesh, gridA), smooth_seam_cost=50.0)
    policy = RegionPolicy([core], config=RegionPolicyConfig(core_merge_cone_limit=68.0))
    before = len(flood_charts(mesh, seams))
    res = region_protected_merge(mesh, seams, policy)
    after = len(flood_charts(mesh, seams))
    if before > 1:                   # only assert the merge if the cut actually split A
        assert res["merges"] >= 1
        assert after < before
        assert all(e not in seams for e in res["removed"])


def test_protected_merge_never_removes_mandatory_fold():
    mesh = build_folded_planes(n=6)
    all_faces = {f.id for f in mesh.faces}
    seams = {e.id for e in mesh.edges
             if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0}   # the fold = chart boundary
    core = ImportantRegion("core", "face_front_core", face_ids=all_faces,
                           protected_edges=region_protected_edges(mesh, all_faces))
    policy = RegionPolicy([core])
    res = region_protected_merge(mesh, seams, policy)
    assert res["merges"] == 0                    # the only boundary is mandatory → never merged
    assert all(mesh.edges[e].dihedral_angle >= 90.0 for e in seams)


def test_protected_merge_rejects_non_disk_union():
    """Two arcs of a tube share two smooth boundaries; merging would make an annulus → refused."""
    mesh = build_cylinder(segments=8, rings=1)   # 8 faces in a loop, dihedral 45° (smooth)
    seams = {_shared_edge(mesh, 0, 1), _shared_edge(mesh, 4, 5)}   # split into two 4-face arcs
    assert len(flood_charts(mesh, seams)) == 2
    all_faces = {f.id for f in mesh.faces}
    core = ImportantRegion("core", "face_front_core", face_ids=all_faces,
                           protected_edges=region_protected_edges(mesh, all_faces),
                           smooth_seam_cost=50.0)
    policy = RegionPolicy([core], config=RegionPolicyConfig(core_merge_cone_limit=180.0))
    res = region_protected_merge(mesh, seams, policy)
    assert res["merges"] == 0                    # union is the full tube (non-disk) → rejected
    assert len(flood_charts(mesh, seams)) == 2


# -- spec loader v2 + mode (§7, §2.1) ----------------------------------------

def test_load_v2_face_recovery_spec_three_zones_default_mode():
    mesh = build_humanoid_blob()
    spec = {"version": 2, "enabled": True, "mode": "face_recovery",
            "front_axis": "+X", "up_axis": "+Z", "core_facing_min": 0.2, "face_center_radius_frac": 0.6,
            "regions": [{"name": "face_front_core", "kind": "face_front_core", "smooth_seam_cost": 50.0},
                        {"name": "face_side_transition", "kind": "face_side_transition", "smooth_seam_cost": 5.0},
                        {"name": "head_back_neck_preferred", "kind": "head_back_neck_preferred", "smooth_seam_cost": 0.25}]}
    policy = load_region_policy(spec, mesh)
    assert policy is not None
    assert policy.mode == "face_recovery"
    kinds = {r.kind for r in policy.regions}
    assert "face_front_core" in kinds
    assert policy.core_protected_edges                # core has protected smooth edges
    # face_recovery is the default mode when "mode" is omitted (post-split reject is opt-in §2.1).
    spec.pop("mode")
    assert load_region_policy(spec, mesh).mode == "face_recovery"


def test_region_boundary_audit_reports_core_smooth_count():
    mesh = build_humanoid_blob()
    spec = {"enabled": True, "mode": "face_recovery", "front_axis": "+X", "up_axis": "+Z",
            "core_facing_min": 0.2, "face_center_radius_frac": 0.6,
            "regions": [{"name": "face_front_core", "kind": "face_front_core"}]}
    policy = load_region_policy(spec, mesh)
    core = next(r for r in policy.regions if r.kind == "face_front_core")
    seams = set(list(core.protected_edges)[:3])       # pretend 3 core smooth edges shipped
    audit = region_boundary_audit(mesh, seams, policy)
    assert audit["face_front_core_smooth_seams"] == len(seams & core.protected_edges)
