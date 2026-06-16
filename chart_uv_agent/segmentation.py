"""Phase U1 — chart segmentation (chart-UV plan §5, the core novelty).

Decompose a mesh into few near-developable charts whose boundaries fall on natural
lines, encoding the two user control laws verbatim:

    (R2) every edge bending ≥ 90° (dihedral) is ALWAYS a seam — unconditional.
    (R1) minimise the island count; split a chart ONLY when its distortion exceeds
         the bar, and only the worst offender.

Pure Python / numpy on a :class:`MeshGraph` — no Blender. The "distortion" used to
drive R1 here is a Blender-free proxy: a chart's **normal-cone half-angle** (the max
angle of any face normal from the chart's mean normal). A near-planar or cylindrical
chart has a small cone and unwraps with low area-stretch; splitting until every chart
is under a cone limit approximates the stretch bar. The Blender pipeline (U2–U4) does
the final real-stretch refinement on top of this segmentation.

A 2-way split clusters a chart's faces by normal (VSA-style: two farthest-normal
seeds, assign by normal proximity), then the inter-group interior edges become the
new seam — which, on a disk, splits it into two disks.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph

FOLD_ANGLE = 90.0     # R2: unconditional seam at/above this dihedral
DEFAULT_CONE_LIMIT = 50.0  # developability proxy for the stretch bar (degrees)
DEFAULT_MAX_CHARTS = 60


@dataclass
class ChartSegmentation:
    """A face→chart partition plus the seam edge set that realises it."""

    mesh: MeshGraph
    face_chart: dict[int, int]
    seams: set[int]
    history: list[dict] = field(default_factory=list)

    @property
    def charts(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for fid, cid in self.face_chart.items():
            out.setdefault(cid, []).append(fid)
        return out

    @property
    def chart_count(self) -> int:
        return len(set(self.face_chart.values()))

    def to_dict(self) -> dict:
        return {"chart_count": self.chart_count, "seam_count": len(self.seams),
                "history": self.history}


def mandatory_seam_edges(mesh: MeshGraph, *, fold_angle: float = FOLD_ANGLE) -> set[int]:
    """R2 + topology: every ≥ ``fold_angle`` fold, plus boundary / non-manifold edges
    (seams by definition). These are never crossed by a chart and never re-routed away."""
    return {
        e.id for e in mesh.edges
        if e.is_boundary or e.is_non_manifold
        or (len(e.face_ids) == 2 and e.dihedral_angle >= fold_angle)
    }


def mandatory_seam_audit(mesh: MeshGraph, seams: set[int], *,
                         fold_angle: float = FOLD_ANGLE) -> dict:
    """R2 audit (MINIMAL_DISTORTION_UV_PLAN §M2): prove every ≥ ``fold_angle`` model fold
    (plus boundary/non-manifold edges) is present in ``seams``. The hard gate is
    ``mandatory_90_missing == 0`` — a 90°+ crease that is NOT a seam would smear the
    checker across a hard edge, which the user forbids. Returns the required/missing
    counts and the offending edge ids (pure; safe to call on the final seam set)."""
    required = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    missing = sorted(required - set(seams))
    return {"mandatory_90_edges": len(required),
            "mandatory_90_missing": len(missing),
            "missing_edge_ids": missing}


def interior_fold_edges(mesh: MeshGraph, seams: set[int], *,
                        fold_angle: float = FOLD_ANGLE) -> list[int]:
    """Mandatory fold edges (≥ ``fold_angle``, 2-face) whose two faces land in the SAME
    flood-chart — an interior 'slit' seam. Blender welds the UV across such a non-separating
    seam, smearing the checker over a hard crease, so it must be turned into a real chart
    boundary (MINIMAL_DISTORTION_UV_PLAN §M2). These arise when a lone crease doesn't reach
    the chart boundary, or when a merge/absorb buried a fold inside a chart."""
    charts = flood_charts(mesh, seams)
    face_chart = {f: cid for cid, fs in enumerate(charts) for f in fs}
    out: list[int] = []
    for e in mesh.edges:
        if len(e.face_ids) != 2 or e.dihedral_angle < fold_angle:
            continue
        a, b = e.face_ids
        if face_chart.get(a) == face_chart.get(b):
            out.append(e.id)
    return out


def enforce_fold_boundaries(mesh: MeshGraph, seams: set[int], *, fold_angle: float = FOLD_ANGLE,
                            normals: np.ndarray | None = None) -> int:
    """Split any chart that contains an interior mandatory fold edge until EVERY ≥
    ``fold_angle`` fold is a chart BOUNDARY (so the unwrap actually cuts the UV there).
    Rule 2 is unconditional — like disk-ification it runs to completion regardless of the
    chart cap (the count may rise and is reported). The VSA 2-way ``split_chart`` separates
    the two sides of a fold (their normals differ by ≥ ``fold_angle``), so the fold lands on
    the new cut. Returns the number of splits performed."""
    if normals is None:
        normals = _face_normals(mesh)
    from collections import Counter

    splits = 0
    for _ in range(mesh.face_count + 1):
        interior = interior_fold_edges(mesh, seams, fold_angle=fold_angle)
        if not interior:
            break
        charts = flood_charts(mesh, seams)
        face_chart = {f: cid for cid, fs in enumerate(charts) for f in fs}
        # Split the chart holding the most interior folds first (clears them fastest).
        worst_cid = Counter(face_chart[mesh.edges[e].face_ids[0]] for e in interior).most_common(1)[0][0]
        _, _, new_seams = split_chart(mesh, charts[worst_cid], seams, normals)
        if not new_seams:
            break  # degenerate chart that cannot be split — surfaced by the UV audit/gate
        seams.update(new_seams)
        splits += 1
    return splits


def edge_cut_cost(mesh: MeshGraph, edge_id: int, *, forbidden=frozenset(),
                  fold_angle: float = FOLD_ANGLE, region_policy=None) -> float:
    """Cost of routing a UV cut through ``edge_id`` (MINIMAL_DISTORTION_UV_PLAN follow-up).
    A cut should run along sharp creases and AVOID flat connective edges, so it never
    introduces a seam on a low-angle edge the user wants preserved:

    - ``forbidden`` (user preserve set) → ``inf`` (never traversed),
    - ≥ ``fold_angle`` mandatory fold → ~0 (free; prefer routing along real creases),
    - otherwise cost grows quadratically as the dihedral flattens, so a 15° edge is far
      more expensive than an 85° one.

    ``region_policy`` (REGION_AWARE_FACE_UV_RECOVERY_PLAN §6.2): when given, the SUB-fold cost
    is multiplied by the edge's region multiplier — a face_front_core smooth edge becomes very
    expensive (the cut reroutes around the face) while a head_back/neck edge becomes cheap (the
    cut is invited there). A ≥``fold_angle`` mandatory edge is NEVER multiplied (region cost can
    never beat mandatory, §6.2). Used by the repair/reroute cut path (§6.4).

    Note: ``dihedral_angle`` is unsigned, so concave/convex are treated alike (a sharper
    fold is always cheaper to cut); a future signed-dihedral pass could prefer concave."""
    if edge_id in forbidden:
        return float("inf")
    d = mesh.edges[edge_id].dihedral_angle
    if d >= fold_angle:
        return 0.05
    cost = 1.0 + (fold_angle - d) ** 2 / 100.0
    if region_policy is not None:
        cost *= region_policy.edge_cost_multiplier(edge_id)
    return cost


def _vertex_edges(mesh: MeshGraph) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for e in mesh.edges:
        for v in e.vertex_ids:
            out.setdefault(v, []).append(e.id)
    return out


def _chart_boundary_vertices(mesh: MeshGraph, chart_faces: set[int], seams: set[int]) -> set[int]:
    """Vertices on the OUTER boundary of a chart — endpoints of edges with exactly one
    incident face in the chart (a real chart border, necessarily a seam) or a mesh-boundary
    edge of the chart. Interior 'slit' seams (both faces in the chart) are NOT boundary, so a
    fold endpoint buried inside the chart is correctly seen as interior (needs extending)."""
    bverts: set[int] = set()
    seen: set[int] = set()
    for f in chart_faces:
        for eid in mesh.faces[f].edge_ids:
            if eid in seen:
                continue
            seen.add(eid)
            e = mesh.edges[eid]
            inside = sum(1 for fc in e.face_ids if fc in chart_faces)
            if e.is_boundary or (len(e.face_ids) == 2 and inside == 1):
                bverts.update(e.vertex_ids)
    return bverts


def _cut_path_to_boundary(mesh, start, bverts, chart_faces, seams, forbidden, fold_angle,
                          vert_edges, region_policy=None):
    """Min-cost path of CHART-INTERIOR edges from ``start`` to any chart-boundary vertex
    (Dijkstra over :func:`edge_cut_cost`). Returns the path edge ids (empty if ``start`` is
    already on the boundary) or ``None`` if no path exists (closed chart)."""
    import heapq
    if start in bverts:
        return []
    inf = float("inf")
    dist = {start: 0.0}
    prev_edge: dict[int, int] = {}
    pq = [(0.0, start)]
    while pq:
        d, v = heapq.heappop(pq)
        if d > dist.get(v, inf):
            continue
        if v != start and v in bverts:
            path = []
            cur = v
            while cur in prev_edge:
                eid = prev_edge[cur]
                path.append(eid)
                a, b = mesh.edges[eid].vertex_ids
                cur = a if b == cur else b
            return path
        for eid in vert_edges.get(v, ()):
            e = mesh.edges[eid]
            if eid in seams or len(e.face_ids) != 2:
                continue
            if e.face_ids[0] not in chart_faces or e.face_ids[1] not in chart_faces:
                continue
            cost = edge_cut_cost(mesh, eid, forbidden=forbidden, fold_angle=fold_angle,
                                 region_policy=region_policy)
            if cost == inf:
                continue
            a, b = e.vertex_ids
            nb = a if b == v else b
            nd = d + cost
            if nd < dist.get(nb, inf):
                dist[nb] = nd
                prev_edge[nb] = eid
                heapq.heappush(pq, (nd, nb))
    return None


def split_welded_folds(mesh: MeshGraph, seams: set[int], welded_edge_ids,
                       normals: np.ndarray | None = None, *, forbidden=frozenset(),
                       fold_angle: float = FOLD_ANGLE, region_policy=None) -> dict:
    """Make each fold that welded in the actual UV a real chart boundary, with a LOCAL,
    minimum-cost cut (MINIMAL_DISTORTION_UV_PLAN follow-up). Replaces the old chart-wide VSA
    split, which drew a broad new boundary that could slice through low-angle connective
    edges (e.g. cutting a 15° edge next to one 90° fold).

    For each welded fold E (its two faces in one chart), extend a cut from each interior
    endpoint of E to the chart boundary along the cheapest path (``edge_cut_cost`` favours
    creases, forbids the preserve set, penalises flat edges). E plus the two extensions
    separates the disk, so the next unwrap UV-cuts E while touching as few low-angle edges as
    possible. If the local cut fails to separate (rare; non-disk/closed chart) it falls back
    to the old VSA chart split. Returns ``{"added": set, "local_cuts": int, "fallback": int}``
    — ``added`` are the auxiliary (non-mandatory) seam edges, recorded for later pruning."""
    if normals is None:
        normals = _face_normals(mesh)
    forbidden = set(forbidden)
    vert_edges = _vertex_edges(mesh)
    added: set[int] = set()
    local = 0
    fallback = 0

    def chart_of(face):
        charts = flood_charts(mesh, seams)
        return charts, {f: i for i, fs in enumerate(charts) for f in fs}

    for eid in welded_edge_ids:
        e = mesh.edges[eid]
        if len(e.face_ids) != 2:
            continue
        fa, fb = e.face_ids
        charts, fc = chart_of(fa)
        if fc.get(fa) != fc.get(fb):
            continue  # already separated by an earlier fold's cut
        chart_faces = set(charts[fc[fa]])
        bverts = _chart_boundary_vertices(mesh, chart_faces, seams)
        path_added: set[int] = set()
        reached = True
        for vx in e.vertex_ids:
            path = _cut_path_to_boundary(mesh, vx, bverts, chart_faces, seams,
                                         forbidden, fold_angle, vert_edges, region_policy)
            if path is None:
                reached = False
                break
            for pe in path:
                if pe not in seams:
                    seams.add(pe)
                    path_added.add(pe)
        # Verify the local cut actually separated E's two faces.
        _, fc2 = chart_of(fa)
        if reached and fc2.get(fa) != fc2.get(fb):
            added.update(path_added)
            local += 1
            continue
        # Fallback: undo the partial local cut and VSA-split the whole chart (last resort).
        seams.difference_update(path_added)
        _, _, ns = split_chart(mesh, charts[fc[fa]], seams, normals)
        ns = [s for s in ns if s not in forbidden]
        if ns:
            seams.update(ns)
            added.update(ns)
            fallback += 1
    return {"added": added, "local_cuts": local, "fallback": fallback}


def flood_charts(mesh: MeshGraph, seams: set[int]) -> list[list[int]]:
    """Connected face groups that never cross a seam edge — the minimal chart set
    consistent with the current seams (R1 starts here)."""
    adjacency = mesh.face_adjacency()
    seen: set[int] = set()
    charts: list[list[int]] = []
    for f in mesh.faces:
        if f.id in seen:
            continue
        comp: list[int] = []
        q = deque([f.id])
        seen.add(f.id)
        while q:
            cur = q.popleft()
            comp.append(cur)
            for nb, eid in adjacency[cur]:
                if nb not in seen and eid not in seams:
                    seen.add(nb)
                    q.append(nb)
        charts.append(comp)
    return charts


def _face_normals(mesh: MeshGraph) -> np.ndarray:
    return np.array([f.normal for f in mesh.faces], dtype=float)


def normal_cone_halfangle(mesh: MeshGraph, face_ids, normals: np.ndarray | None = None) -> float:
    """Half-angle (deg) of the chart's normal cone: the largest angle between any face
    normal and the area-weighted mean normal. ~0 = planar, ~90 = a full bend, ~180 =
    a closed shell. The Blender-free developability/stretch proxy (chart-UV plan §5)."""
    if normals is None:
        normals = _face_normals(mesh)
    if not face_ids:
        return 0.0
    ns = normals[list(face_ids)]
    areas = np.array([mesh.faces[f].area_3d for f in face_ids])[:, None]
    mean = (ns * areas).sum(axis=0)
    n = np.linalg.norm(mean)
    if n < 1e-12:
        return 180.0  # opposing normals cancel -> a closed/folded shell
    mean = mean / n
    dots = np.clip(ns @ mean, -1.0, 1.0)
    return float(np.degrees(np.arccos(dots.min())))


def euler_characteristic(mesh: MeshGraph, face_ids) -> int:
    """V − E + F over just the chart's faces. A topological **disk** has χ = 1; a
    closed shell χ = 2; an annulus/handle χ = 0. The disk invariant the unwrap needs
    (a non-disk chart flips/overlaps in ABF — chart-UV plan §5.2 / U1.4)."""
    verts: set[int] = set()
    edges: set[int] = set()
    for f in face_ids:
        face = mesh.faces[f]
        verts.update(face.vertex_ids)
        edges.update(face.edge_ids)
    return len(verts) - len(edges) + len(face_ids)


def is_disk(mesh: MeshGraph, face_ids) -> bool:
    return len(face_ids) > 0 and euler_characteristic(mesh, face_ids) == 1


def _interior_edges(mesh: MeshGraph, face_set: set[int], seams: set[int]):
    """Edges shared by two faces of the chart and not already a seam (the candidate
    split / merge boundary)."""
    out = []
    for e in mesh.edges:
        if e.id in seams or len(e.face_ids) != 2:
            continue
        a, b = e.face_ids
        if a in face_set and b in face_set:
            out.append(e.id)
    return out


def _farthest_normal_seeds(faces, normals) -> tuple[int, int]:
    """Two opposed-normal seed faces in O(n·k), NOT the O(n²) gram matrix (would be
    ~260 MB on a 5,700-face chart, dangerous at 10k). Pick the face farthest in normal
    from ``faces[0]``, then the face farthest from that — a stable 2-point farthest pair."""
    n0 = normals[faces[0]]
    a = min(faces, key=lambda f: float(np.dot(normals[f], n0)))
    b = min(faces, key=lambda f: float(np.dot(normals[f], normals[a])))
    return a, b


def split_chart(mesh: MeshGraph, face_ids, seams: set[int],
                normals: np.ndarray | None = None) -> tuple[list[int], list[int], list[int]]:
    """VSA-style 2-way split of a chart by normal (chart-UV plan §5.3).

    Two farthest-normal seeds are region-grown by a priority queue over chart-interior
    adjacency: a face is labelled only when popped, and it was pushed by an already-
    labelled neighbour of that region — so **each region is connected by construction**.
    The whole (connected) chart is reached, so every face is labelled; the inter-group
    interior edges form one connected cut. Returns ``(group_a, group_b, new_seam_edges)``;
    both groups are guaranteed connected and non-empty (or ``[], []`` if unsplittable).

    Invariant: re-flooding the seam set after adding ``new_seam_edges`` turns this one
    chart into exactly two connected charts — never a shower of fragments."""
    import heapq

    if normals is None:
        normals = _face_normals(mesh)
    faces = list(face_ids)
    if len(faces) < 2:
        return faces, [], []
    fset = set(faces)
    a, b = _farthest_normal_seeds(faces, normals)
    if a == b:
        return faces, [], []

    adjacency = mesh.face_adjacency()
    seed_n = {0: normals[a], 1: normals[b]}
    label: dict[int, int] = {a: 0, b: 1}
    pq: list[tuple[float, int, int]] = []

    def push_neighbors(f: int, lab: int) -> None:
        for nb, eid in adjacency[f]:
            if nb in fset and nb not in label and eid not in seams:
                cost = 1.0 - float(np.dot(normals[nb], seed_n[lab]))
                heapq.heappush(pq, (cost, nb, lab))

    push_neighbors(a, 0)
    push_neighbors(b, 1)
    while pq:
        _, f, lab = heapq.heappop(pq)
        if f in label:
            continue
        label[f] = lab          # f borders a same-label face -> region stays connected
        push_neighbors(f, lab)

    # Any face not reached via interior edges (chart pinched by seams) -> inherit a
    # labelled neighbour's label by connectivity propagation (NEVER by raw normal,
    # which would scatter the labels and fragment the chart).
    leftover = [f for f in faces if f not in label]
    progressed = True
    while leftover and progressed:
        progressed = False
        still: list[int] = []
        for f in leftover:
            lab = next((label[nb] for nb, _ in adjacency[f] if nb in label), None)
            if lab is None:
                still.append(f)
            else:
                label[f] = lab
                progressed = True
        leftover = still
    for f in leftover:          # truly detached island of the chart -> one group
        label[f] = 0

    group_a = [f for f in faces if label[f] == 0]
    group_b = [f for f in faces if label[f] == 1]
    if not group_a or not group_b:
        return faces, [], []

    new_seams = [eid for eid in _interior_edges(mesh, fset, seams)
                 if label.get(mesh.edges[eid].face_ids[0]) != label.get(mesh.edges[eid].face_ids[1])]
    return group_a, group_b, new_seams


def _charts_from_seams(mesh: MeshGraph, seams: set[int]) -> list[list[int]]:
    """Connected charts (flood fill) — the single source of truth. Every chart is
    connected by construction, so χ is meaningful and groups are never scattered."""
    return flood_charts(mesh, seams)


def segment(
    mesh: MeshGraph,
    *,
    fold_angle: float = FOLD_ANGLE,
    cone_limit: float = DEFAULT_CONE_LIMIT,
    max_charts: int = DEFAULT_MAX_CHARTS,
    merge: bool = True,
    straighten: bool = True,
) -> ChartSegmentation:
    """Segment ``mesh`` into few near-developable charts (chart-UV plan §5).

    Seam-centric: the only state is the seam set; charts are always re-derived by flood
    fill (so they stay connected). Stages:

    - R2: seed seams at every ≥ ``fold_angle`` fold (+ boundary/non-manifold).
    - R1 split: split the worst normal-cone chart (each split adds exactly one connected
      cut ⇒ exactly two connected charts) until all are under ``cone_limit`` or the cap.
    - disk-ify: sever every non-disk chart — completed ALWAYS (the cap may be exceeded
      and reported, but a non-disk chart would flip in ABF, so the invariant is kept).
    - absorb: force any chart < 5 faces into a neighbour across a non-mandatory boundary
      (confetti guard, unconditional on developability; R2 seams are never crossed).
    - merge: fold adjacent charts whose union is still a developable disk sharing no
      mandatory seam (R1 minimality).

    Guarantees: charts partition the faces, each connected and a topological disk;
    no chart smaller than 5 faces unless walled by mandatory seams."""
    normals = _face_normals(mesh)
    seams = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    history = [{"stage": "initial", "charts": len(_charts_from_seams(mesh, seams)),
                "mandatory_seams": len(seams)}]

    # R1 split loop (worst normal-cone first), seam-centric.
    for _ in range(max_charts * 4):
        charts = _charts_from_seams(mesh, seams)
        if len(charts) >= max_charts:
            break
        ranked = sorted(charts, key=lambda fs: normal_cone_halfangle(mesh, fs, normals), reverse=True)
        if normal_cone_halfangle(mesh, ranked[0], normals) <= cone_limit:
            break
        progressed = False
        for fs in ranked:
            if normal_cone_halfangle(mesh, fs, normals) <= cone_limit:
                break
            _, _, new_seams = split_chart(mesh, fs, seams, normals)
            if new_seams:
                seams.update(new_seams)
                progressed = True
                break
        if not progressed:
            break
    history.append({"stage": "split", "charts": len(_charts_from_seams(mesh, seams))})

    # Disk-ification: a non-disk chart flips/overlaps in ABF, so the disk invariant is
    # NON-NEGOTIABLE — it is completed regardless of ``max_charts`` (the cap may be
    # exceeded and reported, but the invariant is always kept). Bounded by face count.
    for _ in range(mesh.face_count + 1):
        nondisk = [fs for fs in _charts_from_seams(mesh, seams) if not is_disk(mesh, fs)]
        if not nondisk:
            break
        progressed = False
        for fs in sorted(nondisk, key=len, reverse=True):
            _, _, new_seams = split_chart(mesh, fs, seams, normals)
            if new_seams:
                seams.update(new_seams)
                progressed = True
                break
        if not progressed:
            break
    charts = _charts_from_seams(mesh, seams)
    non_disk = sum(0 if is_disk(mesh, fs) else 1 for fs in charts)
    history.append({"stage": "diskify", "charts": len(charts), "non_disk": non_disk})

    # Confetti absorption (R1): merge alone only folds developable-disk unions, so
    # tiny slivers (a few faces from an over-eager split) never get absorbed. Force any
    # chart below ``min_chart_faces`` into a neighbour across a NON-mandatory boundary;
    # a sliver fully walled by R2 folds is left and reported.
    _absorb_small_charts(mesh, seams, fold_angle=fold_angle, min_chart_faces=5)
    history.append({"stage": "absorb", "charts": len(_charts_from_seams(mesh, seams))})

    if merge:
        _merge_pass(mesh, seams, normals, cone_limit, fold_angle)
        history.append({"stage": "merge", "charts": len(_charts_from_seams(mesh, seams))})

    # U1.5 boundary straightening (better packing), then a final merge to fold any
    # charts the straightening made mergeable.
    if straighten:
        n_moved = straighten_boundaries(mesh, seams, fold_angle=fold_angle)
        if merge:
            _merge_pass(mesh, seams, normals, cone_limit, fold_angle)
        history.append({"stage": "straighten", "moved": n_moved,
                        "charts": len(_charts_from_seams(mesh, seams))})

    # R2 protection (MINIMAL_DISTORTION_UV_PLAN §5.1): mandatory folds are seeded up front
    # and every pass (absorb/merge/straighten) already refuses to cross them, but re-assert
    # the union here as a belt-and-suspenders guard so no refactor can ever drop one.
    seams |= mandatory_seam_edges(mesh, fold_angle=fold_angle)

    # NOTE: folds that are interior to a chart (a lone crease that doesn't reach the
    # boundary, or one a merge buried) would weld in the UV. We do NOT pre-split them all
    # here: the flood-chart heuristic can't tell which interior slits Blender actually welds
    # (it cuts any slit that reaches a chart boundary), so doing so over-fragments. The real
    # weld is detected post-unwrap from the UVMap and repaired with targeted splits in the
    # pipeline loop (MINIMAL_DISTORTION_UV_PLAN §M2).
    charts = _charts_from_seams(mesh, seams)
    face_chart = {fid: cid for cid, fs in enumerate(charts) for fid in fs}
    final_nondisk = sum(0 if is_disk(mesh, fs) else 1 for fs in charts)
    audit = mandatory_seam_audit(mesh, seams, fold_angle=fold_angle)
    history.append({"stage": "final", "charts": len(charts), "non_disk": final_nondisk,
                    "cap_exceeded": len(charts) > max_charts,
                    "mandatory_90_edges": audit["mandatory_90_edges"],
                    "mandatory_90_missing": audit["mandatory_90_missing"]})
    return ChartSegmentation(mesh=mesh, face_chart=face_chart, seams=seams, history=history)


def _connected_faces(mesh: MeshGraph, face_set: set[int], adjacency) -> bool:
    """Whether ``face_set`` is connected through interior (non-seam, but here any
    shared-edge) adjacency — used as a relabel guard."""
    if not face_set:
        return False
    start = next(iter(face_set))
    seen = {start}
    stack = [start]
    while stack:
        cur = stack.pop()
        for nb, _ in adjacency[cur]:
            if nb in face_set and nb not in seen:
                seen.add(nb)
                stack.append(nb)
    return len(seen) == len(face_set)


def straighten_boundaries(mesh: MeshGraph, seams: set[int], *, fold_angle: float = FOLD_ANGLE,
                          min_chart_faces: int = 5, passes: int = 4) -> int:
    """U1.5 — straighten jagged chart borders by relabelling boundary faces to minimise
    total non-mandatory boundary length (chart-UV plan §5.5). A face that juts into a
    neighbour (more seam edges to it than back to its own chart) is moved there, which
    nets fewer seams ⇒ a straighter, more compact, better-packing chart.

    Mandatory (R2) seams are NEVER re-routed: a face is not moved across a fold, and a
    move is rejected if it would bury a mandatory edge inside a chart. Every move keeps
    both charts connected topological disks of ≥ ``min_chart_faces`` (the disk + no-1-face
    guards). Returns the number of faces relabelled."""
    mandatory = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    adjacency = mesh.face_adjacency()
    moved = 0

    for _ in range(passes):
        charts = _charts_from_seams(mesh, seams)
        face_chart = {f: cid for cid, fs in enumerate(charts) for f in fs}
        chart_faces = {cid: set(fs) for cid, fs in enumerate(charts)}
        changed = False

        for f in mesh.faces:
            fid = f.id
            cid = face_chart[fid]
            to_self: list[int] = []
            by_neighbor: dict[int, list[int]] = {}
            blocked: set[int] = set()
            for nb, eid in adjacency[fid]:
                nc = face_chart[nb]
                if nc == cid:
                    to_self.append(eid)
                elif eid in mandatory:
                    blocked.add(nc)          # cannot move across an R2 fold
                else:
                    by_neighbor.setdefault(nc, []).append(eid)
            cands = {c: e for c, e in by_neighbor.items() if c not in blocked}
            if not cands:
                continue
            target = max(cands, key=lambda k: len(cands[k]))
            removed = cands[target]          # f→target seams become interior
            added = to_self                  # f→old-chart edges become seams
            if len(removed) <= len(added):
                continue                      # not a straightening (net seams not reduced)

            new_self = chart_faces[cid] - {fid}
            new_tgt = chart_faces[target] | {fid}
            if len(new_self) < min_chart_faces:
                continue                      # no-1-face / sliver guard on the source
            if not _connected_faces(mesh, new_self, adjacency) or not is_disk(mesh, new_self):
                continue                      # disk invariant on the source
            if not is_disk(mesh, new_tgt):
                continue                      # disk invariant on the target

            seams.difference_update(removed)
            seams.update(added)
            chart_faces[cid] = new_self
            chart_faces[target] = new_tgt
            face_chart[fid] = target
            moved += 1
            changed = True
        if not changed:
            break
    return moved


def _absorb_small_charts(mesh: MeshGraph, seams: set[int], *, fold_angle: float,
                         min_chart_faces: int) -> None:
    """Dissolve every chart smaller than ``min_chart_faces`` into the neighbour with the
    most shared non-mandatory boundary (chart-UV plan §5.4 confetti guard). Unconditional
    on developability — a stray sliver must not survive — but never crosses an R2 seam.
    A sliver fully bounded by mandatory seams is left in place (reported via chart count)."""
    mandatory = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    adjacency = mesh.face_adjacency()

    for _ in range(mesh.face_count + 1):
        charts = _charts_from_seams(mesh, seams)
        if len(charts) <= 1:
            return
        face_chart = {fid: cid for cid, fs in enumerate(charts) for fid in fs}
        small = [(cid, fs) for cid, fs in enumerate(charts) if len(fs) < min_chart_faces]
        if not small:
            return
        absorbed = False
        for cid, fs in sorted(small, key=lambda x: len(x[1])):
            # Removable boundary edges grouped by neighbouring chart.
            by_neighbor: dict[int, list[int]] = {}
            for f in fs:
                for nb, eid in adjacency[f]:
                    nc = face_chart.get(nb)
                    if nc is not None and nc != cid and eid in seams and eid not in mandatory:
                        by_neighbor.setdefault(nc, []).append(eid)
            if not by_neighbor:
                continue  # walled by mandatory seams -> leave it
            # Disk guard: prefer a neighbour whose union with the sliver stays a disk;
            # only if none qualifies fall back to the largest-contact neighbour (the
            # sliver must be absorbed — never left as 1-face confetti).
            disk_ok = [c for c in by_neighbor
                       if is_disk(mesh, charts[c] + list(fs))]
            pool = disk_ok or list(by_neighbor)
            best = max(pool, key=lambda k: len(by_neighbor[k]))
            seams.difference_update(by_neighbor[best])
            absorbed = True
            break
        if not absorbed:
            return


def _merge_pass(mesh, seams: set[int], normals, cone_limit, fold_angle):
    """Greedily remove a non-mandatory shared boundary between two charts when their
    union stays a developable disk (R1 minimality, chart-UV plan §5.4). Seam-centric:
    operates on the seam set, re-deriving charts each round."""
    mandatory = mandatory_seam_edges(mesh, fold_angle=fold_angle)

    changed = True
    while changed:
        changed = False
        charts = _charts_from_seams(mesh, seams)
        face_chart = {fid: cid for cid, fs in enumerate(charts) for fid in fs}
        chart_faces = {cid: fs for cid, fs in enumerate(charts)}

        # Group the removable seam edges by the chart pair they separate.
        border: dict[tuple[int, int], list[int]] = {}
        for eid in seams:
            e = mesh.edges[eid]
            if eid in mandatory or len(e.face_ids) != 2:
                continue
            ca, cb = face_chart.get(e.face_ids[0]), face_chart.get(e.face_ids[1])
            if ca is None or cb is None or ca == cb:
                continue
            border.setdefault((min(ca, cb), max(ca, cb)), []).append(eid)

        for (ca, cb), edges in sorted(border.items()):
            union = chart_faces[ca] + chart_faces[cb]
            if (normal_cone_halfangle(mesh, union, normals) <= cone_limit
                    and is_disk(mesh, union)):
                seams.difference_update(edges)  # dissolve the whole shared boundary
                changed = True
                break
