"""A1 — semantic part segmentation (AUTO_ARTIST_UV_PLAN §5.A1).

Split a mesh into meaningful 3D *parts* (limbs, caps, panels, strips, blobs) BEFORE UV
charting. This is the semantic layer that ``chart_uv_agent`` lacks: it knows
developable blobs, not "head", "limb", or "cloth strip" (plan §1/§3.1).

Method — barrier-geodesic watershed (Blender-free, deterministic):

1. **Barrier field.** Each interior edge gets a non-negative ``barrier`` = how much a
   part boundary "wants" to live there. Concave necks (valleys where a limb meets a
   body) and sharp folds are strong barriers; smooth continuous surface is ~0. Crossing
   a deep neck is expensive; sliding along a smooth tube is free.

2. **Farthest-point seeds.** Multi-source Dijkstra over the face dual graph with edge
   weight ``barrier(e) + ε·len(e)``. The geodesic cost between two faces ≈ the total
   barrier you must cross to get between them, so a protrusion behind a deep neck is
   "far". Seeds are added at the farthest face until the next farthest min-cost falls
   below ``neck_threshold`` (no more genuine necks to separate) or the cap is hit. On a
   smooth surface with no necks this yields a single seed → a single part (a flat panel
   stays whole; a smooth tube stays whole), which is exactly the artist intent.

3. **Watershed labeling.** Every face is assigned to its nearest seed (the Dijkstra
   that placed the last seed already computes this), so part boundaries fall on the
   barrier ridges — the necks.

4. **Merge tiny parts.** A part below the minimum face/area is dissolved into the
   neighbour it shares the weakest (lowest mean-barrier) boundary with — over-eager
   splits from a spurious local concavity are absorbed (plan §5.A1 step 5).

The result is a stable *major-part* decomposition, not perfect object-part labels
(plan §5.A1: "stable part decomposition, not perfect labels"). Confidence scores and
debug overlays (plan §12) flag where it is unsure; low-confidence parts drive nothing
aggressive (the seam layer falls back to chart segmentation per part).
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph

# Calibration defaults (plan §5.A1; recalibrate on the fixture suite per §AR7).
DEFAULT_NECK_THRESHOLD = 0.22   # min crossed neck-depth (summed) to justify a new seed
DEFAULT_MAX_PARTS = 24          # safety cap on seed count
# Concave necks bound parts at full weight. Convex ridges (box edges, sharp creases)
# mostly become INTRA-part seams in the A4 layer (a box is one part, six charts), so they
# carry only a small fraction of a concave neck's barrier — enough to help enclose a
# convex-sided protrusion (a tent/spike) without slicing a smoothly-ridged body.
CONVEX_WEIGHT = 0.30
DIHEDRAL_FLOOR = 40.0           # ignore folds shallower than this: only genuine necks are barriers
MIN_PART_FACE_FRAC = 0.05       # relative tiny-part floor (with the absolute MIN_PART_FACES)
MIN_PART_FACES = 12
EPS_LEN = 1e-3                  # tiny length term: localises seeds to extremities, never accumulates enough to seed


@dataclass
class Part:
    """One semantic 3D part: its faces, the seed it grew from, its confidence, and its
    adjacency / boundary in the part graph (plan §5.A1 ``Part`` record)."""

    part_id: int
    face_ids: list[int]
    seed_face: int = -1
    confidence: float = 0.0
    neighbors: set[int] = field(default_factory=set)
    boundary_edges: set[int] = field(default_factory=set)

    @property
    def face_count(self) -> int:
        return len(self.face_ids)

    def to_dict(self) -> dict:
        return {"part_id": self.part_id, "face_count": self.face_count,
                "seed_face": self.seed_face, "confidence": round(self.confidence, 4),
                "neighbors": sorted(self.neighbors),
                "boundary_edge_count": len(self.boundary_edges)}


@dataclass
class PartSegmentation:
    """A face→part partition plus the part records and the build history."""

    mesh: MeshGraph
    face_part: dict[int, int]
    parts: list[Part]
    history: list[dict] = field(default_factory=list)

    @property
    def part_count(self) -> int:
        return len(self.parts)

    def part_faces(self) -> dict[int, list[int]]:
        return {p.part_id: p.face_ids for p in self.parts}

    def to_dict(self) -> dict:
        return {"part_count": self.part_count,
                "parts": [p.to_dict() for p in self.parts],
                "history": self.history}


def _face_centroids(mesh: MeshGraph) -> np.ndarray:
    cs = np.zeros((mesh.face_count, 3))
    for f in mesh.faces:
        cs[f.id] = np.mean([mesh.vertices[v].co for v in f.vertex_ids], axis=0)
    return cs


def edge_concavity(mesh: MeshGraph, edge_id: int, centroids: np.ndarray) -> float:
    """Signed concavity of an interior edge: ``> 0`` concave (a valley/neck), ``< 0``
    convex (a ridge). Symmetric in the two faces (plan §5.A1 "concave creases").

    For faces A,B with outward normals ``nA,nB`` and centroids ``cA,cB``, the surface
    folds *inward* (concave) when each face's centroid sits on the +normal side of the
    other — i.e. ``nA·(cB−cA) + nB·(cA−cB) > 0`` (normalised by the centroid gap)."""
    e = mesh.edges[edge_id]
    if len(e.face_ids) != 2:
        return 0.0
    a, b = e.face_ids
    na = np.asarray(mesh.faces[a].normal, float)
    nb = np.asarray(mesh.faces[b].normal, float)
    d = centroids[b] - centroids[a]
    n = float(np.linalg.norm(d))
    if n < 1e-12:
        return 0.0
    d = d / n
    return float(np.dot(na, d) - np.dot(nb, d)) * 0.5


def edge_barrier(mesh: MeshGraph, edge_id: int, centroids: np.ndarray) -> float:
    """Non-negative part-boundary affinity of an interior edge (plan §5.A1 step 2/4).

    Fires only at genuine necks: a fold shallower than ``DIHEDRAL_FLOOR`` contributes 0
    (mild surface curvature is NOT a part boundary — that over-segments smooth blobs).
    Above the floor a concave fold gets full weight; a convex ridge of the same dihedral
    gets ``CONVEX_WEIGHT`` (artists cut concave necks before convex ridges). ``dihedral``
    is in degrees (0 = flat, 180 = full fold)."""
    e = mesh.edges[edge_id]
    if len(e.face_ids) != 2 or e.dihedral_angle < DIHEDRAL_FLOOR:
        return 0.0
    folded = e.dihedral_angle / 180.0
    conc = edge_concavity(mesh, edge_id, centroids)
    return folded if conc >= 0 else CONVEX_WEIGHT * folded


def _dual_graph(mesh: MeshGraph, centroids: np.ndarray) -> dict[int, list[tuple[int, float, int]]]:
    """face → list of (neighbour_face, weight, edge_id); weight = barrier + ε·len."""
    adj: dict[int, list[tuple[int, float, int]]] = {f.id: [] for f in mesh.faces}
    for e in mesh.edges:
        if len(e.face_ids) != 2:
            continue
        a, b = e.face_ids
        bar = edge_barrier(mesh, e.id, centroids)
        ln = float(np.linalg.norm(centroids[a] - centroids[b]))
        w = bar + EPS_LEN * ln
        adj[a].append((b, w, e.id))
        adj[b].append((a, w, e.id))
    return adj


def _multi_source_dijkstra(adj, sources: list[int], n_faces: int):
    """Summed neck-depth distances + nearest-source labels from ``sources`` over the
    weighted dual graph: ``dist[f]`` = the total neck-barrier crossed on the cheapest
    path from a seed to ``f`` (the ``ε·len`` term is negligible vs a real neck). Because
    the barrier is 0 below ``DIHEDRAL_FLOOR``, a smooth path costs ~0 and a path behind N
    necks costs ~N·neck-depth — so a protrusion is "far" in proportion to how enclosed it
    is, and FPS localises each protrusion (a saturating bottleneck metric cannot).
    ``label[f]`` is the nearest seed (the watershed catchment)."""
    dist = np.full(n_faces, np.inf)
    label = np.full(n_faces, -1, dtype=int)
    pq: list[tuple[float, int, int]] = []
    for s in sources:
        dist[s] = 0.0
        label[s] = s
        heapq.heappush(pq, (0.0, s, s))
    while pq:
        d, f, lab = heapq.heappop(pq)
        if d > dist[f]:
            continue
        for nb, w, _eid in adj[f]:
            nd = d + w
            if nd < dist[nb]:
                dist[nb] = nd
                label[nb] = lab
                heapq.heappush(pq, (nd, nb, lab))
    return dist, label


def _farthest_point_seeds(adj, n_faces: int, *, neck_threshold: float,
                          max_parts: int) -> list[int]:
    """Add seeds at the cost-farthest face until the next farthest is within
    ``neck_threshold`` (no genuine neck left to separate) or ``max_parts`` is reached."""
    start = 0  # deterministic; the first FPS step relocates to a true extremity anyway
    seeds = [start]
    dist, _ = _multi_source_dijkstra(adj, seeds, n_faces)
    # Relocate the seed to the global farthest face so seed 0 sits at an extremity.
    finite = dist[np.isfinite(dist)]
    if finite.size:
        seeds = [int(np.argmax(np.where(np.isfinite(dist), dist, -1)))]
    while len(seeds) < max_parts:
        dist, _ = _multi_source_dijkstra(adj, seeds, n_faces)
        masked = np.where(np.isfinite(dist), dist, -1.0)
        cand = int(np.argmax(masked))
        if masked[cand] < neck_threshold:
            break
        seeds.append(cand)
    return seeds


def _part_adjacency(mesh: MeshGraph, face_part: dict[int, int]):
    """Part neighbour graph + per-pair shared edges (for merge / boundary records)."""
    neighbors: dict[int, set[int]] = {}
    boundary: dict[int, set[int]] = {}
    pair_edges: dict[tuple[int, int], list[int]] = {}
    for e in mesh.edges:
        if e.is_boundary or e.is_non_manifold or len(e.face_ids) != 2:
            continue
        a, b = e.face_ids
        pa, pb = face_part[a], face_part[b]
        if pa == pb:
            continue
        neighbors.setdefault(pa, set()).add(pb)
        neighbors.setdefault(pb, set()).add(pa)
        boundary.setdefault(pa, set()).add(e.id)
        boundary.setdefault(pb, set()).add(e.id)
        pair_edges.setdefault((min(pa, pb), max(pa, pb)), []).append(e.id)
    return neighbors, boundary, pair_edges


def _merge_weak_parts(mesh, face_part, centroids, *, min_faces: int, conf_floor: float,
                      absolute_min: int = 3):
    """Dissolve parts that are not genuine semantic regions into the neighbour they share
    the WEAKEST (lowest mean-barrier) boundary with (plan §5.A1 step 5). A part is merged
    when it is poorly neck-walled — confidence below ``conf_floor`` *and* small (< ``min_
    faces``) — or true confetti (< ``absolute_min`` faces). A small-but-strongly-walled
    protrusion (a spike, a cap, a detail) has HIGH boundary contrast and is KEPT: size
    alone never merges a part the geometry genuinely separates."""
    for _ in range(mesh.face_count + 1):
        ids = sorted(set(face_part.values()))
        if len(ids) <= 1:
            return
        faces_by: dict[int, list[int]] = {}
        for f, p in face_part.items():
            faces_by.setdefault(p, []).append(f)
        _, boundary, pair_edges = _part_adjacency(mesh, face_part)

        def weak(pid):
            n = len(faces_by[pid])
            if n < absolute_min:
                return True
            conf = _part_confidence(mesh, faces_by[pid], boundary.get(pid, set()), centroids)
            return n < min_faces and conf < conf_floor

        weaks = [pid for pid in ids if weak(pid)]
        if not weaks:
            return
        merged = False
        for pid in sorted(weaks, key=lambda p: len(faces_by[p])):
            cands = []
            for (x, y), eids in pair_edges.items():
                if x == pid or y == pid:
                    nb = y if x == pid else x
                    strength = float(np.mean([edge_barrier(mesh, e, centroids) for e in eids]))
                    cands.append((strength, -len(eids), nb))
            if not cands:
                continue
            cands.sort()
            target = cands[0][2]
            for f in faces_by[pid]:
                face_part[f] = target
            merged = True
            break
        if not merged:
            return


def _relabel_compact(face_part: dict[int, int]) -> dict[int, int]:
    remap = {old: new for new, old in enumerate(sorted(set(face_part.values())))}
    return {f: remap[p] for f, p in face_part.items()}


def _part_confidence(mesh, faces, boundary_edges, centroids) -> float:
    """How neck-walled a part is: mean boundary barrier vs the part's interior barriers.
    A part ringed by strong concave necks scores high; one carved out of smooth surface
    by the watershed tie-break scores low (and so drives nothing aggressive downstream)."""
    if not boundary_edges:
        return 0.2
    fset = set(faces)
    bvals = [edge_barrier(mesh, e, centroids) for e in boundary_edges]
    interior = [edge_barrier(mesh, e.id, centroids) for e in mesh.edges
                if len(e.face_ids) == 2 and e.face_ids[0] in fset and e.face_ids[1] in fset]
    bmean = float(np.mean(bvals)) if bvals else 0.0
    imean = float(np.mean(interior)) if interior else 0.0
    contrast = bmean - imean
    # 0.30 ≈ a clean ~55° concave neck contrast → confidence 1.0.
    return float(np.clip(contrast / 0.30, 0.0, 1.0))


def segment_parts(mesh: MeshGraph, *, neck_threshold: float = DEFAULT_NECK_THRESHOLD,
                  max_parts: int = DEFAULT_MAX_PARTS, min_part_faces: int | None = None,
                  conf_floor: float = 0.35) -> PartSegmentation:
    """Segment ``mesh`` into semantic parts (plan §5.A1). Pure / Blender-free.

    Returns a :class:`PartSegmentation` whose ``parts`` partition the faces; each part
    carries a seed, confidence, neighbour set, and boundary edges. A part below
    ``min_part_faces`` (default ``max(MIN_PART_FACES, MIN_PART_FACE_FRAC·F)``) is dissolved
    ONLY when also weakly walled (confidence < ``conf_floor``) — a small strongly-necked
    protrusion is kept (plan §5.A1 step 5)."""
    n = mesh.face_count
    if min_part_faces is None:
        min_part_faces = max(MIN_PART_FACES, round(MIN_PART_FACE_FRAC * n))
    history: list[dict] = [{"stage": "input", "faces": n}]
    if n == 0:
        return PartSegmentation(mesh, {}, [], history)
    centroids = _face_centroids(mesh)
    adj = _dual_graph(mesh, centroids)

    seeds = _farthest_point_seeds(adj, n, neck_threshold=neck_threshold, max_parts=max_parts)
    history.append({"stage": "seeds", "count": len(seeds), "seed_faces": seeds})

    _, label = _multi_source_dijkstra(adj, seeds, n)
    # Any unreachable face (separate shell with no dual edge to a seed) seeds its own part.
    face_part = {f: int(label[f]) if label[f] >= 0 else f for f in range(n)}
    face_part = _relabel_compact(face_part)
    history.append({"stage": "watershed", "parts": len(set(face_part.values()))})

    _merge_weak_parts(mesh, face_part, centroids,
                      min_faces=min_part_faces, conf_floor=conf_floor)
    face_part = _relabel_compact(face_part)
    history.append({"stage": "merge", "parts": len(set(face_part.values()))})

    parts = _build_parts(mesh, face_part, centroids, seeds)
    history.append({"stage": "final", "parts": len(parts)})
    return PartSegmentation(mesh, face_part, parts, history)


def _build_parts(mesh, face_part, centroids, seeds) -> list[Part]:
    neighbors, boundary, _ = _part_adjacency(mesh, face_part)
    faces_by: dict[int, list[int]] = {}
    for f, p in face_part.items():
        faces_by.setdefault(p, []).append(f)
    seed_set = set(seeds)
    parts: list[Part] = []
    for pid in sorted(faces_by):
        faces = sorted(faces_by[pid])
        bedges = boundary.get(pid, set())
        seed = next((f for f in faces if f in seed_set), faces[0])
        parts.append(Part(
            part_id=pid, face_ids=faces, seed_face=seed,
            confidence=_part_confidence(mesh, faces, bedges, centroids),
            neighbors=set(neighbors.get(pid, set())), boundary_edges=set(bedges)))
    return parts


# Branch-split tunables (plan §5.A1 "approximate skeleton branches"). A trident's tines are
# mutually close, so concave-neck watershed AND geodesic farthest-point both cluster them;
# an axis cross-section sweep separates them by where the prongs become disjoint loops.
BRANCH_MIN_ELONG = 3.0       # only sweep clearly tube-like parts (a blob never branches here)
BRANCH_MIN_TINE = 6          # a prong must have at least this many faces
BRANCH_MIN_COUNT = 3         # split only at a genuine multi-prong fork (e.g. a trident)


def _ccs(region, padj) -> list[list[int]]:
    """Connected components of ``region`` faces over the part-interior adjacency ``padj``."""
    rset = set(region)
    seen: set[int] = set()
    comps: list[list[int]] = []
    for f in region:
        if f in seen:
            continue
        comp = [f]
        seen.add(f)
        stack = [f]
        while stack:
            cur = stack.pop()
            for nb in padj.get(cur, ()):
                if nb in rset and nb not in seen:
                    seen.add(nb)
                    comp.append(nb)
                    stack.append(nb)
        comps.append(comp)
    return comps


def _branch_split(mesh: MeshGraph, faces, adjacency, centroids: np.ndarray,
                  *, min_tine: int, min_branches: int) -> list[list[int]]:
    """Split one tube-like part at a multi-prong fork via an AXIS cross-section sweep.

    Project face centroids on the part's long axis; sweep a cut plane in from each end and
    count the connected components of the end region. Where the end fans into ``≥
    min_branches`` components (the prongs), cut there: the prongs become separate sub-parts
    and the remainder is the shaft (which keeps the original part). Returns ``[shaft, prong,
    prong, ...]`` or ``[faces]`` if no clean fork is found (a simple/bent tube is untouched)."""
    fset = set(faces)
    pts = np.array([centroids[f] for f in faces])
    cov = np.cov((pts - pts.mean(0)).T)
    w, v = np.linalg.eigh(cov)
    axis = v[:, int(np.argmax(w))]
    t = {f: float(np.dot(centroids[f] - pts.mean(0), axis)) for f in faces}
    tmin, tmax = min(t.values()), max(t.values())
    span = max(tmax - tmin, 1e-9)
    padj = {f: [nb for nb, _e in adjacency[f] if nb in fset] for f in faces}

    # Sweep deep enough (up to 0.85) to reach the fork base, so each prong region captures
    # the WHOLE prong (not just its tip) and clears the min-tine floor.
    best_n, best_prongs = 1, None
    for end_sign in (1, -1):
        for frac in np.linspace(0.12, 0.85, 16):
            if end_sign > 0:
                region = [f for f in faces if t[f] > tmax - frac * span]
            else:
                region = [f for f in faces if t[f] < tmin + frac * span]
            prongs = [c for c in _ccs(region, padj) if len(c) >= min_tine]
            if len(prongs) > best_n:
                best_n, best_prongs = len(prongs), prongs
    if best_n < min_branches or best_prongs is None:
        return [faces]
    tine_faces = set().union(*[set(p) for p in best_prongs])
    shaft = [f for f in faces if f not in tine_faces]
    if len(shaft) < min_tine or len(_ccs(shaft, padj)) != 1:
        return [faces]                         # no coherent shaft remains → don't split
    return [shaft] + [list(p) for p in best_prongs]


def split_branched_parts(mesh: MeshGraph, seg: PartSegmentation, *,
                         min_elong: float = BRANCH_MIN_ELONG, min_tine: int = BRANCH_MIN_TINE,
                         min_branches: int = BRANCH_MIN_COUNT) -> PartSegmentation:
    """Refinement: split each tube-like part at a multi-prong fork (plan §5.A1). This is the
    shaft/tine separation the concave-neck watershed cannot do (a trident's tines have no
    concave necks). Re-derives the parts; a non-branching mesh is returned essentially
    unchanged."""
    centroids = _face_centroids(mesh)
    adjacency = mesh.face_adjacency()
    face_part = dict(seg.face_part)
    next_id = (max(face_part.values()) + 1) if face_part else 0
    n_split = 0
    for part in seg.parts:
        faces = part.face_ids
        if len(faces) < min_branches * min_tine:
            continue
        pts = _part_vertex_coords_seg(mesh, faces)
        ext = _pca_extents(pts)
        if ext[0] / max(ext[1], 1e-9) < min_elong:
            continue
        groups = _branch_split(mesh, faces, adjacency, centroids,
                               min_tine=min_tine, min_branches=min_branches)
        if len(groups) <= 1:
            continue
        for g in groups[1:]:                   # groups[0] = shaft keeps the part id
            for f in g:
                face_part[f] = next_id
            next_id += 1
        n_split += 1
    if not n_split:
        return seg
    face_part = _relabel_compact(face_part)
    seeds = [p.seed_face for p in seg.parts]
    parts = _build_parts(mesh, face_part, centroids, seeds)
    history = seg.history + [{"stage": "branch_split", "parts": len(parts), "split": n_split}]
    return PartSegmentation(mesh, face_part, parts, history)


def _part_vertex_coords_seg(mesh: MeshGraph, faces) -> np.ndarray:
    vids = set()
    for f in faces:
        vids.update(mesh.faces[f].vertex_ids)
    return np.array([mesh.vertices[v].co for v in vids], dtype=float)


def _pca_extents(points: np.ndarray) -> np.ndarray:
    c = points.mean(axis=0)
    x = points - c
    if len(points) < 3:
        return np.array([1.0, 1.0, 1.0])
    w, v = np.linalg.eigh(np.cov(x.T))
    proj = x @ v[:, np.argsort(w)[::-1]]
    ext = proj.max(axis=0) - proj.min(axis=0)
    return np.sort(ext)[::-1]


def part_seam_edges(mesh: MeshGraph, face_part: dict[int, int]) -> set[int]:
    """Every edge separating two parts (or on the mesh boundary / non-manifold) — the
    seam floor the A4 templates build on (plan §5.A4)."""
    seams: set[int] = set()
    for e in mesh.edges:
        if e.is_boundary or e.is_non_manifold:
            seams.add(e.id)
        elif len(e.face_ids) == 2 and face_part.get(e.face_ids[0]) != face_part.get(e.face_ids[1]):
            seams.add(e.id)
    return seams
