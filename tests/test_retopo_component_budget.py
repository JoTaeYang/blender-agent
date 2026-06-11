"""Component budget policy -- Phase DM3 (decimation plan §6).

These verify, offline, the per-component budget planner the plan's §6 completion
criterion calls for: per-component measurement, importance-weighted budget
distribution under the three policies, tiny-component handling (minimal shell vs
removal), and the achievable lower bound with vs without tiny-component removal.
The Blender adapter only wraps this on an extracted mesh, so the logic is fully
covered here.
"""

from retopo_agent.geometry.component_budget import (
    ACTION_DECIMATE,
    ACTION_MIN_SHELL,
    ACTION_REMOVE,
    DEFAULT_MIN_SHELL_FACES,
    analyze_components,
    normalize_policy,
    plan_component_budget,
)
from retopo_agent.geometry.diagnosis import (
    POLICY_COMPONENT_BUDGET,
    POLICY_LARGEST_ONLY,
    POLICY_PRESERVE_ALL,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_cube, build_grid_plane


def _combine(*meshes) -> MeshGraph:
    verts: list[tuple[float, float, float]] = []
    faces: list[list[int]] = []
    for m in meshes:
        off = len(verts)
        verts.extend(v.co for v in m.vertices)
        faces.extend([vid + off for vid in f.vertex_ids] for f in m.faces)
    return MeshGraph.from_faces("combined", verts, faces)


def _tiny_triangle(x: float) -> MeshGraph:
    verts = [(x, 0.0, 0.0), (x + 0.1, 0.0, 0.0), (x, 0.1, 0.0)]
    return MeshGraph.from_faces("tri", verts, [[0, 1, 2]])


def _multi_component(n_tiny: int = 5):
    """One dominant grid (100 quads) + ``n_tiny`` one-face shells -- the anchor's
    component structure in miniature."""
    big = build_grid_plane(10, 10)  # 100 quads
    tinies = [_tiny_triangle(5.0 + 2.0 * i) for i in range(n_tiny)]
    return _combine(big, *tinies)


# -- policy normalization --------------------------------------------------


def test_normalize_policy_accepts_cli_and_canonical():
    assert normalize_policy("budget") == POLICY_COMPONENT_BUDGET  # CLI spelling
    assert normalize_policy("component_budget") == POLICY_COMPONENT_BUDGET
    assert normalize_policy("largest_only") == POLICY_LARGEST_ONLY
    assert normalize_policy("preserve_all") == POLICY_PRESERVE_ALL
    assert normalize_policy(None) == POLICY_PRESERVE_ALL
    assert normalize_policy("nonsense") == POLICY_PRESERVE_ALL


# -- component measurement -------------------------------------------------


def test_analyze_components_measures_and_ranks():
    comps = analyze_components(_multi_component(5))
    assert len(comps) == 6
    # Sorted by descending face count: component 0 is the dominant grid.
    assert comps[0].id == 0 and comps[0].face_count == 100
    assert comps[0].is_tiny is False
    assert all(c.face_count == 1 and c.is_tiny for c in comps[1:])
    assert comps[0].surface_area > 0 and comps[0].bbox_diagonal > 0


def test_analyze_single_component_cube():
    comps = analyze_components(build_cube())
    assert len(comps) == 1
    assert comps[0].face_count == 6
    assert comps[0].material_indices == [0]


# -- preserve_all ----------------------------------------------------------


def test_preserve_all_keeps_every_component():
    rep = plan_component_budget(_multi_component(5), 60, policy=POLICY_PRESERVE_ALL)
    assert rep.policy == POLICY_PRESERVE_ALL
    assert rep.removed_component_count == 0
    assert all(p.action == ACTION_DECIMATE for p in rep.components)
    # The dominant shell takes the lion's share of the budget by importance.
    biggest = next(p for p in rep.components if p.component.id == 0)
    assert biggest.allocated_budget > sum(p.allocated_budget for p in rep.components if p.component.id != 0)


# -- component_budget ------------------------------------------------------


def test_component_budget_shells_tiny_without_removal():
    rep = plan_component_budget(_multi_component(5), 60, policy=POLICY_COMPONENT_BUDGET)
    tiny_plans = [p for p in rep.components if p.component.id != 0]
    assert all(p.action == ACTION_MIN_SHELL for p in tiny_plans)
    # A one-face shell can't grow, so its minimal shell is its single face.
    assert all(p.allocated_budget == 1 for p in tiny_plans)
    assert rep.removed_component_count == 0
    big = next(p for p in rep.components if p.component.id == 0)
    assert big.action == ACTION_DECIMATE


def test_component_budget_removes_tiny_when_allowed():
    rep = plan_component_budget(_multi_component(5), 60, policy=POLICY_COMPONENT_BUDGET, allow_removal=True)
    tiny_plans = [p for p in rep.components if p.component.id != 0]
    assert all(p.action == ACTION_REMOVE for p in tiny_plans)
    assert all(p.allocated_budget == 0 for p in tiny_plans)
    assert rep.removed_component_count == 5
    assert rep.removed_face_count == 5


# -- largest_only ----------------------------------------------------------


def test_largest_only_keeps_just_the_dominant_shell():
    rep = plan_component_budget(_multi_component(5), 60, policy=POLICY_LARGEST_ONLY, allow_removal=True)
    big = next(p for p in rep.components if p.component.id == 0)
    assert big.action == ACTION_DECIMATE
    assert all(p.action == ACTION_REMOVE for p in rep.components if p.component.id != 0)
    assert rep.removed_component_count == 5


def test_largest_only_shells_others_without_removal():
    rep = plan_component_budget(_multi_component(5), 60, policy=POLICY_LARGEST_ONLY)
    assert all(
        p.action == ACTION_MIN_SHELL for p in rep.components if p.component.id != 0
    )
    assert rep.removed_component_count == 0


# -- lower-bound comparison (the §6 completion criterion) ------------------


def test_lower_bound_with_vs_without_removal():
    rep = plan_component_budget(_multi_component(20), 50, policy=POLICY_COMPONENT_BUDGET, allow_removal=True)
    # Without removal every shell costs at least one face: 4 (grid min shell) + 20 tiny.
    assert rep.lower_bound_without_removal == DEFAULT_MIN_SHELL_FACES + 20
    # With removal the 20 tiny shells drop out, leaving only the dominant shell.
    assert rep.lower_bound_with_removal == DEFAULT_MIN_SHELL_FACES
    assert rep.lower_bound_with_removal < rep.lower_bound_without_removal


def test_reachability_flags_track_target_vs_bound():
    mesh = _multi_component(20)
    # target 5 is below the no-removal floor (24) but above the with-removal floor (4).
    rep = plan_component_budget(mesh, 5, policy=POLICY_COMPONENT_BUDGET, allow_removal=True)
    assert rep.reachable_without_removal is False
    assert rep.reachable_with_removal is True


# -- budget never empties the mesh / clamping ------------------------------


def test_active_budget_clamped_to_component_face_count():
    # Target far above total faces: each active component caps at its own face count.
    rep = plan_component_budget(build_cube(), 1000, policy=POLICY_PRESERVE_ALL)
    assert rep.components[0].allocated_budget == 6  # cannot exceed the 6 cube faces


def test_all_tiny_still_keeps_one_active():
    # Many equal tiny shells, none dominant: the largest is still kept active.
    mesh = _combine(*[_tiny_triangle(3.0 * i) for i in range(4)])
    rep = plan_component_budget(mesh, 2, policy=POLICY_COMPONENT_BUDGET, allow_removal=True)
    assert sum(1 for p in rep.components if p.action == ACTION_DECIMATE) >= 1


# -- output contract / robustness -----------------------------------------


def test_to_dict_contract():
    rep = plan_component_budget(_multi_component(3), 40, policy=POLICY_COMPONENT_BUDGET).to_dict()
    for key in (
        "policy",
        "target_face_count",
        "allow_removal",
        "component_count",
        "tiny_component_count",
        "allocated_total",
        "removed_component_count",
        "removed_face_count",
        "lower_bound_without_removal",
        "lower_bound_with_removal",
        "reachable_without_removal",
        "reachable_with_removal",
        "components",
    ):
        assert key in rep
    entry = rep["components"][0]
    for key in ("id", "face_count", "surface_area", "bbox_diagonal", "allocated_budget", "action", "is_tiny"):
        assert key in entry


def test_empty_mesh_is_safe():
    rep = plan_component_budget(MeshGraph.from_faces("empty", [], []), 100, policy=POLICY_COMPONENT_BUDGET)
    assert rep.component_count == 0
    assert rep.allocated_total == 0
    assert rep.removed_component_count == 0
