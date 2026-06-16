"""A4 — seam templates per part type (AUTO_ARTIST_UV_PLAN §5.A4).

Turn the A1 part decomposition + A3 classes into a concrete seam edge set whose charts
correspond to parts. Pure Python on a :class:`~uv_agent.geometry.mesh_graph.MeshGraph`
(the SLIM unwrap that consumes the seams is the Blender step in ``pipeline``).

Template rules (plan §5.A4), built on the part-boundary seam floor:

- ``cylinder``  DEDICATED template (:func:`cylinder_template`): separate the end caps, then
                cut the tube body lengthwise so SLIM flattens it into a RECTANGLE (+ cap
                disks). Validated and REVERTS rather than shatter a fat/stubby tube. This is
                what makes the trident shaft a rectangular strip instead of a blob.
- ``strip``     kept as one long island (no template seams, no cone-split); unwraps flat.
- ``panel``     kept intact (no template seams, no cone-split); only diskified if non-disk.
- ``blob``      pelt-style chart segmentation of that part (cone-split + diskify) so an
                organic mass does not unwrap as one high-stretch sheet.
- ``cap``/``detail``  small self-contained island; diskify only.
- ``unknown``/``shell``  EXPLICIT fall back to chart segmentation for that part (plan §5.A4).

Every chart is guaranteed a topological disk (a non-disk chart self-folds in SLIM); the
total chart count is capped (``max_charts``) and over-runs are reported, never silently
dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from artist_uv_agent.classification import PartClass
from artist_uv_agent.descriptors import PartDescriptor, quiet_fp
from artist_uv_agent.segmentation import PartSegmentation, part_seam_edges
from uv_agent.geometry.mesh_graph import MeshGraph

# Classes whose interior is decomposed by cone-split (organic / fallback); the rest keep
# their template shape and are only diskified (a topological necessity).
CONE_SPLIT_CLASSES = {"blob", "unknown", "shell"}
DEFAULT_CONE_LIMIT = 55.0
DEFAULT_MAX_CHARTS = 60


@dataclass
class SeamResult:
    seams: set[int]
    chart_to_part: dict[int, int]          # flooded chart id → part id
    chart_role: dict[int, str]             # flooded chart id → layout role (part class)
    part_charts: dict[int, list[int]]      # part id → its chart ids
    repair_log: list[dict] = field(default_factory=list)
    cap_exceeded: bool = False

    def to_dict(self) -> dict:
        return {"seam_count": len(self.seams), "chart_count": len(self.chart_to_part),
                "chart_to_part": {int(k): int(v) for k, v in self.chart_to_part.items()},
                "chart_role": {int(k): v for k, v in self.chart_role.items()},
                "part_charts": {int(k): v for k, v in self.part_charts.items()},
                "repair_log": self.repair_log, "cap_exceeded": self.cap_exceeded}


def _vertex_co(mesh, vid):
    return np.asarray(mesh.vertices[vid].co, float)


def _part_boundary_vertices(mesh: MeshGraph, faces) -> dict[int, int]:
    """Vertex → boundary-edge incidence count for the part chart (edges used once by the
    part, or globally boundary). Vertices on the part's boundary loops."""
    fset = set(faces)
    use: dict[int, int] = {}
    for f in faces:
        for e in mesh.faces[f].edge_ids:
            use[e] = use.get(e, 0) + 1
    inc: dict[int, int] = {}
    for e, n in use.items():
        ed = mesh.edges[e]
        if n == 1 or ed.is_boundary or ed.is_non_manifold:
            for v in ed.vertex_ids:
                inc[v] = inc.get(v, 0) + 1
    return inc


def _boundary_loops_of_part(mesh: MeshGraph, faces) -> list[list[int]]:
    """The part chart's boundary loops as vertex lists (used to open a tube between two
    ends). Chains boundary edges of the part by shared vertices."""
    fset = set(faces)
    use: dict[int, int] = {}
    for f in faces:
        for e in mesh.faces[f].edge_ids:
            use[e] = use.get(e, 0) + 1
    bedges = [e for e, n in use.items()
              if n == 1 or mesh.edges[e].is_boundary or mesh.edges[e].is_non_manifold]
    adj: dict[int, list[int]] = {}
    for e in bedges:
        a, b = mesh.edges[e].vertex_ids
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    seen: set[int] = set()
    loops: list[list[int]] = []
    for start in list(adj):
        if start in seen:
            continue
        comp = [start]
        seen.add(start)
        stack = [start]
        while stack:
            v = stack.pop()
            for nb in adj.get(v, ()):
                if nb not in seen:
                    seen.add(nb)
                    comp.append(nb)
                    stack.append(nb)
        loops.append(comp)
    return loops


