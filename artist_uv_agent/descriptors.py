"""A2 — per-part geometry descriptors (AUTO_ARTIST_UV_PLAN §5.A2).

For each :class:`~artist_uv_agent.segmentation.Part` compute the shape descriptors that
drive classification (A3) and layout grammar (A6): area, an oriented bounding box in the
part's own principal frame, elongation, flatness, cylindricalness, "stripness", the
normal-cone half-angle, boundary-loop count, disk likelihood, an extremity score, and a
symmetry-mate candidate. Pure numpy on a :class:`~uv_agent.geometry.mesh_graph.MeshGraph`.

The descriptor vocabulary is deliberately geometric (cylinder/panel/strip/blob), not
semantic ("arm"/"head") — artist-style UV needs geometry class + grouping, not object
recognition (plan §3 / §A3).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field

import numpy as np

from artist_uv_agent.segmentation import Part, PartSegmentation
from uv_agent.geometry.mesh_graph import MeshGraph


def quiet_fp(fn):
    """Suppress numpy's harmless FP-flag warnings (a tube's area-weighted normals nearly
    cancel → a large clip-protected intermediate in ``normal_cone_halfangle``; the result
    is always correct). Real NaNs surface in the gate's overlap/bounds checks, not here."""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
            return fn(*args, **kwargs)
    return wrapped


@dataclass
class PartDescriptor:
    part_id: int
    area: float
    face_count: int
    centroid: tuple[float, float, float]
    principal_axes: list[list[float]]          # 3×3, rows = axes (descending extent)
    extents: tuple[float, float, float]        # full extent along each principal axis (descending)
    elongation: float                          # ext0 / ext1   (>1 → long)
    flatness: float                            # ext2 / ext1   (→0 → planar)
    stripness: float                           # long AND flat
    cylindricalness: float                     # normals ⟂ long axis (a tube wall)
    normal_cone_deg: float                     # spread of face normals (→0 planar, →180 closed)
    boundary_loops: int                        # open ends (1 = disk-ish, 2 = open tube)
    is_disk: bool
    extremity: float                           # 0 (core) … 1 (tip), from segmentation seed depth
    area_frac: float                           # area / total mesh area
    symmetry_mate: int = -1                    # part_id of mirror mate, or -1
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "part_id": self.part_id, "area": round(self.area, 6), "face_count": self.face_count,
            "area_frac": round(self.area_frac, 4), "elongation": round(self.elongation, 3),
            "flatness": round(self.flatness, 3), "stripness": round(self.stripness, 3),
            "cylindricalness": round(self.cylindricalness, 3),
            "normal_cone_deg": round(self.normal_cone_deg, 1),
            "boundary_loops": self.boundary_loops, "is_disk": self.is_disk,
            "extremity": round(self.extremity, 3), "symmetry_mate": self.symmetry_mate,
            "confidence": round(self.confidence, 3),
            "extents": [round(e, 4) for e in self.extents],
            "centroid": [round(c, 4) for c in self.centroid],
        }


def _part_vertex_coords(mesh: MeshGraph, face_ids) -> np.ndarray:
    vids = set()
    for f in face_ids:
        vids.update(mesh.faces[f].vertex_ids)
    return np.array([mesh.vertices[v].co for v in vids], dtype=float)


def _pca(points: np.ndarray):
    """Principal axes (rows, descending variance) + full extents along each axis."""
    c = points.mean(axis=0)
    x = points - c
    if len(points) < 3:
        axes = np.eye(3)
    else:
        cov = np.cov(x.T)
        w, v = np.linalg.eigh(cov)
        order = np.argsort(w)[::-1]
        axes = v[:, order].T            # rows = axes, descending variance
    proj = x @ axes.T
    extents = proj.max(axis=0) - proj.min(axis=0)
    # Re-sort axes strictly by extent (eigvalue order can tie on symmetric shapes).
    order = np.argsort(extents)[::-1]
    return axes[order], extents[order], c


