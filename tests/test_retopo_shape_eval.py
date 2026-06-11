"""Shape-preservation evaluator (retopology plan §6.7, §10 Phase 3, §15.6).

Confirms the Phase 3 metrics: bbox-diagonal-normalized surface distance,
normal deviation, volume error, and the accepted/retry/failed banding.
"""

import math

from retopo_agent.geometry.decimate import decimate_to_target
from retopo_agent.geometry.shape_eval import (
    build_shape_report,
    evaluate_shape_match,
    mesh_volume,
    _triangulate_arrays,
)
from retopo_agent.io.fixtures import build_uv_sphere
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_cube, build_grid_plane


def _translated(mesh: MeshGraph, dx: float, dy: float = 0.0, dz: float = 0.0) -> MeshGraph:
    verts = [(v.co[0] + dx, v.co[1] + dy, v.co[2] + dz) for v in mesh.vertices]
    return MeshGraph.from_faces(mesh.object_id + "_t", verts, [f.vertex_ids for f in mesh.faces])


def _scaled(mesh: MeshGraph, s: float) -> MeshGraph:
    verts = [(v.co[0] * s, v.co[1] * s, v.co[2] * s) for v in mesh.vertices]
    return MeshGraph.from_faces(mesh.object_id + "_s", verts, [f.vertex_ids for f in mesh.faces])


# -- identical / near-identical --------------------------------------------


def test_identical_mesh_is_perfect():
    high = build_uv_sphere(segments=20, rings=12)
    rep = evaluate_shape_match(high, high)
    assert rep.surface_distance_mean_ratio < 1e-6
    assert rep.surface_distance_max_ratio < 1e-6
    assert rep.normal_deviation_mean_deg < 1e-6
    assert rep.volume_error_ratio is not None and rep.volume_error_ratio < 1e-6
    assert rep.status == "accepted"


def test_decimated_sphere_stays_close():
    high = build_uv_sphere(segments=48, rings=32)
    low = decimate_to_target(high, 400).low_mesh
    rep = evaluate_shape_match(high, low)
    # Clustering pulls vertices slightly inside the sphere, but not far.
    assert 0.0 < rep.surface_distance_mean_ratio < 0.05
    assert rep.status in {"accepted", "retry"}


# -- surface distance ------------------------------------------------------


def test_translation_increases_distance_monotonically():
    cube = build_cube()
    near = evaluate_shape_match(cube, _translated(cube, 0.02))
    far = evaluate_shape_match(cube, _translated(cube, 0.5))
    assert far.surface_distance_mean_ratio > near.surface_distance_mean_ratio
    assert far.status == "failed"


def test_large_offset_fails():
    sph = build_uv_sphere(segments=16, rings=10)
    rep = evaluate_shape_match(sph, _translated(sph, 2.0))  # move by ~the diameter
    assert rep.surface_distance_max_ratio > 0.10
    assert rep.status == "failed"


# -- normal deviation ------------------------------------------------------


def test_flipped_winding_folds_to_zero_deviation():
    plane = build_grid_plane(4, 4)
    flipped = MeshGraph.from_faces(
        "flip", [v.co for v in plane.vertices], [list(reversed(f.vertex_ids)) for f in plane.faces]
    )
    rep = evaluate_shape_match(plane, flipped)
    # Same geometry, reversed winding -> normals are anti-parallel but folded to 0.
    assert rep.surface_distance_mean_ratio < 1e-6
    assert rep.normal_deviation_mean_deg < 1e-6


def test_orthogonal_orientation_detected():
    plane = build_grid_plane(4, 4)  # lies in XY, normal +Z
    # Rotate +90 deg about X: (x, y, z) -> (x, -z, y); a vertical plane, normal +Y.
    verts = [(v.co[0], -v.co[2], v.co[1]) for v in plane.vertices]
    vertical = MeshGraph.from_faces("vert", verts, [f.vertex_ids for f in plane.faces])
    rep = evaluate_shape_match(plane, vertical)
    assert rep.normal_deviation_mean_deg > 45.0  # ~90 deg between +Z and +Y


# -- volume ----------------------------------------------------------------


def test_volume_error_of_shrunken_cube():
    cube = build_cube()  # unit cube, volume 1
    a, b, c, _ = _triangulate_arrays(_scaled(cube, 0.5))
    assert math.isclose(mesh_volume(a, b, c), 0.125, rel_tol=1e-6)  # 0.5^3
    rep = evaluate_shape_match(cube, _scaled(cube, 0.5))
    assert rep.volume_error_ratio is not None
    assert math.isclose(rep.volume_error_ratio, 0.875, rel_tol=1e-3)


# -- banding ---------------------------------------------------------------


def test_build_shape_report_bands():
    acc = build_shape_report(bbox_diagonal=1.0, distances=[0.005] * 9 + [0.02],
                             normal_angles_deg=[5.0], volume_error_ratio=0.0)
    assert acc.status == "accepted"
    retry = build_shape_report(bbox_diagonal=1.0, distances=[0.02] * 10,
                               normal_angles_deg=[5.0], volume_error_ratio=None)
    assert retry.status == "retry"  # mean ratio 0.02 in (0.01, 0.03]
    failed = build_shape_report(bbox_diagonal=1.0, distances=[0.05] * 10,
                                normal_angles_deg=[5.0], volume_error_ratio=None)
    assert failed.status == "failed"
    normal_fail = build_shape_report(bbox_diagonal=1.0, distances=[0.001] * 10,
                                     normal_angles_deg=[30.0], volume_error_ratio=None)
    assert normal_fail.status == "failed"  # normal deviation 30 deg > 25


def test_report_dict_has_phase3_fields():
    rep = evaluate_shape_match(build_cube(), build_cube()).to_dict()
    for key in (
        "surface_distance_mean_ratio",
        "surface_distance_max_ratio",
        "normal_deviation_mean_deg",
        "bounding_box_diagonal",
        "status",
    ):
        assert key in rep