def _part_edge_graph(mesh: MeshGraph, faces, seams: set[int]):
    """Vertex adjacency over edges interior to the part (not already seams), with the
    edge id, for routing a longitudinal cut path."""
    fset = set(faces)
    adj: dict[int, list[tuple[int, int]]] = {}
    for e in mesh.edges:
        if e.id in seams or len(e.face_ids) != 2:
            continue
        if e.face_ids[0] in fset and e.face_ids[1] in fset:
            a, b = e.vertex_ids
            adj.setdefault(a, []).append((b, e.id))
            adj.setdefault(b, []).append((a, e.id))
    return adj


def _open_tube_seam(mesh: MeshGraph, faces, desc: PartDescriptor, seams: set[int],
                    back_dir: np.ndarray | None) -> list[int]:
    """Cut a cylinder open with ONE lengthwise seam: a shortest vertex path between its
    two boundary loops, biased toward the least-visible (``back_dir``) side. Returns the
    seam edge ids, or ``[]`` if the part is not a clean two-loop tube (diskify handles it).
    The path turns the annulus (tube, χ=0) into a disk (χ=1)."""
    loops = _boundary_loops_of_part(mesh, faces)
    if len(loops) != 2:
        return []
    adj = _part_edge_graph(mesh, faces, seams)
    if not adj:
        return []
    axis = np.asarray(desc.principal_axes[0], float)
    if back_dir is None:
        back_dir = np.array([0.0, -1.0, 0.0])
    # back component perpendicular to the axis (a longitudinal seam should hide on one side)
    back = back_dir - np.dot(back_dir, axis) * axis
    bn = np.linalg.norm(back)
    back = back / bn if bn > 1e-9 else np.zeros(3)

    import heapq
    starts = set(loops[0])
    goals = set(loops[1])
    # Dijkstra from all of loop0; edge cost favours the back side (low cost where the edge
    # midpoint faces back) so the cut hides there.
    dist = {v: 0.0 for v in starts}
    prev: dict[int, tuple[int, int]] = {}
    pq = [(0.0, v) for v in starts]
    heapq.heapify(pq)
    reached = None
    while pq:
        d, v = heapq.heappop(pq)
        if d > dist.get(v, np.inf):
            continue
        if v in goals:
            reached = v
            break
        for nb, eid in adj.get(v, ()):
            mid = 0.5 * (_vertex_co(mesh, v) + _vertex_co(mesh, nb))
            seglen = float(np.linalg.norm(_vertex_co(mesh, nb) - _vertex_co(mesh, v)))
            facing = float(np.dot(mid - np.asarray(desc.centroid), back))  # >0 = back side
            cost = seglen * (1.0 + 0.6 * np.clip(-facing, 0.0, None))
            nd = d + cost
            if nd < dist.get(nb, np.inf):
                dist[nb] = nd
                prev[nb] = (v, eid)
                heapq.heappush(pq, (nd, nb))
    if reached is None:
        return []
    path_edges: list[int] = []
    v = reached
    while v in prev:
        pv, eid = prev[v]
        path_edges.append(eid)
        v = pv
    return path_edges


CAP_ALIGN = 0.55       # |face_normal · axis| above this ⇒ an END-CAP face, not tube wall
CAP_END_BAND = 0.30    # a cap component must sit within this fraction of an axis end


def _face_centroid(mesh: MeshGraph, fid: int) -> np.ndarray:
    return np.mean([mesh.vertices[v].co for v in mesh.faces[fid].vertex_ids], axis=0)


