"""Organic-mesh seam generation (UV repair plan §3 Track 1).

The default ``is_seam_edge`` strategy cuts at every edge whose dihedral exceeds a
threshold. On a faceted low-poly *organic* mesh a large fraction of ALL edges clear
that threshold, so the flood fill shatters the surface into hundreds of confetti
islands (the 551-island / 0.99-small-island failure, plan §1/§2). That dihedral=seam
rule is a hard-surface assumption.

For organic meshes seams must instead be **few, long, deliberately-placed paths**:
open each closed shell into a topological disk with a *cut tree* (cutting a closed
genus-0 surface along a connected tree of edges yields a single disk — a "pelt"),
routing the tree through creases / concave valleys / hidden back-faces so the seam is
unobtrusive, and anchoring its leaves at the surface's extremities (trident tines,
arms, legs) so those protrusions unfold instead of collapsing.

This module is pure (no Blender): it consumes a :class:`MeshGraph` and returns the
seam edge id set. The Blender side (:mod:`uv_agent.blender.organic_unwrap`) marks
those seams and runs a real angle-based unwrap. Everything here is unit-tested
offline on fixture meshes.
"""

from __future__ import annotations

import heapq
import math
from collections import deque

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph

# A mesh is "organic" when this fraction of its edges exceeds the dihedral threshold
# — i.e. there is no sparse set of creases to cut along, so the hard-surface rule
# would shatter it (plan §3 strategy selection).
DEFAULT_ORGANIC_FRACTION = 0.25


def edge_over_threshold_fraction(mesh: MeshGraph, angle_threshold: float) -> float:
    """Fraction of interior (2-face) edges whose dihedral ≥ ``angle_threshold``."""
    interior = [e for e in mesh.edges if len(e.face_ids) == 2]
    if not interior:
        return 0.0
    over = sum(1 for e in interior if e.dihedral_angle >= angle_threshold)
    return over / len(interior)


def classify_seam_strategy(
    mesh: MeshGraph,
    *,
    angle_threshold: float = 30.0,
    organic_fraction: float = DEFAULT_ORGANIC_FRACTION,
) -> str:
    """``"organic"`` if a large fraction of edges clear the dihedral threshold (no
    sparse crease set to cut along), else ``"hard_surface"`` (plan §3)."""
    return "organic" if edge_over_threshold_fraction(mesh, angle_threshold) >= organic_fraction else "hard_surface"


def _vertex_edges(mesh: MeshGraph) -> dict[int, list[tuple[int, int]]]:
    """vertex_id -> list of (other_vertex_id, edge_id)."""
    adj: dict[int, list[tuple[int, int]]] = {v.id: [] for v in mesh.vertices}
    for e in mesh.edges:
        a, b = e.vertex_ids
        adj[a].append((b, e.id))
        adj[b].append((a, e.id))
    return adj


def _components(mesh: MeshGraph, adj: dict[int, list[tuple[int, int]]]) -> list[list[int]]:
    """Connected vertex components over the edge graph."""
    seen: set[int] = set()
    comps: list[list[int]] = []
    for v in mesh.vertices:
        if v.id in seen:
            continue
        comp: list[int] = []
        q = deque([v.id])
        seen.add(v.id)
        while q:
            cur = q.popleft()
            comp.append(cur)
            for nxt, _eid in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        comps.append(comp)
    return comps


def _seam_edge_cost(mesh: MeshGraph, edge_id: int, view: np.ndarray) -> float:
    """Dijkstra edge cost: short, crease-following, back-facing edges are CHEAP so the
    cut tree prefers them (hidden, natural seams). High-dihedral creases divide the
    length down; front-facing (camera-visible) edges are penalised up."""
    e = mesh.edges[edge_id]
    a = mesh.vertex_co(e.vertex_ids[0])
    b = mesh.vertex_co(e.vertex_ids[1])
    length = float(np.linalg.norm(b - a)) or 1e-9

    # Crease bonus: a sharp dihedral is a good place to hide a seam.
    crease = 1.0 + min(e.dihedral_angle, 90.0) / 30.0  # 1..4
    cost = length / crease

    # Visibility penalty: prefer edges whose faces look AWAY from the camera.
    facing = 0.0
    for fid in e.face_ids:
        n = np.asarray(mesh.faces[fid].normal, dtype=float)
        facing = max(facing, float(np.dot(n, -view)))  # >0 => faces the camera
    if facing > 0:
        cost *= 1.0 + 2.0 * facing  # up to 3x on a fully front-facing edge
    return cost


