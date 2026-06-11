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


def test_strip_unwrap_straightens_curved_ribbon():
    import math

    m = fixtures.build_curved_strip(12, 1.0, 1.3, math.pi)
    faces = [f.id for f in m.faces]

    # Per-vertex UV from the first loop of each vertex.
    def rails(uvm):
        v2uv = {}
        for f in m.faces:
            for li in f.loop_indices:
                v2uv.setdefault(m.loops[li].vertex_id, uvm.get(li))
        inner = np.array([v2uv[i] for i in range(m.vertex_count) if i % 2 == 0])
        return inner

    def off_axis_ratio(pts):
        c = pts - pts.mean(0)
        s = np.linalg.svd(c, compute_uv=False)
        return float(s[1] / (s[0] + 1e-9))

    from uv_agent.geometry.projection import project_island_planar, project_island_strip

    uv_p = UVMap.for_mesh(m)
    project_island_planar(m, faces, uv_p)
    uv_s = UVMap.for_mesh(m)
    assert project_island_strip(m, faces, uv_s) is True

    # Planar keeps the arc curved; strip makes the rail (nearly) straight.
    assert off_axis_ratio(rails(uv_p)) > 0.2
    assert off_axis_ratio(rails(uv_s)) < 1e-6

    # And the straightened strip has no folded faces (consistent winding).
    def shoelace(uvs):
        s = 0.0
        for i in range(len(uvs)):
            x1, y1 = uvs[i]
            x2, y2 = uvs[(i + 1) % len(uvs)]
            s += x1 * y2 - x2 * y1
        return s
    signs = [shoelace([uv_s.get(li) for li in m.faces[f].loop_indices]) for f in faces]
    assert all(s > 0 for s in signs) or all(s < 0 for s in signs)


def test_grid_island_is_not_treated_as_strip():
    # A 2D grid (plane) is not a 1-wide chain, so strip unwrap must decline.
    from uv_agent.geometry.projection import project_island_strip

    m = fixtures.build_grid_plane(4, 4)
    assert project_island_strip(m, [f.id for f in m.faces], UVMap.for_mesh(m)) is False


def test_grid_unwrap_straightens_curved_band():
    import math

    from uv_agent.geometry.projection import (
        _signed_area_loop,
        project_island_grid,
        project_island_planar,
    )

    m = fixtures.build_curved_band(segments=12, width_quads=3, total_angle=math.pi)
    faces = [f.id for f in m.faces]

    uv_p = UVMap.for_mesh(m)
    project_island_planar(m, faces, uv_p)
    uv_g = UVMap.for_mesh(m)
    assert project_island_grid(m, faces, uv_g) is True

    def boundary_rail_offaxis(uvm):
        # Outermost ring vertices (w == width) should be collinear after unwrap.
        rings = 4  # width_quads + 1
        outer = [v for v in range(m.vertex_count) if v % rings == rings - 1]
        v2uv = {}
        for f in m.faces:
            for li in f.loop_indices:
                v2uv.setdefault(m.loops[li].vertex_id, uvm.get(li))
        pts = np.array([v2uv[v] for v in outer])
        c = pts - pts.mean(0)
        s = np.linalg.svd(c, compute_uv=False)
        return float(s[1] / (s[0] + 1e-9))

    assert boundary_rail_offaxis(uv_p) > 0.2  # planar keeps it curved
    assert boundary_rail_offaxis(uv_g) < 1e-6  # grid unwrap straightens it

    # No folded faces (consistent winding).
    signs = [_signed_area_loop([uv_g.get(li) for li in m.faces[f].loop_indices]) for f in faces]
    assert all(s > 0 for s in signs) or all(s < 0 for s in signs)


def test_flat_grid_unwrap_matches_geometry():
    # A flat grid plane through grid unwrap stays undistorted (areas preserved).
    from uv_agent.geometry.projection import project_island_grid

    m = fixtures.build_grid_plane(4, 4)
    uvm = UVMap.for_mesh(m)
    assert project_island_grid(m, [f.id for f in m.faces], uvm) is True
    for f in m.faces:
        p = uvm.uv[f.loop_indices]
        area = 0.0
        for i in range(len(p)):
            x1, y1 = p[i]
            x2, y2 = p[(i + 1) % len(p)]
            area += x1 * y2 - x2 * y1
        assert np.isclose(abs(area) / 2, f.area_3d, atol=1e-6)


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