def _connected_within(mesh: MeshGraph, faces, seams: set[int], adjacency) -> list[list[int]]:
    """Flood ``faces`` into components that never cross ``seams`` (restricted to the set)."""
    fset = set(faces)
    seen: set[int] = set()
    comps: list[list[int]] = []
    for f in faces:
        if f in seen:
            continue
        comp = [f]
        seen.add(f)
        stack = [f]
        while stack:
            cur = stack.pop()
            for nb, eid in adjacency[cur]:
                if nb in fset and nb not in seen and eid not in seams:
                    seen.add(nb)
                    comp.append(nb)
                    stack.append(nb)
        comps.append(comp)
    return comps


def _end_caps(mesh: MeshGraph, faces, axis: np.ndarray) -> list[list[int]]:
    """At most ONE end-cap component per axis end: the largest connected group of
    axis-aligned-normal faces sitting near each extreme (a fat tube has many cap faces; we
    take a single coherent cap per end, never a shower of slivers)."""
    cap = [f for f in faces if abs(float(np.dot(mesh.faces[f].normal, axis))) > CAP_ALIGN]
    if not cap:
        return []
    adjacency = mesh.face_adjacency()
    comps = _connected_within(mesh, cap, set(), adjacency)
    cents = {f: _face_centroid(mesh, f) for f in faces}
    c = np.mean(list(cents.values()), axis=0)
    t = {f: float(np.dot(cents[f] - c, axis)) for f in faces}
    tmin, tmax = min(t.values()), max(t.values())
    span = max(tmax - tmin, 1e-9)
    near_min, near_max = None, None
    for comp in comps:
        tc = np.mean([t[f] for f in comp])
        if (tc - tmin) < CAP_END_BAND * span:
            if near_min is None or len(comp) > len(near_min):
                near_min = comp
        elif (tmax - tc) < CAP_END_BAND * span:
            if near_max is None or len(comp) > len(near_max):
                near_max = comp
    return [c for c in (near_min, near_max) if c]


def cylinder_template(mesh: MeshGraph, faces, desc: PartDescriptor, seams: set[int],
                      back_dir: np.ndarray | None, *, max_part_charts: int = 3
                      ) -> tuple[list[int], list[list[int]]]:
    """Dedicated CYLINDER unwrap template (plan §5.A4): separate the end CAPS, then cut the
    tube body open with ONE lengthwise seam so SLIM flattens it into a RECTANGLE.

    A capped/attached tube usually has only one boundary loop, so :func:`_open_tube_seam`
    alone never fires (it needs two) and the tube cannot flatten — the bug behind blob-shaped
    cylinder UVs. Here ≤1 cap ring per end is seamed off first (each adds a boundary loop),
    THEN one lengthwise cut runs between the two ends. The result is VALIDATED — body + caps
    must each be a single UV disk and the part must stay ≤ ``max_part_charts``; otherwise the
    template REVERTS (returns ``([], [])``) so a fat/stubby tube is never shattered (it falls
    back to its intact island). Returns ``(seam_edges, cap_face_groups)``."""
    axis = np.asarray(desc.principal_axes[0], float)
    n = np.linalg.norm(axis)
    axis = axis / n if n > 1e-9 else np.array([0.0, 0.0, 1.0])
    fset = set(faces)
    adjacency = mesh.face_adjacency()

    caps = _end_caps(mesh, faces, axis)
    new_seams: list[int] = []
    capped: set[int] = set()
    for comp in caps:
        comp_set = set(comp)
        for f in comp:
            for nb, eid in adjacency[f]:
                if nb in fset and nb not in comp_set:
                    new_seams.append(eid)
        capped |= comp_set

    body = [f for f in faces if f not in capped]
    work = set(seams) | set(new_seams)
    lengthwise = _open_tube_seam(mesh, body, desc, work, back_dir) if body else []
    if not lengthwise:
        return [], []                      # no clean open cut → leave the tube intact

    # Validate: body is ONE disk, each cap is a disk, part stays within the chart budget.
    final = work | set(lengthwise)
    body_comps = _connected_within(mesh, body, final, adjacency)
    pieces = body_comps + [list(c) for c in caps]
    if len(pieces) > max_part_charts or any(not uv_is_disk(mesh, p, final) for p in pieces):
        return [], []                      # would shatter → revert to the intact island
    return new_seams + lengthwise, caps


