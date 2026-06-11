"""Deterministic, Blender-free low-poly generator (retopology plan §10 Phase 1).

Inside Blender the prototype prefers QuadriFlow Remesh + Shrinkwrap (see
:mod:`retopo_agent.blender.retopo`). That path is unavailable in unit tests and
offline ``--provider mock`` runs (plan §15.12), so this module provides an
equivalent pure-Python reduction: **vertex-clustering decimation**
(Rossignac-Borrel).

The mesh is overlaid with a uniform cubic grid; all vertices that fall in the
same cell collapse to a single representative vertex (their average position).
Faces are remapped onto the surviving vertices, degenerate and duplicate faces
are dropped. A coarser grid yields fewer faces, so the output face count is a
monotonically non-decreasing function of the grid resolution -- which lets
:func:`decimate_to_target` binary-search a grid that lands near a target face
count (plan §15.5 "If face count deviates significantly from the target, adjust
the remesh ratio").

This is intentionally simple: it reduces polygon count deterministically and
preserves the silhouette reasonably, but does *not* produce quad-clean topology.
Quad flow is QuadriFlow's job in Blender (and a later phase offline). Topology
and shape *validation* of whatever comes out is Phase 2/3, not Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph


def _coords(mesh: MeshGraph) -> np.ndarray:
    return np.asarray([v.co for v in mesh.vertices], dtype=float)


def bounding_box_diagonal(mesh: MeshGraph) -> float:
    """Length of the mesh's axis-aligned bounding-box diagonal.

    The reference length the retopology spec normalises surface distances against
    (plan §15.6 ``surface_distance_ratio = distance / bounding_box_diagonal``).
    """
    co = _coords(mesh)
    if len(co) == 0:
        return 0.0
    return float(np.linalg.norm(co.max(axis=0) - co.min(axis=0)))


def _canonical_face(verts: list[int]) -> tuple[int, ...]:
    """A rotation/reflection-invariant key for a face's vertex loop, so true
    duplicate faces (same loop, any start vertex or winding) collapse to one."""
    n = len(verts)
    candidates = [tuple(verts[i:] + verts[:i]) for i in range(n)]
    rev = verts[::-1]
    candidates += [tuple(rev[i:] + rev[:i]) for i in range(n)]
    return min(candidates)


def cluster_decimate(mesh: MeshGraph, grid: int, *, object_id: str | None = None) -> MeshGraph:
    """Collapse vertices onto a ``grid``-resolution cubic lattice (plan §6.4).

    ``grid`` is the number of cells along the longest bounding-box axis. Larger
    ``grid`` keeps more detail (more faces); ``grid=1`` collapses (almost)
    everything. The result is a new :class:`MeshGraph`; the input is untouched.
    """
    grid = max(1, int(grid))
    co = _coords(mesh)
    oid = object_id or f"{mesh.object_id}_LOW"
    if len(co) == 0 or mesh.face_count == 0:
        return MeshGraph.from_faces(oid, [], [])

    lo = co.min(axis=0)
    extent = co.max(axis=0) - lo
    max_extent = float(extent.max())
    if max_extent <= 0.0:  # all vertices coincident -> nothing to cluster
        return MeshGraph.from_faces(oid, [tuple(co[0])], [])
    cell = max_extent / grid

    # Cell index per vertex; vertices on the far boundary land in cell ``grid``.
    cells = np.clip(np.floor((co - lo) / cell).astype(np.int64), 0, grid)
    keys = [tuple(int(c) for c in row) for row in cells]

    members: dict[tuple[int, int, int], list[int]] = {}
    for vid, key in enumerate(keys):
        members.setdefault(key, []).append(vid)

    # Deterministic, position-independent ordering of the surviving vertices.
    sorted_keys = sorted(members)
    cluster_of_key = {key: i for i, key in enumerate(sorted_keys)}
    new_vertices = [tuple(co[members[key]].mean(axis=0)) for key in sorted_keys]
    vertex_cluster = np.fromiter((cluster_of_key[k] for k in keys), dtype=np.int64, count=len(keys))

    return _rebuild_from_clusters(mesh, vertex_cluster, new_vertices, oid)


def feature_aware_decimate(
    mesh: MeshGraph,
    grid: int,
    feature_mask,
    *,
    normal_buckets: int = 2,
    object_id: str | None = None,
) -> MeshGraph:
    """Like :func:`cluster_decimate`, but vertices flagged in ``feature_mask`` are
    kept *exactly* (each its own cluster) while flat vertices collapse onto the
    grid (plan §10 Phase 5: "maintain density in feature regions, reduce it in
    flat areas"). Preserving feature vertices keeps hard edges and silhouettes
    crisp -- both endpoints of a hard edge survive, so the crease survives.

    Flat vertices are clustered by grid cell *and* by a quantized surface normal
    (``normal_buckets`` levels per axis), so vertices on differently-facing
    surfaces -- e.g. opposite/perpendicular sides of a thin shell -- never average
    together into an off-surface point.

    ``feature_mask`` is a length-``vertex_count`` boolean sequence (e.g. from
    :func:`retopo_agent.geometry.features.feature_vertex_mask`).
    """
    grid = max(1, int(grid))
    co = _coords(mesh)
    oid = object_id or f"{mesh.object_id}_LOW"
    if len(co) == 0 or mesh.face_count == 0:
        return MeshGraph.from_faces(oid, [], [])

    lo = co.min(axis=0)
    extent = co.max(axis=0) - lo
    max_extent = float(extent.max())
    if max_extent <= 0.0:
        return MeshGraph.from_faces(oid, [tuple(co[0])], [])
    cell = max_extent / grid
    cells = np.clip(np.floor((co - lo) / cell).astype(np.int64), 0, grid)
    mask = np.asarray(feature_mask, dtype=bool)
    vnq = _quantized_vertex_normals(mesh, normal_buckets)

    # Feature vertices get a unique key (kept exactly); flat ones share a grid
    # cell *of the same orientation*.
    keys: list[tuple] = []
    for vid in range(len(co)):
        if mask[vid]:
            keys.append(("F", vid, 0, 0, 0, 0, 0))
        else:
            c = cells[vid]
            n = vnq[vid]
            keys.append(("C", int(c[0]), int(c[1]), int(c[2]), int(n[0]), int(n[1]), int(n[2])))

    members: dict[tuple, list[int]] = {}
    for vid, key in enumerate(keys):
        members.setdefault(key, []).append(vid)

    sorted_keys = sorted(members)
    cluster_of_key = {key: i for i, key in enumerate(sorted_keys)}
    new_vertices = [tuple(co[members[key]].mean(axis=0)) for key in sorted_keys]
    vertex_cluster = np.fromiter((cluster_of_key[k] for k in keys), dtype=np.int64, count=len(keys))

    return _rebuild_from_clusters(mesh, vertex_cluster, new_vertices, oid)


def _quantized_vertex_normals(mesh: MeshGraph, buckets: int) -> np.ndarray:
    """Per-vertex normal (area-weighted sum of incident face normals), quantized
    to an integer lattice so near-coplanar vertices share a key."""
    acc = np.zeros((mesh.vertex_count, 3), dtype=float)
    for face in mesh.faces:
        n = np.asarray(face.normal, dtype=float) * max(face.area_3d, 1e-9)
        for vid in face.vertex_ids:
            acc[vid] += n
    norms = np.linalg.norm(acc, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return np.round(acc / norms * max(1, int(buckets))).astype(np.int64)


def _rebuild_from_clusters(mesh: MeshGraph, vertex_cluster: np.ndarray, new_vertices: list, oid: str) -> MeshGraph:
    """Remap faces onto the clustered vertices, dropping degenerate and duplicate
    faces. Shared by :func:`cluster_decimate` and :func:`feature_aware_decimate`."""
    new_faces: list[list[int]] = []
    new_materials: list[int] = []
    seen: set[tuple[int, ...]] = set()
    for face in mesh.faces:
        mapped = [int(vertex_cluster[vid]) for vid in face.vertex_ids]
        # Drop vertices that collapsed onto their cyclic neighbour.
        loop: list[int] = []
        for c in mapped:
            if not loop or loop[-1] != c:
                loop.append(c)
        if len(loop) >= 2 and loop[0] == loop[-1]:
            loop.pop()
        if len(set(loop)) < 3:  # degenerate (collapsed to an edge or point)
            continue
        key = _canonical_face(loop)
        if key in seen:  # fold duplicate faces created by the collapse
            continue
        seen.add(key)
        new_faces.append(loop)
        new_materials.append(face.material_index)

    return MeshGraph.from_faces(oid, new_vertices, new_faces, material_indices=new_materials)


@dataclass
class RetopoResult:
    """Outcome of a Phase 1 low-poly generation pass."""

    low_mesh: MeshGraph
    method: str
    source_face_count: int
    target_face_count: int
    grid: int
    retry_count: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def actual_face_count(self) -> int:
        return self.low_mesh.face_count

    @property
    def target_error_ratio(self) -> float:
        if self.target_face_count <= 0:
            return 0.0
        return abs(self.actual_face_count - self.target_face_count) / self.target_face_count

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "source_face_count": self.source_face_count,
            "target_face_count": self.target_face_count,
            "actual_face_count": self.actual_face_count,
            "target_error_ratio": round(self.target_error_ratio, 4),
            "grid": self.grid,
            "retry_count": self.retry_count,
            "source_vertex_count": self.extra.get("source_vertex_count"),
            "actual_vertex_count": self.low_mesh.vertex_count,
            "bounding_box_diagonal": self.extra.get("bounding_box_diagonal"),
        }


def decimate_to_target(
    mesh: MeshGraph,
    target_face_count: int,
    *,
    max_grid: int = 512,
    max_iter: int = 24,
    object_id: str | None = None,
) -> RetopoResult:
    """Search for the cluster grid whose output face count is closest to
    ``target_face_count`` (plan §10 Phase 1 / §15.5 remesh-ratio adjustment).

    Because ``cluster_decimate``'s face count rises monotonically with the grid,
    a binary search converges quickly. If even ``max_grid`` cannot reach the
    target (the mesh is already coarser than requested), the finest available
    result is returned -- the closest we can get.
    """
    source_faces = mesh.face_count
    common = {
        "source_vertex_count": mesh.vertex_count,
        "bounding_box_diagonal": round(bounding_box_diagonal(mesh), 6),
    }

    # Target at or above the source: we cannot synthesise detail, so just hand
    # back a faithful (re-keyed) copy of the input.
    if target_face_count >= source_faces:
        return RetopoResult(_passthrough(mesh, object_id), "passthrough", source_faces,
                            target_face_count, max_grid, extra=common)

    low, grid = _search_grid_for_target(
        lambda g: cluster_decimate(mesh, g, object_id=object_id),
        target_face_count, max_grid=max_grid, max_iter=max_iter,
    )
    return RetopoResult(low, "cluster_decimate", source_faces, target_face_count, grid, extra=common)


def feature_aware_decimate_to_target(
    mesh: MeshGraph,
    target_face_count: int,
    feature_mask,
    *,
    max_grid: int = 512,
    max_iter: int = 24,
    object_id: str | None = None,
) -> RetopoResult:
    """Feature-aware counterpart of :func:`decimate_to_target` (plan §10 Phase 5).

    Searches the *flat-region* grid for the closest face count while keeping all
    feature vertices. The preserved features form a floor on the face count, so
    if even ``grid=1`` exceeds the target the coarsest result is returned.
    """
    source_faces = mesh.face_count
    common = {
        "source_vertex_count": mesh.vertex_count,
        "bounding_box_diagonal": round(bounding_box_diagonal(mesh), 6),
    }
    if target_face_count >= source_faces:
        return RetopoResult(_passthrough(mesh, object_id), "passthrough", source_faces,
                            target_face_count, max_grid, extra=common)

    low, grid = _search_grid_for_target(
        lambda g: feature_aware_decimate(mesh, g, feature_mask, object_id=object_id),
        target_face_count, max_grid=max_grid, max_iter=max_iter,
    )
    return RetopoResult(low, "feature_aware_cluster_decimate", source_faces, target_face_count, grid, extra=common)


def _passthrough(mesh: MeshGraph, object_id: str | None) -> MeshGraph:
    return MeshGraph.from_faces(
        object_id or f"{mesh.object_id}_LOW",
        [v.co for v in mesh.vertices],
        [f.vertex_ids for f in mesh.faces],
        material_indices=[f.material_index for f in mesh.faces],
    )


def _search_grid_for_target(make_fn, target_face_count: int, *, max_grid: int, max_iter: int):
    """Binary-search the cluster grid for the face count closest to the target.

    ``make_fn(grid) -> MeshGraph``. Face count rises monotonically with grid, so
    a bisection converges; returns ``(best_mesh, best_grid)``.
    """
    cache: dict[int, MeshGraph] = {}

    def at(grid: int) -> MeshGraph:
        if grid not in cache:
            cache[grid] = make_fn(grid)
        return cache[grid]

    finest = at(max_grid)
    if finest.face_count <= target_face_count:
        return finest, max_grid

    lo, hi = 1, max_grid
    best_grid = max_grid
    best_diff = abs(finest.face_count - target_face_count)
    for _ in range(max_iter):
        if lo > hi:
            break
        mid = (lo + hi) // 2
        faces = at(mid).face_count
        diff = abs(faces - target_face_count)
        if diff < best_diff or (diff == best_diff and mid < best_grid):
            best_diff, best_grid = diff, mid
        if faces < target_face_count:
            lo = mid + 1
        elif faces > target_face_count:
            hi = mid - 1
        else:
            best_grid = mid
            break

    return at(best_grid), best_grid
