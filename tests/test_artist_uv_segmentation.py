"""A1 semantic part segmentation — Blender-free unit tests (AUTO_ARTIST_UV_PLAN §5.A1 /
§AR2). The Blender SLIM unwrap / renders are exercised by the headless acceptance run;
the segmentation core is pure numpy on a MeshGraph and is fully tested here."""

import numpy as np

from artist_uv_agent.segmentation import (
    edge_barrier, edge_concavity, part_seam_edges, segment_parts,
)
from chart_uv_agent.fixtures import (
    build_capsule_with_spikes, build_displaced_sphere, build_humanoid_blob,
)
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.io.fixtures import build_grid_plane


def _centroids(mesh):
    from artist_uv_agent.segmentation import _face_centroids
    return _face_centroids(mesh)


# -- partition invariants ----------------------------------------------------

def _assert_partition(mesh, seg):
    seen = set()
    for p in seg.parts:
        assert p.face_ids, "empty part"
        assert not (seen & set(p.face_ids)), "parts overlap"
        seen |= set(p.face_ids)
    assert seen == set(range(mesh.face_count)), "parts do not cover every face"


def test_grid_plane_is_a_single_part():
    """A flat panel has no concave neck — it must stay ONE part (it becomes a single
    intact panel chart in A4), never fragmented into developable blobs."""
    mesh = build_grid_plane(nx=6, ny=6)
    seg = segment_parts(mesh)
    _assert_partition(mesh, seg)
    assert seg.part_count == 1


def test_protrusion_fixtures_split_off_parts():
    """Spiked / limbed blobs must separate the body from its protrusions (≥ 2 parts),
    with the protrusion parts strongly neck-walled (high confidence)."""
    for build in (build_capsule_with_spikes, build_humanoid_blob):
        mesh = build()
        seg = segment_parts(mesh)
        _assert_partition(mesh, seg)
        assert seg.part_count >= 2, f"{mesh.object_id} did not split"
        small = sorted(seg.parts, key=lambda p: p.face_count)[:-1]
        assert all(p.confidence >= 0.5 for p in small), "protrusion parts must be confident"


def test_smooth_blob_stays_compact():
    """A smoothly-curved blob must NOT shatter into many parts (mild curvature is not a
    part boundary); a small, stable handful is acceptable."""
    mesh = build_displaced_sphere()
    seg = segment_parts(mesh)
    _assert_partition(mesh, seg)
    assert 1 <= seg.part_count <= 6


def test_segmentation_is_deterministic():
    mesh = build_humanoid_blob()
    a = segment_parts(mesh).face_part
    b = segment_parts(mesh).face_part
    assert a == b


def test_confidence_in_unit_range():
    for build in (build_grid_plane, build_displaced_sphere, build_capsule_with_spikes):
        mesh = build() if build is not build_grid_plane else build_grid_plane(nx=5, ny=5)
        for p in segment_parts(mesh).parts:
            assert 0.0 <= p.confidence <= 1.0


# -- concavity sign ----------------------------------------------------------

def test_concavity_sign_convex_vs_concave():
    """A roof ridge reads convex (< 0); a valley reads concave (> 0)."""
    # Two faces sharing edge (0,1) on the x-axis. Ridge: both tilt UP (peak).
    ridge = MeshGraph.from_faces("ridge",
        [(0, 0, 0), (1, 0, 0), (-0.5, 1, 0.5), (1.5, 1, 0.5)],
        [(0, 1, 2), (1, 0, 3)])
    # share edge between the two tris is (0,1); build a clean shared-edge pair instead:
    ridge = MeshGraph.from_faces("ridge",
        [(0, -1, 0.5), (0, 1, 0.5), (-1, 0, 0), (1, 0, 0)],
        [(0, 1, 2), (1, 0, 3)])          # peak along y-axis at z=0.5, wings drop to z=0
    valley = MeshGraph.from_faces("valley",
        [(0, -1, -0.5), (0, 1, -0.5), (-1, 0, 0), (1, 0, 0)],
        [(0, 1, 2), (1, 0, 3)])          # trough along y-axis, wings rise
    c_ridge = _centroids(ridge)
    c_valley = _centroids(valley)
    shared_r = next(e.id for e in ridge.edges if len(e.face_ids) == 2)
    shared_v = next(e.id for e in valley.edges if len(e.face_ids) == 2)
    assert edge_concavity(ridge, shared_r, c_ridge) < 0      # ridge = convex
    assert edge_concavity(valley, shared_v, c_valley) > 0     # valley = concave


def test_flat_edge_has_zero_barrier():
    """A shallow fold (below DIHEDRAL_FLOOR) is not a part boundary."""
    mesh = build_grid_plane(nx=4, ny=4)
    c = _centroids(mesh)
    interior = [e.id for e in mesh.edges if len(e.face_ids) == 2]
    assert all(edge_barrier(mesh, e, c) == 0.0 for e in interior)


# -- seam floor --------------------------------------------------------------

def _fork_mesh():
    """A flat 3-prong fork (trident-like): a base block with three separated teeth, so a
    cross-section sweep along the long axis splits the top into three components."""
    cells = [(i, j) for i in range(5) for j in range(3)]          # base 5×3
    cells += [(i, j) for i in (0, 2, 4) for j in range(3, 9)]     # 3 teeth, 6 tall each
    vidx: dict[tuple, int] = {}
    verts = []

    def vid(i, j):
        if (i, j) not in vidx:
            vidx[(i, j)] = len(verts)
            verts.append((float(i), float(j), 0.0))
        return vidx[(i, j)]

    faces = [(vid(i, j), vid(i + 1, j), vid(i + 1, j + 1), vid(i, j + 1)) for i, j in cells]
    return MeshGraph.from_faces("fork", verts, faces)


def test_branch_split_separates_three_prongs():
    """A trident-like fork (one part from the concave-neck pass) is split into a shaft +
    three prongs by the axis cross-section sweep (the tine-separation capability)."""
    from artist_uv_agent.segmentation import split_branched_parts
    mesh = _fork_mesh()
    seg = segment_parts(mesh)
    assert seg.part_count == 1                                    # flat → one part initially
    split = split_branched_parts(mesh, seg, min_elong=1.5, min_tine=6, min_branches=3)
    _assert_partition(mesh, split)
    assert split.part_count >= 4                                  # shaft + 3 prongs
    small = sorted(p.face_count for p in split.parts)[:3]
    assert all(s >= 6 for s in small)                            # each prong is a real region


def test_branch_split_leaves_simple_tube_alone():
    """A simple (un-forked) mesh is returned unchanged — no false splits."""
    from artist_uv_agent.segmentation import split_branched_parts
    mesh = build_grid_plane(nx=6, ny=6)
    seg = segment_parts(mesh)
    assert split_branched_parts(mesh, seg).part_count == seg.part_count


def test_part_seam_edges_match_boundaries():
    """Every part-separating edge (and every mesh boundary) is a seam; no interior edge
    of a single part is."""
    mesh = build_capsule_with_spikes()
    seg = segment_parts(mesh)
    seams = part_seam_edges(mesh, seg.face_part)
    for e in mesh.edges:
        if e.is_boundary or e.is_non_manifold:
            assert e.id in seams
        elif len(e.face_ids) == 2:
            a, b = e.face_ids
            cross = seg.face_part[a] != seg.face_part[b]
            assert (e.id in seams) == cross