def _boundary_loop_count(mesh: MeshGraph, face_ids) -> int:
    """Number of boundary loops of the chart formed by ``face_ids`` — edges used by
    exactly one of the part's faces, chained into loops. A topological disk has 1 loop;
    an open tube has 2; a closed shell has 0."""
    fset = set(face_ids)
    edge_use: dict[int, int] = {}
    for f in face_ids:
        for e in mesh.faces[f].edge_ids:
            edge_use[e] = edge_use.get(e, 0) + 1
    # Boundary edges of the part: used once within the part, OR a global boundary edge.
    bedges = [e for e, n in edge_use.items()
              if n == 1 or mesh.edges[e].is_boundary or mesh.edges[e].is_non_manifold]
    if not bedges:
        return 0
    # Chain boundary edges into loops via shared vertices.
    adj: dict[int, list[int]] = {}
    for e in bedges:
        a, b = mesh.edges[e].vertex_ids
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    seen: set[int] = set()
    loops = 0
    for start in list(adj):
        if start in seen:
            continue
        loops += 1
        stack = [start]
        seen.add(start)
        while stack:
            v = stack.pop()
            for nb in adj.get(v, ()):
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
    return loops


def describe_part(mesh: MeshGraph, part: Part, *, total_area: float,
                  seed_depth: dict[int, float], max_seed_depth: float) -> PartDescriptor:
    from chart_uv_agent.segmentation import is_disk, normal_cone_halfangle

    faces = part.face_ids
    pts = _part_vertex_coords(mesh, faces)
    area = float(sum(mesh.faces[f].area_3d for f in faces))
    axes, extents, centroid = _pca(pts)
    ext = np.maximum(extents, 1e-9)
    elongation = float(ext[0] / ext[1])
    flatness = float(ext[2] / ext[1])
    long_axis = axes[0]

    # Sanitise: a degenerate (zero-area) face in a real mesh can carry a non-finite
    # normal; replace it so the dot products below stay finite.
    normals = np.nan_to_num(np.array([mesh.faces[f].normal for f in faces], dtype=float))
    areas = np.array([mesh.faces[f].area_3d for f in faces])
    long_axis = np.nan_to_num(long_axis)
    ln = np.linalg.norm(long_axis)
    long_axis = long_axis / ln if ln > 1e-12 else np.array([1.0, 0.0, 0.0])
    # Cylindricalness: a tube wall's normals are perpendicular to the long axis → mean
    # |n·long_axis| ≈ 0. A flat disk's normals are parallel to one axis. Weight by area.
    along = np.abs(normals @ long_axis)
    cyl = float(1.0 - np.average(along, weights=np.maximum(areas, 1e-12)))
    cone = normal_cone_halfangle(mesh, faces)   # builds its own global-indexed normals
    loops = _boundary_loop_count(mesh, faces)
    disk = is_disk(mesh, faces)

    # Stripness: long (high elongation) AND flat (low flatness).
    stripness = float(np.clip((elongation - 1.5) / 3.0, 0.0, 1.0) * np.clip(1.0 - flatness, 0.0, 1.0))
    extremity = float(np.clip(seed_depth.get(part.seed_face, 0.0) / max(max_seed_depth, 1e-9), 0.0, 1.0))

    return PartDescriptor(
        part_id=part.part_id, area=area, face_count=len(faces),
        centroid=(float(centroid[0]), float(centroid[1]), float(centroid[2])),
        principal_axes=[list(map(float, a)) for a in axes],
        extents=(float(ext[0]), float(ext[1]), float(ext[2])),
        elongation=elongation, flatness=flatness, stripness=stripness,
        cylindricalness=cyl, normal_cone_deg=cone, boundary_loops=loops, is_disk=disk,
        extremity=extremity, area_frac=area / max(total_area, 1e-12),
        confidence=part.confidence)


