"""U1 — chart segmentation (chart-UV plan §5). Pure, Blender-free."""

from chart_uv_agent.fixtures import (
    build_capsule_with_spikes, build_displaced_sphere, build_humanoid_blob,
)
from chart_uv_agent.segmentation import (
    euler_characteristic, is_disk, mandatory_seam_edges, normal_cone_halfangle,
    segment, split_chart,
)
from retopo_agent.io.fixtures import build_subdivided_cube, build_uv_sphere
from uv_agent.io.fixtures import build_grid_plane

FIXTURES = lambda: [build_displaced_sphere(), build_capsule_with_spikes(), build_humanoid_blob()]


def _connected(mesh, face_ids) -> bool:
    adj = mesh.face_adjacency()
    fset = set(face_ids)
    seen = {face_ids[0]}
    stack = [face_ids[0]]
    while stack:
        cur = stack.pop()
        for nb, _ in adj[cur]:
            if nb in fset and nb not in seen:
                seen.add(nb)
                stack.append(nb)
    return len(seen) == len(face_ids)


# -- R2: mandatory 90° seams (user directive, verbatim) ---------------------


def test_mandatory_seams_are_all_folds_or_boundary():
    cube = build_subdivided_cube(divisions=6)
    seams = mandatory_seam_edges(cube, fold_angle=90.0)
    assert len(seams) > 0
    for eid in seams:
        e = cube.edges[eid]
        assert e.is_boundary or e.is_non_manifold or e.dihedral_angle >= 90.0


def test_every_90deg_fold_is_unconditionally_a_seam():
    cube = build_subdivided_cube(divisions=4)
    seams = mandatory_seam_edges(cube, fold_angle=90.0)
    folds = {e.id for e in cube.edges if len(e.face_ids) == 2 and e.dihedral_angle >= 90.0}
    assert folds.issubset(seams)  # R2: never dropped


def test_smooth_sphere_has_few_mandatory_fold_seams():
    sphere = build_uv_sphere(rings=24, segments=32)
    # A finely-tessellated smooth sphere has very few ≥90° folds (only near the poles)
    # — far below the ~30% an organic 30° threshold would mark (the confetti cause).
    frac = len(mandatory_seam_edges(sphere, fold_angle=90.0)) / sphere.edge_count
    assert frac < 0.05


# -- developability proxy + disk invariant ----------------------------------


def test_normal_cone_planar_is_zero_curved_is_large():
    plane = build_grid_plane(nx=4, ny=4)
    assert normal_cone_halfangle(plane, list(range(plane.face_count))) < 1e-6
    sphere = build_uv_sphere(rings=10, segments=16)
    assert normal_cone_halfangle(sphere, list(range(sphere.face_count))) > 80.0


def test_euler_and_disk():
    sphere = build_uv_sphere(rings=8, segments=12)
    assert euler_characteristic(sphere, list(range(sphere.face_count))) == 2  # closed
    assert not is_disk(sphere, list(range(sphere.face_count)))
    assert is_disk(sphere, [0])  # a single triangle is a disk


# -- segment(): invariants on every fixture ---------------------------------


def test_segment_covers_partitions_and_connects():
    for mesh in FIXTURES():
        seg = segment(mesh, cone_limit=50.0)
        charts = list(seg.charts.values())
        all_faces = [f for fs in charts for f in fs]
        assert sorted(all_faces) == list(range(mesh.face_count))  # cover + disjoint
        assert all(_connected(mesh, fs) for fs in charts)


def test_segment_charts_are_disks():
    for mesh in FIXTURES():
        seg = segment(mesh, cone_limit=50.0)
        assert all(is_disk(mesh, fs) for fs in seg.charts.values())


def test_segment_drives_charts_toward_cone_limit():
    for mesh in FIXTURES():
        seg = segment(mesh, cone_limit=50.0, max_charts=200)
        assert seg.chart_count < 200  # converged before the cap
        cones = [normal_cone_halfangle(mesh, fs) for fs in seg.charts.values()]
        # Most charts under the bar; absorb/merge (R1 minimality) may push a few
        # slightly over, but never wildly — bounded well under the closed-shell 180°.
        under = sum(c <= 50.0 + 1e-6 for c in cones)
        assert under >= 0.7 * len(cones)
        assert max(cones) <= 50.0 * 1.5


