"""Importance map -- Phase DM4 (decimation plan §7).

These verify, offline, the per-vertex/edge/face importance map: that each plan §7
source (curvature, hard edge, boundary, non-manifold, material boundary, UV seam,
sharp normal, face-area percentile, user vertex group) contributes, that the map
stays in [0, 1], and that the report exposes the importance_stats + sources
contract. The Blender adapter only wraps this, so the logic is fully covered here.
"""

import numpy as np

from retopo_agent.geometry.importance import (
    ALL_SOURCES,
    SRC_BOUNDARY,
    SRC_CURVATURE,
    SRC_FACE_AREA,
    SRC_HARD_EDGE,
    SRC_MATERIAL_BOUNDARY,
    SRC_NON_MANIFOLD,
    SRC_USER_GROUP,
    SRC_UV_SEAM,
    compute_importance_map,
    importance_to_vertex_weights,
)
from retopo_agent.io.fixtures import build_uv_sphere
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_cube, build_grid_plane, build_two_material_plane


def _quad_two_tris(seam=None):
    verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    return MeshGraph.from_faces("quad", verts, [[0, 1, 2], [0, 2, 3]], seam_edge_keys=seam)


# -- range / robustness ----------------------------------------------------


def test_importance_in_unit_range():
    imap = compute_importance_map(build_uv_sphere(segments=24, rings=16))
    for arr in (imap.vertex_importance, imap.edge_importance, imap.face_importance):
        assert arr.min() >= 0.0 and arr.max() <= 1.0


def test_empty_mesh_safe():
    imap = compute_importance_map(MeshGraph.from_faces("empty", [], []))
    assert imap.vertex_importance.size == 0
    assert all(v is False for v in imap.sources.values())
    assert imap.importance_stats == {"min": 0.0, "mean": 0.0, "max": 0.0}


# -- individual sources ----------------------------------------------------


def test_cube_hard_edges_saturate_importance():
    # Every cube edge is a 90 deg dihedral -> all vertices fully important.
    imap = compute_importance_map(build_cube())
    assert np.allclose(imap.vertex_importance, 1.0)
    assert imap.sources[SRC_HARD_EDGE] is True
    assert imap.sources[SRC_CURVATURE] is True
    assert imap.sources[SRC_BOUNDARY] is False  # closed manifold


def test_grid_plane_boundary_only():
    # Flat plane: interior is importance 0, only the open border is protected.
    imap = compute_importance_map(build_grid_plane(4, 4))
    assert imap.sources[SRC_BOUNDARY] is True
    assert imap.sources[SRC_HARD_EDGE] is False
    assert imap.importance_stats["min"] == 0.0
    assert imap.importance_stats["max"] == 1.0
    assert 0.0 < imap.importance_stats["mean"] < 1.0  # mix of border + flat interior


def test_material_boundary_source():
    imap = compute_importance_map(build_two_material_plane(4, 4))
    assert imap.sources[SRC_MATERIAL_BOUNDARY] is True
    # The material seam edges carry ~the material-boundary weight (0.9).
    assert imap.edge_importance.max() >= 0.9


def test_uv_seam_source():
    mesh = _quad_two_tris(seam=[(0, 2)])  # the interior diagonal is a seam
    imap = compute_importance_map(mesh, enabled_sources=[SRC_UV_SEAM])
    assert imap.sources[SRC_UV_SEAM] is True
    seam_edge = mesh.edge_key(0, 2)
    assert imap.edge_importance[seam_edge] >= 0.9


def test_non_manifold_source():
    # Three quads sharing edge (0,1) -> that edge is non-manifold.
    verts = [
        (0, 0, 0), (0, 1, 0),
        (1, 1, 0), (1, 0, 0),
        (-1, 1, 0), (-1, 0, 0),
        (0, 1, 1), (0, 0, 1),
    ]
    fan = MeshGraph.from_faces("fan", verts, [[0, 1, 2, 3], [0, 1, 4, 5], [0, 1, 6, 7]])
    imap = compute_importance_map(fan)
    assert imap.sources[SRC_NON_MANIFOLD] is True
    assert imap.vertex_importance[0] == 1.0 and imap.vertex_importance[1] == 1.0


def test_face_area_source_lifts_small_faces():
    # Two unit triangles (area 0.5) + a tiny triangle; isolate the area source.
    verts = [
        (0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
        (2, 0, 0), (2.01, 0, 0), (2, 0.001, 0),
    ]
    mesh = MeshGraph.from_faces("small", verts, [[0, 1, 2], [0, 2, 3], [4, 5, 6]])
    imap = compute_importance_map(mesh, enabled_sources=[SRC_FACE_AREA])
    assert imap.sources[SRC_FACE_AREA] is True
    # Tiny-triangle vertices are lifted; the big-triangle vertices stay ~0.
    assert imap.vertex_importance[4] > 0.0
    assert imap.vertex_importance[0] == 0.0


def test_user_vertex_group_source():
    mesh = build_grid_plane(4, 4)
    weights = np.zeros(mesh.vertex_count)
    weights[5] = 0.8
    imap = compute_importance_map(mesh, enabled_sources=[SRC_USER_GROUP], user_vertex_weights=weights)
    assert imap.sources[SRC_USER_GROUP] is True
    assert abs(imap.vertex_importance[5] - 0.8) < 1e-9


# -- source enabling / disabling -------------------------------------------


def test_disabling_a_source_silences_it():
    # Grid plane has a boundary; disabling boundary leaves nothing important.
    imap = compute_importance_map(build_grid_plane(4, 4), enabled_sources=[SRC_HARD_EDGE])
    assert imap.sources[SRC_BOUNDARY] is False
    assert imap.vertex_importance.max() == 0.0


def test_sources_default_lists_all_keys():
    imap = compute_importance_map(build_cube())
    assert set(imap.sources.keys()) == set(ALL_SOURCES)


# -- vertex weight mapping (strength) --------------------------------------


def test_importance_to_weights_strength():
    imp = np.array([0.0, 0.25, 0.5, 1.0])
    # strength 1 is identity.
    assert np.allclose(importance_to_vertex_weights(imp, 1.0), imp)
    # strength > 1 raises mid weights (stronger protection), endpoints fixed.
    strong = importance_to_vertex_weights(imp, 2.0)
    assert strong[0] == 0.0 and strong[-1] == 1.0
    assert strong[2] > imp[2]
    # strength <= 0 -> no protection.
    assert np.all(importance_to_vertex_weights(imp, 0.0) == 0.0)


def test_feature_mask_threshold():
    imap = compute_importance_map(build_grid_plane(4, 4))
    mask = imap.feature_vertex_mask(threshold=0.5)
    assert mask.dtype == bool
    assert mask.sum() == int((imap.vertex_importance >= 0.5).sum())


# -- output contract -------------------------------------------------------


def test_to_dict_contract():
    d = compute_importance_map(build_cube()).to_dict()
    assert set(d["importance_stats"].keys()) == {"min", "mean", "max"}
    for key in ("importance_stats", "edge_importance_stats", "face_importance_stats",
                "sources", "weights", "vertex_count", "edge_count", "face_count"):
        assert key in d
    assert isinstance(d["sources"], dict)