def _seed_depths(mesh: MeshGraph, seg: PartSegmentation) -> tuple[dict[int, float], float]:
    """Summed neck-depth of each part's seed from the global core (the lowest-extremity
    seed) — reuses the A1 barrier geodesic so extremity is consistent with segmentation."""
    from artist_uv_agent.segmentation import _dual_graph, _face_centroids, _multi_source_dijkstra

    centroids = _face_centroids(mesh)
    adj = _dual_graph(mesh, centroids)
    seeds = [p.seed_face for p in seg.parts]
    if not seeds:
        return {}, 1.0
    dist, _ = _multi_source_dijkstra(adj, seeds, mesh.face_count)
    # core = the seed nearest all others; depth = barrier distance from that core seed.
    core = min(seeds, key=lambda s: float(np.nanmean(np.where(np.isfinite(dist), dist, 0.0))))
    d, _ = _multi_source_dijkstra(adj, [core], mesh.face_count)
    depths = {s: float(d[s]) if np.isfinite(d[s]) else 0.0 for s in seeds}
    return depths, max(depths.values(), default=1.0)


def detect_symmetry(descriptors: list[PartDescriptor], *, mesh_centroid: np.ndarray,
                    tol_frac: float = 0.18) -> None:
    """Assign ``symmetry_mate`` in place: pair parts whose centroids mirror across the
    mesh's dominant symmetry plane and whose areas match (plan §5.A2/§A6 symmetry pairs).

    Tries each world axis as the plane normal; picks the axis maximising matched pairs.
    A part with no mate keeps ``symmetry_mate = -1`` (e.g. an on-axis central part)."""
    if len(descriptors) < 2:
        return
    cents = np.array([d.centroid for d in descriptors])
    areas = np.array([d.area for d in descriptors])
    diag = float(np.linalg.norm(cents.max(axis=0) - cents.min(axis=0))) or 1.0
    tol = tol_frac * diag

    best_axis, best_pairs = None, []
    for axis in range(3):
        n = np.zeros(3)
        n[axis] = 1.0
        mirrored = cents - 2.0 * ((cents - mesh_centroid) @ n)[:, None] * n[None, :]
        used: set[int] = set()
        pairs: list[tuple[int, int]] = []
        for i in range(len(descriptors)):
            if i in used:
                continue
            best_j, best_d = -1, tol
            for j in range(len(descriptors)):
                if j == i or j in used:
                    continue
                d = float(np.linalg.norm(mirrored[i] - cents[j]))
                amatch = abs(areas[i] - areas[j]) <= 0.4 * max(areas[i], areas[j])
                # an on-axis part mirrors onto itself; skip self-pairs via distance>~0.
                if d < best_d and amatch and np.linalg.norm(mirrored[i] - cents[i]) > tol:
                    best_d, best_j = d, j
            if best_j >= 0:
                used.add(i)
                used.add(best_j)
                pairs.append((i, best_j))
        if len(pairs) > len(best_pairs):
            best_axis, best_pairs = axis, pairs

    for i, j in best_pairs:
        descriptors[i].symmetry_mate = descriptors[j].part_id
        descriptors[j].symmetry_mate = descriptors[i].part_id


@quiet_fp
def describe_parts(mesh: MeshGraph, seg: PartSegmentation) -> list[PartDescriptor]:
    """Compute every part's descriptor and resolve symmetry mates (plan §5.A2)."""
    total_area = float(sum(f.area_3d for f in mesh.faces)) or 1.0
    seed_depth, max_depth = _seed_depths(mesh, seg)
    descs = [describe_part(mesh, p, total_area=total_area,
                           seed_depth=seed_depth, max_seed_depth=max_depth)
             for p in seg.parts]
    all_pts = np.array([v.co for v in mesh.vertices], dtype=float)
    detect_symmetry(descs, mesh_centroid=all_pts.mean(axis=0))
    return descs