def test_split_yields_exactly_two_connected_charts():
    # Defect #1: one split must produce exactly two connected charts, never a shower.
    from chart_uv_agent.segmentation import flood_charts, mandatory_seam_edges
    for mesh in FIXTURES():
        seams = set(mandatory_seam_edges(mesh))
        chart = max(flood_charts(mesh, seams), key=len)  # biggest initial chart
        cf = set(chart)
        _, _, new_seams = split_chart(mesh, chart, seams)
        assert new_seams
        pieces = [c for c in flood_charts(mesh, seams | set(new_seams)) if set(c) & cf]
        assert len(pieces) == 2
        assert all(_connected(mesh, p) for p in pieces)


def test_disk_invariant_held_even_when_cap_exceeded():
    # Defect #2: disk-ification is non-negotiable, completed regardless of max_charts.
    for mesh in FIXTURES():
        seg = segment(mesh, cone_limit=50.0, max_charts=4)
        assert all(is_disk(mesh, fs) for fs in seg.charts.values())
        # The cap may legitimately be exceeded to keep the invariant; it's reported.
        final = [h for h in seg.history if h["stage"] == "final"][0]
        assert final["non_disk"] == 0


def test_no_confetti_charts():
    # Defect #3: tiny slivers are absorbed (min chart size 5) unless mandatory-walled.
    for mesh in (build_displaced_sphere(), build_humanoid_blob()):
        seg = segment(mesh, cone_limit=50.0)
        tiny = [fs for fs in seg.charts.values() if len(fs) < 5]
        assert tiny == []


def test_straighten_preserves_mandatory_seams_and_invariants():
    # U1.5: boundary straightening never re-routes a 90° fold, and keeps every chart a
    # connected disk of >= 5 faces (the disk + no-1-face guards).
    from chart_uv_agent.segmentation import (
        mandatory_seam_edges, segment, straighten_boundaries,
    )
    for mesh in FIXTURES():
        # Build a segmentation WITHOUT straightening, then straighten its seam set.
        seg = segment(mesh, cone_limit=60.0, straighten=False)
        seams = set(seg.seams)
        mandatory = mandatory_seam_edges(mesh, fold_angle=90.0)
        straighten_boundaries(mesh, seams, fold_angle=90.0)
        assert mandatory.issubset(seams)  # R2 folds never re-routed away
        charts = [c for c in _flood(mesh, seams) if c]
        assert all(is_disk(mesh, c) for c in charts)
        assert all(_connected(mesh, c) for c in charts)
        assert min(len(c) for c in charts) >= 5


def test_straighten_does_not_increase_total_boundary():
    from chart_uv_agent.segmentation import segment, straighten_boundaries
    mesh = build_displaced_sphere()
    seams = set(segment(mesh, cone_limit=60.0, straighten=False).seams)
    before = len(seams)
    straighten_boundaries(mesh, seams, fold_angle=90.0)
    assert len(seams) <= before  # straightening only ever reduces boundary length


def _flood(mesh, seams):
    from chart_uv_agent.segmentation import flood_charts
    return flood_charts(mesh, seams)


def test_seed_pair_is_distinct_and_cheap():
    # Defect #4: seeds via iterative farthest-normal (no O(n^2) gram).
    from chart_uv_agent.segmentation import _farthest_normal_seeds, _face_normals
    mesh = build_displaced_sphere(segments=24, rings=16)
    normals = _face_normals(mesh)
    a, b = _farthest_normal_seeds(list(range(mesh.face_count)), normals)
    assert a != b


def test_segment_is_deterministic():
    mesh = build_displaced_sphere()
    a = segment(mesh, cone_limit=50.0)
    b = segment(mesh, cone_limit=50.0)
    assert a.face_chart == b.face_chart
    assert a.seams == b.seams


def test_merge_pass_reduces_chart_count():
    mesh = build_displaced_sphere()
    with_merge = segment(mesh, cone_limit=50.0, merge=True).chart_count
    without = segment(mesh, cone_limit=50.0, merge=False).chart_count
    assert with_merge <= without  # merge (R1 minimality) never increases the count


def test_lower_cone_limit_yields_more_charts():
    mesh = build_displaced_sphere()
    coarse = segment(mesh, cone_limit=70.0).chart_count
    fine = segment(mesh, cone_limit=30.0).chart_count
    assert fine >= coarse  # tighter developability -> more charts


