import itertools

from uv_agent.geometry.packing import island_bbox, pack_islands
from uv_agent.geometry.projection import project_island
from uv_agent.geometry.solution import UVMap
from uv_agent.io import fixtures
from uv_agent.planner.island_planner import PlanConstraints, plan_islands


def _solve(mesh, **plan_kw):
    plan = plan_islands(mesh, constraints=PlanConstraints(padding_px=8, texture_size_px=512), **plan_kw)
    uvm = UVMap.for_mesh(mesh)
    for isl in plan.islands:
        project_island(mesh, isl.face_ids, uvm, isl.projection)
    transforms = pack_islands(mesh, plan, uvm)
    return plan, uvm, transforms


def _boxes_overlap(a, b, eps=1e-9):
    return a[0] < b[2] - eps and b[0] < a[2] - eps and a[1] < b[3] - eps and b[1] < a[3] - eps


def test_cube_pack_in_bounds_and_no_overlap():
    m = fixtures.build_cube()
    plan, uvm, tr = _solve(m)
    assert (uvm.uv >= -1e-6).all() and (uvm.uv <= 1 + 1e-6).all()
    boxes = [island_bbox(m, i, uvm) for i in plan.islands]
    for a, b in itertools.combinations(boxes, 2):
        assert not _boxes_overlap(a, b)


def test_single_scale_shared_across_islands():
    m = fixtures.build_cube()
    _, _, tr = _solve(m)
    scales = {round(t.scale, 9) for t in tr}
    assert len(scales) == 1  # one global scale preserves relative texel density


def test_many_islands_still_fit():
    m = fixtures.build_cylinder(8, 3)
    plan, uvm, tr = _solve(m, angle_threshold=20)
    assert len(plan.islands) > 1
    assert (uvm.uv >= -1e-6).all() and (uvm.uv <= 1 + 1e-6).all()
    boxes = [island_bbox(m, i, uvm) for i in plan.islands if i.face_ids]
    for a, b in itertools.combinations(boxes, 2):
        assert not _boxes_overlap(a, b)


def _solve_strategy(mesh, strategy, **plan_kw):
    plan = plan_islands(mesh, constraints=PlanConstraints(padding_px=8, texture_size_px=1024), **plan_kw)
    uvm = UVMap.for_mesh(mesh)
    for isl in plan.islands:
        project_island(mesh, isl.face_ids, uvm, isl.projection)
    pack_islands(mesh, plan, uvm, strategy=strategy)
    return plan, uvm


def test_maxrects_overlap_free_and_in_bounds():
    for mesh, kw in [(fixtures.build_cube(), {}), (fixtures.build_cylinder(8, 3), {"angle_threshold": 20})]:
        plan, uvm = _solve_strategy(mesh, "maxrects", **kw)
        assert (uvm.uv >= -1e-6).all() and (uvm.uv <= 1 + 1e-6).all()
        boxes = [island_bbox(mesh, i, uvm) for i in plan.islands if i.face_ids]
        for a, b in itertools.combinations(boxes, 2):
            assert not _boxes_overlap(a, b)


def test_maxrects_keeps_single_scale():
    plan = plan_islands(fixtures.build_cube())
    uvm = UVMap.for_mesh(fixtures.build_cube())
    m = fixtures.build_cube()
    for isl in plan.islands:
        project_island(m, isl.face_ids, uvm, isl.projection)
    tr = pack_islands(m, plan, uvm, strategy="maxrects")
    assert len({round(t.scale, 9) for t in tr}) == 1


def test_auto_never_worse_than_shelf():
    # The auto strategy must never report lower packing efficiency than shelf.
    from uv_agent.geometry.evaluation import evaluate_uv_solution

    cases = [
        (fixtures.build_cube(), {}),
        (fixtures.build_grid_plane(4, 4), {}),
        (fixtures.build_cylinder(8, 3), {"angle_threshold": 20}),
    ]
    for mesh, kw in cases:
        plan_s, uv_s = _solve_strategy(mesh, "shelf", **kw)
        plan_a, uv_a = _solve_strategy(mesh, "auto", **kw)
        eff_s = evaluate_uv_solution(mesh, plan_s, uv_s).packing_efficiency
        eff_a = evaluate_uv_solution(mesh, plan_a, uv_a).packing_efficiency
        assert eff_a >= eff_s - 1e-6
