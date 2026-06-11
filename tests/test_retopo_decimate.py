"""Phase 1 low-poly generator: vertex-clustering decimation (retopology plan §10).

These exercise the Blender-free core that the QuadriFlow path falls back to and
that ``--provider mock`` runs offline (plan §15.12). They verify the Phase 1
completion criterion: a high-poly mesh is reduced to a separate, lower-poly mesh
near a chosen target face count.
"""

import math

from retopo_agent.geometry.decimate import (
    RetopoResult,
    bounding_box_diagonal,
    cluster_decimate,
    decimate_to_target,
)
from retopo_agent.io.fixtures import build_subdivided_cube, build_uv_sphere


def test_fixtures_face_counts_match_formula():
    # sphere: segments*(rings-2) quads + 2*segments cap tris
    sph = build_uv_sphere(segments=24, rings=16)
    assert sph.face_count == 24 * (16 - 2) + 2 * 24
    # cube: 6 * divisions^2 quads, welded into one closed manifold
    cube = build_subdivided_cube(divisions=10)
    assert cube.face_count == 6 * 10 * 10
    # surface lattice points of an n^3 grid cube: (n+1)^3 - (n-1)^3
    assert cube.vertex_count == 11 ** 3 - 9 ** 3


def test_cluster_decimate_reduces_and_keeps_valid_faces():
    high = build_uv_sphere(segments=40, rings=28)
    low = cluster_decimate(high, grid=8)

    assert low.face_count > 0
    assert low.face_count < high.face_count
    assert low.vertex_count < high.vertex_count
    # Every surviving face is a non-degenerate polygon with >=3 distinct verts.
    assert all(len(set(f.vertex_ids)) >= 3 for f in low.faces)
    assert all(f.area_3d > 0 for f in low.faces)
    # It is a distinct object id, not a mutation of the input.
    assert low.object_id != high.object_id
    assert high.face_count == 40 * (28 - 2) + 2 * 40  # input untouched


def test_finer_grid_yields_more_faces_monotonic():
    high = build_uv_sphere(segments=48, rings=32)
    counts = [cluster_decimate(high, grid=g).face_count for g in (3, 6, 12, 24)]
    assert counts == sorted(counts)  # monotonically non-decreasing
    assert counts[0] < counts[-1]


def test_decimate_to_target_lands_near_target():
    high = build_uv_sphere(segments=64, rings=48)  # 64*46 + 128 = 3072 faces
    target = 400
    result = decimate_to_target(high, target)

    assert isinstance(result, RetopoResult)
    assert result.source_face_count == high.face_count
    assert result.actual_face_count < high.face_count
    # Within the spec's "retry" tolerance band (plan §15.6: target_error <= 0.30).
    assert result.target_error_ratio <= 0.30


def test_decimate_is_deterministic():
    high = build_subdivided_cube(divisions=16)
    a = decimate_to_target(high, 200)
    b = decimate_to_target(high, 200)
    assert a.grid == b.grid
    assert a.actual_face_count == b.actual_face_count
    assert [v.co for v in a.low_mesh.vertices] == [v.co for v in b.low_mesh.vertices]
    assert [f.vertex_ids for f in a.low_mesh.faces] == [f.vertex_ids for f in b.low_mesh.faces]


def test_target_at_or_above_source_passes_through():
    high = build_subdivided_cube(divisions=6)  # 216 faces
    result = decimate_to_target(high, high.face_count + 1000)
    assert result.method == "passthrough"
    assert result.actual_face_count == high.face_count


def test_report_dict_shape():
    high = build_uv_sphere(segments=32, rings=24)
    report = decimate_to_target(high, 300).to_dict()
    for key in (
        "method",
        "source_face_count",
        "target_face_count",
        "actual_face_count",
        "target_error_ratio",
        "grid",
    ):
        assert key in report
    assert report["target_face_count"] == 300


def test_bounding_box_diagonal_of_unit_sphere():
    sph = build_uv_sphere(segments=24, rings=16, radius=1.0)
    # Diameter 2 along each axis -> diagonal of a 2x2x2 box = 2*sqrt(3).
    assert math.isclose(bounding_box_diagonal(sph), 2.0 * math.sqrt(3.0), rel_tol=1e-3)