def _dijkstra(
    source: int,
    targets: set[int],
    adj: dict[int, list[tuple[int, int]]],
    edge_cost: dict[int, float],
) -> dict[int, tuple[int, int]]:
    """Single-source Dijkstra returning ``prev[v] = (prev_vertex, edge_id)`` for every
    vertex reachable until all ``targets`` are settled."""
    dist = {source: 0.0}
    prev: dict[int, tuple[int, int]] = {}
    pq: list[tuple[float, int]] = [(0.0, source)]
    remaining = set(targets)
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, math.inf):
            continue
        remaining.discard(u)
        if not remaining:
            break
        for v, eid in adj[u]:
            nd = d + edge_cost[eid]
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = (u, eid)
                heapq.heappush(pq, (nd, v))
    return prev


def _path_edges(prev: dict[int, tuple[int, int]], target: int) -> list[int]:
    """Edge ids along the Dijkstra path from the source to ``target``."""
    out: list[int] = []
    cur = target
    while cur in prev:
        pv, eid = prev[cur]
        out.append(eid)
        cur = pv
    return out


def _geodesic_extremities(
    comp: list[int],
    adj: dict[int, list[tuple[int, int]]],
    edge_cost: dict[int, float],
    *,
    k: int,
) -> tuple[list[int], int]:
    """Farthest-point sampling on the edge graph: the protrusion tips of ``comp``.

    Returns ``(extremities, root)`` where ``root`` is the most central seed (the
    first farthest-point seed's antipode — a stable interior anchor for the cut tree).
    """
    if len(comp) <= 1:
        return list(comp), (comp[0] if comp else 0)

    start = comp[0]
    # First, jump to a true extremity: farthest vertex from an arbitrary start.
    prev = _dijkstra(start, set(comp), adj, edge_cost)
    dist0 = _distances_from_prev(start, comp, adj, edge_cost)
    first = max(comp, key=lambda v: dist0.get(v, 0.0))

    selected = [first]
    # Greedy farthest-point sampling for the remaining extremities.
    dmap = _distances_from_prev(first, comp, adj, edge_cost)
    while len(selected) < k:
        nxt = max(comp, key=lambda v: dmap.get(v, 0.0))
        if dmap.get(nxt, 0.0) <= 1e-9:
            break
        selected.append(nxt)
        dn = _distances_from_prev(nxt, comp, adj, edge_cost)
        dmap = {v: min(dmap.get(v, math.inf), dn.get(v, math.inf)) for v in comp}

    # Root = the vertex minimising the max distance to the extremities (a center).
    ext_dists = [_distances_from_prev(s, comp, adj, edge_cost) for s in selected]
    root = min(comp, key=lambda v: max(d.get(v, math.inf) for d in ext_dists))
    return selected, root


def _distances_from_prev(source, comp, adj, edge_cost) -> dict[int, float]:
    """Plain Dijkstra distance map from ``source`` over the component."""
    dist = {source: 0.0}
    pq = [(0.0, source)]
    comp_set = set(comp)
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, math.inf):
            continue
        for v, eid in adj[u]:
            if v not in comp_set:
                continue
            nd = d + edge_cost[eid]
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def crease_seam_edges(mesh: MeshGraph, *, percentile: float = 88.0, min_angle: float = 40.0) -> set[int]:
    """The strongest creases — interior edges whose dihedral is both above the
    ``percentile`` of all dihedrals AND ≥ ``min_angle``. Cutting along these relieves
    the surface curvature that makes a single pelt high-stretch, without shattering at
    every minor facet (the confetti failure). The cut-tree opens topology; these add
    the *relief* charts (plan §3: seams follow creases)."""
    interior = [e for e in mesh.edges if len(e.face_ids) == 2]
    if not interior:
        return set()
    angles = np.array([e.dihedral_angle for e in interior])
    cut = max(min_angle, float(np.percentile(angles, percentile)))
    return {e.id for e in interior if e.dihedral_angle >= cut}


