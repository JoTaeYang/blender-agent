"""Feature detection + feature-aware retopology (retopology plan §6.1, §6.3, §10 Phase 5)."""

from retopo_agent.geometry.decimate import (
    decimate_to_target,
    feature_aware_decimate,
    feature_aware_decimate_to_target,
)
from retopo_agent.geometry.features import (
    analyze_features,
    detect_hard_edges,
    feature_vertex_mask,
    material_boundary_edges,
    plan_feature_preservation,
    vertex_feature_scores,
)
from retopo_agent.geometry.shape_eval import evaluate_shape_match
from retopo_agent.io.fixtures import build_subdivided_cube, build_uv_sphere
from uv_agent.io.fixtures import build_cube, build_grid_plane, build_two_material_plane


# -- detection -------------------------------------------------------------


def test_cube_edges_are_all_hard():
    cube = build_cube()  # every edge is a 90-deg crease
    hard = detect_hard_edges(cube, angle_threshold=30.0)
    assert len(hard) == cube.edge_count == 12
    # all 8 corners are feature vertices
    assert feature_vertex_mask(cube, angle_threshold=30.0).all()


def test_flat_plane_interior_not_hard():
    plane = build_grid_plane(6, 6)  # coplanar interior, open border
    hard = detect_hard_edges(plane, angle_threshold=30.0)
    # only the boundary edges qualify (silhouette); interior edges are flat (0 deg)
    assert all(plane.edges[e].is_boundary for e in hard)
    scores = vertex_feature_scores(plane)
    # an interior vertex (center of a 6x6 grid) is flat
    assert scores.min() == 0.0


def test_subdivided_cube_features_are_the_12_edges():
    cube = build_subdivided_cube(divisions=8)
    rep = analyze_features(cube, angle_threshold=30.0)
    # 12 cube edges * 8 segments = 96 hard interior edges (no open boundary: closed)
    assert rep.boundary_edge_count == 0
    assert rep.hard_edge_count == 12 * 8
    assert 0 < rep.feature_vertex_count < cube.vertex_count  # edges only, not faces


def test_material_boundary_detection():
    mat = build_two_material_plane(6, 6)  # split into two material slots at x=0
    mb = material_boundary_edges(mat)
    assert len(mb) > 0
    # plain grid plane has a single material -> no material seams
    assert material_boundary_edges(build_grid_plane(6, 6)) == []


def test_plan_feature_preservation_schema():
    plan = plan_feature_preservation(build_subdivided_cube(divisions=6), angle_threshold=30.0)
    ids = {r["region_id"] for r in plan["preserve_regions"]}
    assert "hard_edges" in ids
    assert all("priority" in r and "reason" in r for r in plan["preserve_regions"])
    assert plan["feature_vertex_count"] > 0


# -- feature-aware decimation ----------------------------------------------


def test_feature_aware_keeps_feature_vertices_exactly():
    cube = build_subdivided_cube(divisions=6)
    mask = feature_vertex_mask(cube, angle_threshold=30.0)
    low = feature_aware_decimate(cube, grid=2, feature_mask=mask)
    # Reducing, but every feature vertex's position survives in the output.
    assert low.face_count < cube.face_count
    low_coords = {tuple(round(c, 6) for c in v.co) for v in low.vertices}
    for vid, is_feature in enumerate(mask):
        if is_feature:
            assert tuple(round(c, 6) for c in cube.vertices[vid].co) in low_coords


def test_feature_aware_preserves_silhouette_better_than_uniform():
    high = build_subdivided_cube(divisions=12)  # 864 faces; corners are the features
    mask = feature_vertex_mask(high, angle_threshold=30.0)

    uniform = decimate_to_target(high, 150)
    feature = feature_aware_decimate_to_target(high, 150, mask)

    # Both reduce the polygon count...
    assert uniform.actual_face_count < high.face_count
    assert feature.actual_face_count < high.face_count

    uniform_shape = evaluate_shape_match(high, uniform.low_mesh)
    feature_shape = evaluate_shape_match(high, feature.low_mesh)
    # ...but the feature-aware result keeps the box silhouette far tighter
    # (uniform clustering rounds/cuts the corners -> large max deviation).
    assert feature_shape.surface_distance_max_ratio < uniform_shape.surface_distance_max_ratio
    assert feature.method == "feature_aware_cluster_decimate"


def test_smooth_sphere_has_few_features():
    # An organic mesh with no hard creases -> feature-aware ~ uniform.
    sph = build_uv_sphere(segments=32, rings=24)
    rep = analyze_features(sph, angle_threshold=30.0)
    # the lat-long sphere is smooth; at a 30-deg threshold few/no edges are hard
    assert rep.feature_vertex_ratio < 0.2
