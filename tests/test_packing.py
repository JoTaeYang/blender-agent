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