def organic_seam_edges(
    mesh: MeshGraph,
    *,
    n_extremities: int = 6,
    view_dir: tuple[float, float, float] = (0.0, -1.0, 0.0),
    crease_percentile: float | None = None,
) -> set[int]:
    """Cut-tree seams that open every shell into a disk (plan §3 Track 1, steps 1–2).

    Per connected component: locate up to ``n_extremities`` protrusion tips by
    geodesic farthest-point sampling, then route a least-cost path (crease-following,
    back-face-preferring — :func:`_seam_edge_cost`) from a central root to each tip.
    The union of those paths is a connected **tree**; cutting a closed genus-0 surface
    along a tree yields a single topological disk, so an angle-based unwrap of the
    result is a low-distortion pelt with few, hidden seams — the reference-style layout.
    """
    adj = _vertex_edges(mesh)
    view = np.asarray(view_dir, dtype=float)
    view /= float(np.linalg.norm(view)) or 1.0
    edge_cost = {e.id: _seam_edge_cost(mesh, e.id, view) for e in mesh.edges}

    # Already-boundary / non-manifold edges are seams for free.
    seams: set[int] = {e.id for e in mesh.edges if e.is_boundary or e.is_non_manifold}

    for comp in _components(mesh, adj):
        if len(comp) < 4:
            continue
        extremities, root = _geodesic_extremities(comp, adj, edge_cost, k=n_extremities)
        targets = {v for v in extremities if v != root}
        if not targets:
            continue
        prev = _dijkstra(root, targets, adj, edge_cost)
        for t in targets:
            seams.update(_path_edges(prev, t))

    if crease_percentile is not None:
        seams |= crease_seam_edges(mesh, percentile=crease_percentile)
    return seams


def merge_small_islands(
    mesh: MeshGraph,
    seams: set[int],
    *,
    min_island_faces: int = 40,
    max_islands: int = 30,
    protected: set[int] | None = None,
) -> set[int]:
    """Dissolve tiny charts back into a neighbour to satisfy the island_count /
    small_island_ratio gates (plan §5) after crease-cutting over-segments the mesh.

    Repeatedly takes the smallest island under ``min_island_faces`` and removes the
    seam edges it shares with its largest neighbour (merging the two). ``protected``
    edges (genuine boundaries / non-manifold) are never removed, so topology stays
    valid. Stops when no island is too small and the count is within ``max_islands``."""
    protected = protected or {e.id for e in mesh.edges if e.is_boundary or e.is_non_manifold}
    seams = set(seams)
    adjacency = mesh.face_adjacency()

    for _ in range(len(mesh.faces)):  # generous upper bound; breaks out early
        islands = _flood_islands(mesh, seams, adjacency)
        if len(islands) <= 1:
            break
        small = [isl for isl in islands if len(isl) < min_island_faces]
        if not small and len(islands) <= max_islands:
            break
        face_island = {f: idx for idx, isl in enumerate(islands) for f in isl}
        # Target the smallest island (or, if none are 'small', the smallest overall
        # while we still exceed max_islands).
        target = min(small or islands, key=len)
        tgt_idx = face_island[target[0]]

        # Seam edges on this island's border, grouped by the neighbour island.
        border: dict[int, list[int]] = {}
        for fid in target:
            for neighbor, eid in adjacency[fid]:
                if eid in seams and eid not in protected and face_island.get(neighbor) != tgt_idx:
                    border.setdefault(face_island.get(neighbor, -1), []).append(eid)
        border.pop(-1, None)
        if not border:
            break  # fully walled by protected edges; cannot merge
        # Merge into the neighbour sharing the most border (largest contact).
        best_neighbor = max(border, key=lambda k: len(border[k]))
        seams.difference_update(border[best_neighbor])
    return seams


def _flood_islands(mesh: MeshGraph, seams: set[int], adjacency) -> list[list[int]]:
    seen: set[int] = set()
    out: list[list[int]] = []
    for f in mesh.faces:
        if f.id in seen:
            continue
        comp: list[int] = []
        q = deque([f.id])
        seen.add(f.id)
        while q:
            cur = q.popleft()
            comp.append(cur)
            for neighbor, eid in adjacency[cur]:
                if neighbor not in seen and eid not in seams:
                    seen.add(neighbor)
                    q.append(neighbor)
        out.append(comp)
    return out