def open_multiloop_tube(mesh: MeshGraph, faces, seams: set[int], back_dir=None,
                        desc: PartDescriptor | None = None) -> list[int]:
    """Open a tube with ANY number of boundary loops into a disk by connecting all its
    boundary loops with min-cost (optionally back-biased) slits.

    :func:`cylinder_template` only handles a clean TWO-loop tube; a decimated limb/sleeve
    often has 3+ boundary loops (extra holes), so its single lengthwise cut never forms and
    the tube stays a blob. Here each successive loop is joined to the growing connected
    boundary by the cheapest interior path; ``k`` loops need ``k-1`` slits, which turns the
    multiply-connected surface into a topological disk. Returns the slit edge ids, or ``[]``
    if the loops cannot be connected (kept intact) or the result is still not a disk."""
    import heapq

    loops = _boundary_loops_of_part(mesh, faces)
    if len(loops) < 2:
        return []
    adj = _part_edge_graph(mesh, faces, seams)
    if not adj:
        return []

    back = None
    centroid = None
    if desc is not None:
        axis = np.asarray(desc.principal_axes[0], float)
        an = np.linalg.norm(axis)
        axis = axis / an if an > 1e-9 else np.array([0.0, 0.0, 1.0])
        bd = np.array([0.0, -1.0, 0.0]) if back_dir is None else np.asarray(back_dir, float)
        b = bd - np.dot(bd, axis) * axis
        bn = np.linalg.norm(b)
        back = b / bn if bn > 1e-9 else None
        centroid = np.asarray(desc.centroid, float)

    inf = float("inf")
    slits: list[int] = []
    connected = set(loops[0])
    for L in loops[1:]:
        goals = set(L)
        dist = {v: 0.0 for v in connected}
        prev: dict[int, tuple[int, int]] = {}
        pq = [(0.0, v) for v in connected]
        heapq.heapify(pq)
        reached = None
        while pq:
            d, v = heapq.heappop(pq)
            if d > dist.get(v, inf):
                continue
            if v in goals:
                reached = v
                break
            for nb, eid in adj.get(v, ()):
                seglen = float(np.linalg.norm(_vertex_co(mesh, nb) - _vertex_co(mesh, v)))
                cost = seglen
                if back is not None:
                    mid = 0.5 * (_vertex_co(mesh, v) + _vertex_co(mesh, nb))
                    facing = float(np.dot(mid - centroid, back))
                    cost = seglen * (1.0 + 0.6 * float(np.clip(-facing, 0.0, None)))
                nd = d + cost
                if nd < dist.get(nb, inf):
                    dist[nb] = nd
                    prev[nb] = (v, eid)
                    heapq.heappush(pq, (nd, nb))
        if reached is None:
            return []                       # a loop we cannot reach → leave the tube intact
        v = reached
        while v in prev:
            pv, eid = prev[v]
            if eid not in slits:
                slits.append(eid)
            connected.add(v)                # path vertices join the connected boundary
            v = pv
        connected |= goals
    if not slits or not uv_is_disk(mesh, faces, set(seams) | set(slits)):
        return []
    return slits


