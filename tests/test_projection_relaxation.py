import numpy as np

from uv_agent.geometry.projection import (
    project_island_cylindrical,
    project_island_planar,
)
from uv_agent.geometry.relaxation import build_island_topology, relax_island
from uv_agent.geometry.solution import UVMap
from uv_agent.io import fixtures
from uv_agent.planner.island_planner import plan_islands


def test_planar_projection_of_flat_plane_is_isometric():
    m = fixtures.build_grid_plane(4, 4)
    uvm = UVMap.for_mesh(m)
    project_island_planar(m, [f.id for f in m.faces], uvm)
    # A flat XY plane projects to its own coordinates (up to basis/sign), so
    # every face's UV area should equal its 3D area.
    for f in m.faces:
        p = uvm.uv[f.loop_indices]
        # shoelace area
        area = 0.0
        for i in range(len(p)):
            x1, y1 = p[i]
            x2, y2 = p[(i + 1) % len(p)]
            area += x1 * y2 - x2 * y1
        assert np.isclose(abs(area) / 2, f.area_3d, atol=1e-9)


def test_plane_topology_interior_and_boundary():
    m = fixtures.build_grid_plane(4, 4)
    topo = build_island_topology(m, [f.id for f in m.faces])
    assert len(topo.vertex_ids) == 25
    assert len(topo.boundary) == 16
    assert len(topo.interior) == 9


def test_relaxation_keeps_boundary_fixed_and_is_finite():
    m = fixtures.build_grid_plane(4, 4)
    plan = plan_islands(m)
    uvm = UVMap.for_mesh(m)
    project_island_planar(m, plan.islands[0].face_ids, uvm)
    topo = build_island_topology(m, plan.islands[0].face_ids)
    before = uvm.uv.copy()
    relax_island(m, plan.islands[0].face_ids, uvm, iterations=15)
    assert np.all(np.isfinite(uvm.uv))
    # Boundary loops unchanged.
    for v in topo.boundary:
        for li in topo.loops_of_vertex[v]:
            assert np.allclose(uvm.uv[li], before[li])


def test_cylindrical_projection_seam_unwrap_has_no_fold():
    m = fixtures.build_cylinder(16, 4)
    uvm = UVMap.for_mesh(m)
    project_island_cylindrical(m, [f.id for f in m.faces], uvm)
    # Every face must keep positive (consistent) winding after the seam fix.
    for f in m.faces:
        p = uvm.uv[f.loop_indices]
        area = 0.0
        for i in range(len(p)):
            x1, y1 = p[i]
            x2, y2 = p[(i + 1) % len(p)]
            area += x1 * y2 - x2 * y1
        assert area > 0  # no flipped/folded face