def refinement_seam_edges(
    mesh: MeshGraph,
    island_face_ids,
    global_seams: set[int],
    face_stretch,
    *,
    view_dir: tuple[float, float, float] = (0.0, -1.0, 0.0),
) -> list[int]:
    """Grow ONE new seam that splits a high-stretch island (plan §3 Track 2 step 3).

    Cuts the island along its geodesically-longest axis, routed to pass *through* the
    worst-stretch faces (so the relief lands where it is needed) and along creases /
    hidden back-faces (so the new seam stays unobtrusive). The path runs between the
    two farthest-apart island-boundary vertices over the island's INTERIOR edges, so
    its edges are new cuts (not already seams) and splitting a disk along a
    boundary-to-boundary path yields two disks. Returns the interior edge ids to add;
    empty if the island cannot be meaningfully split."""
    island = set(island_face_ids)
    if len(island) < 4:
        return []
    view = np.asarray(view_dir, dtype=float)
    view /= float(np.linalg.norm(view)) or 1.0
    fstretch = np.asarray(face_stretch, dtype=float)

    # Interior edges of the island = shared by two island faces and not already a seam.
    interior_adj: dict[int, list[tuple[int, int]]] = {}
    boundary_verts: set[int] = set()
    for e in mesh.edges:
        fids = e.face_ids
        a, b = e.vertex_ids
        in_island = [f for f in fids if f in island]
        if e.id in global_seams or len(fids) < 2 or len(in_island) == 1:
            # An edge on the island's border (a seam or a one-side-in edge).
            if in_island:
                boundary_verts.add(a)
                boundary_verts.add(b)
            continue
        if len(in_island) == 2:
            adj_stretch = float(fstretch[fids[0]] + fstretch[fids[1]]) * 0.5
            crease = 1.0 + min(e.dihedral_angle, 90.0) / 30.0
            length = float(np.linalg.norm(mesh.vertex_co(a) - mesh.vertex_co(b))) or 1e-9
            facing = 0.0
            for fid in fids:
                n = np.asarray(mesh.faces[fid].normal, dtype=float)
                facing = max(facing, float(np.dot(n, -view)))
            # Cheap through HIGH stretch (divide), along creases, away from camera.
            cost = length / (crease * (1.0 + 3.0 * adj_stretch))
            if facing > 0:
                cost *= 1.0 + 2.0 * facing
            interior_adj.setdefault(a, []).append((b, e.id))
            interior_adj.setdefault(b, []).append((a, e.id))
            _EDGE_COST_CACHE[e.id] = cost

    if len(boundary_verts) < 2 or not interior_adj:
        return []

    cost = _EDGE_COST_CACHE
    # Anchor at the boundary vertex nearest the worst-stretch island face.
    worst_fid = max(island, key=lambda f: fstretch[f])
    worst_verts = set(mesh.faces[worst_fid].vertex_ids)
    start = _nearest_reachable(worst_verts, boundary_verts, interior_adj, cost)
    if start is None:
        start = next(iter(boundary_verts))
    dmap = _distances_from_prev(start, list(interior_adj.keys()), interior_adj, cost)
    reach_bound = [v for v in boundary_verts if v in dmap and v != start]
    if not reach_bound:
        return []
    end = max(reach_bound, key=lambda v: dmap[v])
    prev = _dijkstra(start, {end}, interior_adj, cost)
    path = _path_edges(prev, end)
    _EDGE_COST_CACHE.clear()
    return path


# Scratch cache for refinement edge costs (cleared each call); avoids recomputing
# the per-edge cost twice when building the adjacency and running Dijkstra.
_EDGE_COST_CACHE: dict[int, float] = {}


def _nearest_reachable(sources, boundary_verts, adj, cost) -> int | None:
    """The boundary vertex reachable with least cost from any vertex in ``sources``."""
    pq = [(0.0, s) for s in sources if s in adj]
    if not pq:
        return None
    heapq.heapify(pq)
    seen: set[int] = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
        if u in boundary_verts:
            return u
        for v, eid in adj[u]:
            if v not in seen:
                heapq.heappush(pq, (d + cost[eid], v))
    return None