def uv_is_disk(mesh: MeshGraph, faces, seams: set[int]) -> bool:
    """Whether a flooded chart unwraps to a topological **disk** once cut along its
    incident ``seams`` (χ = 1). Unlike the chart engine's :func:`is_disk` — which counts
    the raw face-set euler characteristic — this is SEAM-AWARE: a non-disconnecting slit
    (a cylinder opened lengthwise) correctly reads as a disk, because Blender treats a
    seam edge as a UV cut even when it does not disconnect the face component.

    χ_cut = V_cut − E_cut + F on the *cut* cell complex: a seam edge is a boundary on each
    side it touches (so an internal slit counts twice, a boundary edge once); a vertex is
    duplicated per wedge-group — the faces around it grouped by NON-seam edge adjacency."""
    fset = set(faces)
    F = len(faces)
    # Edges incident to the chart.
    edge_faces: dict[int, list[int]] = {}
    for f in faces:
        for e in mesh.faces[f].edge_ids:
            edge_faces.setdefault(e, []).append(f)
    e_cut = 0
    for e, fs in edge_faces.items():
        inside = [f for f in fs if f in fset]
        if e in seams or mesh.edges[e].is_boundary or mesh.edges[e].is_non_manifold:
            e_cut += len(inside)        # each side is its own boundary edge
        else:
            e_cut += 1                  # glued interior edge (shared by two chart faces)
    # Vertices duplicated per wedge group (faces around v joined only by non-seam edges).
    vert_faces: dict[int, list[int]] = {}
    for f in faces:
        for v in mesh.faces[f].vertex_ids:
            vert_faces.setdefault(v, []).append(f)
    v_cut = 0
    for v, vfs in vert_faces.items():
        # union-find over vfs joined by a shared non-seam, non-boundary edge incident to v.
        parent = {f: f for f in vfs}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        vset = set(vfs)
        for f in vfs:
            for e in mesh.faces[f].edge_ids:
                ed = mesh.edges[e]
                if v not in ed.vertex_ids or e in seams or ed.is_boundary or ed.is_non_manifold:
                    continue
                other = [g for g in ed.face_ids if g != f and g in vset]
                for g in other:
                    parent[find(f)] = find(g)
        v_cut += len({find(f) for f in vfs})
    chi = v_cut - e_cut + F
    return chi == 1


def _has_interior_seam(mesh: MeshGraph, faces, seams: set[int]) -> bool:
    """Whether the chart has any seam edge with BOTH faces inside it (an interior 'slit',
    e.g. a cylinder's lengthwise cut or a buried mandatory fold)."""
    fset = set(faces)
    for f in faces:
        for e in mesh.faces[f].edge_ids:
            if e in seams:
                ed = mesh.edges[e]
                if len(ed.face_ids) == 2 and ed.face_ids[0] in fset and ed.face_ids[1] in fset:
                    return True
    return False


def _is_uv_disk_cheap(mesh: MeshGraph, faces, seams: set[int]) -> bool:
    """Disk test with a cheap precheck: a chart with NO interior seam slit has the same
    cut-complex Euler characteristic as the raw face set, so the O(chart) euler ``is_disk``
    is exact and we skip the heavier per-vertex wedge union-find of :func:`uv_is_disk`. A
    chart WITH an interior slit needs the seam-aware test (a lengthwise-cut tube reads as a
    disk only there)."""
    from chart_uv_agent.segmentation import is_disk

    if not _has_interior_seam(mesh, faces, seams):
        return is_disk(mesh, faces)
    return uv_is_disk(mesh, faces, seams)


def _diskify_and_split(mesh: MeshGraph, seams: set[int], split_classes_faces: set[int],
                       *, cone_limit: float, max_charts: int, log: list[dict],
                       min_chart_faces: int = 5, max_diskify_rounds: int | None = None) -> bool:
    """Diskify every chart (topological necessity), then cone-split only charts whose
    faces are in ``split_classes_faces`` (organic / fallback parts). Reuses the chart
    engine's VSA split so each cut yields exactly two connected disks. A cone-split is
    rejected if it would create a sub-``min_chart_faces`` piece (no confetti — plan §6
    min-island hard gate). Returns whether the chart cap was exceeded.

    ``max_diskify_rounds`` (default ``None`` = unbounded, the topological guarantee) caps the
    diskify split count for a time-budgeted run; any chart left non-disk is surfaced by the
    caller's disk audit, never silently shipped."""
    from chart_uv_agent.segmentation import flood_charts, normal_cone_halfangle, split_chart

    # 1. Diskify — unconditional by default (a non-disk chart self-folds in SLIM). Seam-aware:
    #    a cylinder already opened by its lengthwise slit reads as a disk and is left alone.
    #    The cheap precheck skips the union-find for the common no-interior-seam chart.
    budget = (mesh.face_count + 1) if max_diskify_rounds is None else max_diskify_rounds
    for _ in range(budget):
        charts = flood_charts(mesh, seams)
        nondisk = [fs for fs in charts if not _is_uv_disk_cheap(mesh, fs, seams)]
        if not nondisk:
            break
        progressed = False
        for fs in sorted(nondisk, key=len, reverse=True):
            _, _, ns = split_chart(mesh, fs, seams)
            if ns:
                seams.update(ns)
                progressed = True
                log.append({"op": "diskify_split", "faces": len(fs)})
                break
        if not progressed:
            break

    # 2. Cone-split organic / fallback charts to control stretch (R1, chart §5.3). Try the
    #    worst-cone charts first; skip any split that would shed a sub-floor sliver (keep a
    #    slightly higher cone over confetti). Stop when none is both over-cone and cleanly
    #    splittable.
    for _ in range(max_charts * 4):
        charts = flood_charts(mesh, seams)
        if len(charts) >= max_charts:
            break
        eligible = sorted((fs for fs in charts if set(fs) <= split_classes_faces
                           and normal_cone_halfangle(mesh, fs) > cone_limit),
                          key=lambda fs: normal_cone_halfangle(mesh, fs), reverse=True)
        committed = False
        for fs in eligible:
            ga, gb, ns = split_chart(mesh, fs, seams)
            if ns and len(ga) >= min_chart_faces and len(gb) >= min_chart_faces:
                seams.update(ns)
                log.append({"op": "cone_split", "faces": len(fs)})
                committed = True
                break
        if not committed:
            break
    return len(flood_charts(mesh, seams)) > max_charts


