from uv_agent.geometry.evaluation import evaluate_uv_solution, per_island_metrics
from uv_agent.geometry.packing import pack_islands
from uv_agent.geometry.projection import project_island
from uv_agent.geometry.solution import UVMap
from uv_agent.io import fixtures
from uv_agent.planner.island_planner import plan_islands


def _evaluate(mesh, force_projection=None, **plan_kw):
    plan = plan_islands(mesh, **plan_kw)
    if force_projection:
        for isl in plan.islands:
            isl.projection = force_projection
    uvm = UVMap.for_mesh(mesh)
    for isl in plan.islands:
        project_island(mesh, isl.face_ids, uvm, isl.projection)
    pack_islands(mesh, plan, uvm)
    return plan, uvm, evaluate_uv_solution(mesh, plan, uvm)


def test_flat_plane_is_perfect():
    _, _, ev = _evaluate(fixtures.build_grid_plane(4, 4))
    assert ev.overlap_ratio == 0.0
    assert ev.stretch_score == 0.0
    assert ev.angle_distortion == 0.0
    assert ev.status == "accepted"
    assert ev.packing_efficiency >= 0.95


def test_cube_per_face_is_undistorted():
    _, _, ev = _evaluate(fixtures.build_cube())
    assert ev.island_count == 6
    assert ev.overlap_ratio == 0.0
    assert ev.stretch_score == 0.0
    assert ev.status == "accepted"


def test_planar_projection_of_tube_is_bad():
    # Forcing a single planar island onto a tube folds half of it.
    _, _, ev = _evaluate(fixtures.build_cylinder(16, 4), force_projection="planar", angle_threshold=45)
    assert ev.overlap_ratio > 0.3
    assert ev.stretch_score > 0.5
    assert ev.status == "needs_repair"


def test_cylindrical_projection_of_tube_is_good():
    _, _, ev = _evaluate(fixtures.build_cylinder(16, 4), force_projection="cylindrical", angle_threshold=45)
    assert ev.overlap_ratio == 0.0
    assert ev.stretch_score < 0.05
    assert ev.status == "accepted"


def test_per_island_metrics_match_status():
    plan, uvm, _ = _evaluate(fixtures.build_cylinder(16, 4), force_projection="planar", angle_threshold=45)
    m = fixtures.build_cylinder(16, 4)
    metrics = per_island_metrics(m, plan.islands[0].face_ids, uvm)
    assert metrics["overlap_ratio"] > 0.3
