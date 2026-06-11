"""Normal / visual cleanup for Decimation Optimize mode (Phase D4, decimation §6.4, §7).

Exercises the Blender-free core that models Auto Smooth + Weighted Normal: split
shading normals by smoothing group, and the before/after normal-deviation metric.
The Phase D4 completion criterion -- "normal deviation ... should improve" -- is
the assertion that smoothed deviation is below flat deviation on a curved LOD.
"""

import numpy as np

from retopo_agent.geometry.decimate import decimate_to_target
from retopo_agent.geometry.normals import (
    NormalCleanupReport,
    evaluate_normal_cleanup,
    face_corner_normals,
    face_shading_normals,
)
from retopo_agent.io.fixtures import build_subdivided_cube, build_uv_sphere
from uv_agent.io.fixtures import build_grid_plane


def _distinct_corner_normals(mesh, corners, vid, decimals=3):
    fs = [f.id for f in mesh.faces if vid in f.vertex_ids]
    return {tuple(np.round(corners[(fid, vid)], decimals)) for fid in fs}


# -- the completion criterion ----------------------------------------------


def test_smooth_normals_reduce_deviation_on_curved_mesh():
    high = build_uv_sphere(segments=48, rings=32)
    low = decimate_to_target(high, 500).low_mesh
    rep = evaluate_normal_cleanup(high, low, auto_smooth_angle=30.0, weighted=True)

    assert isinstance(rep, NormalCleanupReport)
    assert rep.sample_count > 0
    # Auto Smooth pulls the per-face shading normals toward the true sphere normal.
    assert rep.normal_deviation_mean_deg_smoothed < rep.normal_deviation_mean_deg_flat
    assert rep.improvement_deg > 0.0
    assert rep.status == "improved"


def test_flat_surface_has_nothing_to_improve():
    # A flat plane already shades perfectly; smoothing must not change it.
    plane = build_grid_plane(4, 4)
    rep = evaluate_normal_cleanup(plane, plane, auto_smooth_angle=30.0)
    assert abs(rep.improvement_deg) <= 0.01
    assert rep.status == "unchanged"


# -- smoothing split control -----------------------------------------------


def test_auto_smooth_keeps_creases_split():
    # At a cube corner three faces meet across 90 deg edges -> three shading
    # normals (creases stay crisp), not one averaged blob.
    cube = build_subdivided_cube(divisions=4)
    corners = face_corner_normals(cube, auto_smooth_angle=30.0)
    corner_v = min(
        (v.id for v in cube.vertices if len(_distinct_corner_normals(cube, corners, v.id)) == 3),
        default=None,
    )
    assert corner_v is not None
    assert len(_distinct_corner_normals(cube, corners, corner_v)) == 3


def test_flat_face_interior_stays_flat():
    # Interior vertices of a cube face are surrounded by coplanar faces -> one
    # shading normal equal to that face's normal.
    cube = build_subdivided_cube(divisions=6)
    sn = face_shading_normals(cube, auto_smooth_angle=30.0)
    for i, f in enumerate(cube.faces):
        # every shading normal is a unit vector
        assert abs(np.linalg.norm(sn[i]) - 1.0) < 1e-6
    # a face deep inside a flat side keeps (approximately) its own normal
    flat_face = cube.faces[0]
    assert abs(abs(float(np.dot(sn[0], flat_face.normal))) - 1.0) < 1e-6


def test_zero_angle_splits_everything_no_smoothing():
    # auto_smooth_angle = 0 marks every non-flat edge sharp, so shading == flat.
    high = build_uv_sphere(segments=32, rings=20)
    low = decimate_to_target(high, 400).low_mesh
    rep = evaluate_normal_cleanup(high, low, auto_smooth_angle=0.0)
    assert abs(rep.improvement_deg) <= 0.01  # nothing smoothed -> no change


# -- weighted normal -------------------------------------------------------


def test_weighted_differs_from_uniform_on_uneven_fans():
    # Where incident faces have unequal areas + differing normals, area weighting
    # changes the result (the "Weighted Normal" modifier).
    sph = build_uv_sphere(segments=24, rings=16)
    weighted = face_shading_normals(sph, auto_smooth_angle=180.0, weighted=True)
    uniform = face_shading_normals(sph, auto_smooth_angle=180.0, weighted=False)
    assert not np.allclose(weighted, uniform)
    assert np.allclose(np.linalg.norm(weighted, axis=1), 1.0, atol=1e-6)


def test_report_dict_serializable():
    high = build_uv_sphere(segments=32, rings=20)
    low = decimate_to_target(high, 400).low_mesh
    d = evaluate_normal_cleanup(high, low).to_dict()
    for key in (
        "auto_smooth_angle_deg",
        "weighted",
        "normal_deviation_mean_deg_flat",
        "normal_deviation_mean_deg_smoothed",
        "improvement_deg",
        "status",
    ):
        assert key in d
