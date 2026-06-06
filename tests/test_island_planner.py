from uv_agent.io import fixtures
from uv_agent.planner import operations
from uv_agent.planner.island_planner import plan_islands


def test_cube_splits_into_six_islands():
    plan = plan_islands(fixtures.build_cube())
    assert len(plan.islands) == 6
    assert all(len(i.face_ids) == 1 for i in plan.islands)


def test_flat_plane_is_single_island():
    plan = plan_islands(fixtures.build_grid_plane(4, 4))
    assert len(plan.islands) == 1
    assert len(plan.islands[0].face_ids) == 16


def test_material_boundary_splits_plane():
    m = fixtures.build_two_material_plane(4, 4)
    assert len(plan_islands(m, split_by_material=True).islands) == 2
    assert len(plan_islands(m, split_by_material=False).islands) == 1


def test_high_threshold_keeps_cylinder_whole():
    plan = plan_islands(fixtures.build_cylinder(16, 4), angle_threshold=45)
    assert len(plan.islands) == 1


def test_split_island_action():
    m = fixtures.build_grid_plane(4, 4)
    plan = plan_islands(m)
    iid = plan.islands[0].island_id
    new_plan = operations.split_island(plan, iid, target_faces=[0, 1])
    assert len(new_plan.islands) == 2
    sizes = sorted(len(i.face_ids) for i in new_plan.islands)
    assert sizes == [2, 14]


def test_merge_islands_action():
    plan = plan_islands(fixtures.build_cube())
    ids = [i.island_id for i in plan.islands[:3]]
    merged = operations.merge_islands(plan, ids)
    assert len(merged.islands) == 4  # 3 merged into 1, plus 3 untouched


def test_protect_region_marks_island():
    m = fixtures.build_grid_plane(4, 4)
    plan = plan_islands(m)
    protected = operations.protect_region(plan, [0])
    assert protected.islands[0].protected is True


def test_faces_for_region_keywords():
    m = fixtures.build_cube()
    top = operations.faces_for_region(m, "top")
    bottom = operations.faces_for_region(m, "bottom")
    assert top and bottom
    assert set(top).isdisjoint(set(bottom))