@quiet_fp
def part_seams(mesh: MeshGraph, seg: PartSegmentation, descriptors: list[PartDescriptor],
               classes: list[PartClass], *, back_dir=None, cone_limit: float = DEFAULT_CONE_LIMIT,
               max_charts: int = DEFAULT_MAX_CHARTS) -> SeamResult:
    """Build the seam set from the part templates (plan §5.A4). Returns a
    :class:`SeamResult` mapping every flooded chart to its part + layout role."""
    from chart_uv_agent.segmentation import flood_charts

    desc_by = {d.part_id: d for d in descriptors}
    class_by = {c.part_id: c for c in classes}
    face_part = seg.face_part

    seams = part_seam_edges(mesh, face_part)
    log: list[dict] = []

    # CYLINDER template: separate end caps + cut the tube lengthwise → a rectangle body
    # (+ cap disks). PANEL/STRIP keep their single intact island (no template seams, no
    # cone-split) — they unwrap flat as-is. Only blob/unknown/shell get cone-split below.
    bd = None if back_dir is None else np.asarray(back_dir, float)
    cap_charts: set[int] = set()      # face ids that became cap charts (role override)
    for p in seg.parts:
        if class_by[p.part_id].type != "cylinder":
            continue
        opened, caps = cylinder_template(mesh, p.face_ids, desc_by[p.part_id], seams, bd)
        if opened:
            seams.update(opened)
            log.append({"op": "cylinder_template", "part": p.part_id, "edges": len(opened),
                        "caps": len(caps)})
        for comp in caps:
            cap_charts.update(comp)

    # Faces eligible for cone-split (organic / fallback parts).
    split_faces: set[int] = set()
    for p in seg.parts:
        if class_by[p.part_id].type in CONE_SPLIT_CLASSES:
            split_faces.update(p.face_ids)

    cap_exceeded = _diskify_and_split(mesh, seams, split_faces, cone_limit=cone_limit,
                                      max_charts=max_charts, log=log)

    # Map flooded charts → part + role. Each chart is wholly inside one part (part
    # boundaries are seams), so the part id is the part of any of its faces.
    charts = flood_charts(mesh, seams)
    chart_to_part: dict[int, int] = {}
    chart_role: dict[int, str] = {}
    part_charts: dict[int, list[int]] = {}
    for cid, fs in enumerate(charts):
        pid = face_part[fs[0]]
        chart_to_part[cid] = pid
        # a chart made entirely of separated end-cap faces is a 'cap' (shaft/tine/cap split);
        # the rest of a cylinder part is the rectangular tube body.
        chart_role[cid] = "cap" if set(fs) <= cap_charts else class_by[pid].type
        part_charts.setdefault(pid, []).append(cid)

    return SeamResult(seams=seams, chart_to_part=chart_to_part, chart_role=chart_role,
                      part_charts=part_charts, repair_log=log, cap_exceeded=cap_exceeded)
