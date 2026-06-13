"""T2 — chart-id projection onto the adaptive mesh (UV_TRANSFER_PLAN §3.T2).

For each adaptive face, copy the ``ref_chart_id`` of the nearest *compatible* reference
surface point, then clean the label field. The nearest-surface query itself is a spatial
structure (a BVH in Blender); this module is the Blender-free LOGIC around it — the three
mandatory pitfall guards — exercised by a brute-force oracle in the unit tests:

  - normal compatibility: reject a hit whose ref-face normal opposes the adaptive face
    normal (``dot < min_dot``); take the nearest compatible hit instead. Stops chart
    bleed where the reference's separate shells touch (between legs, arm–torso, cloth).
  - distance sanity: a hit farther than ``max_distance`` is "unassigned" → filled later
    from face-adjacency majority.
  - speckle cleanup: majority-vote label smoothing, then one-connected-component per id.
"""

from __future__ import annotations

from collections import Counter, deque

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph

UNASSIGNED = -1


def face_centroid(mesh: MeshGraph, fid: int) -> np.ndarray:
    return np.mean([mesh.vertex_co(v) for v in mesh.faces[fid].vertex_ids], axis=0)


def pick_compatible_hit(adaptive_normal, candidates, *, min_dot: float = 0.2,
                        max_distance: float | None = None):
    """From distance-sorted ``candidates`` (each ``(chart_id, distance, ref_normal)``),
    return the chart id of the nearest hit that is normal-compatible
    (``dot(n_adaptive, n_ref) >= min_dot``) and within ``max_distance``; else ``None``
    (T2 normal-compat + distance guards). ``candidates`` need not be pre-sorted — it is
    sorted here by distance."""
    n = np.asarray(adaptive_normal, dtype=float)
    nn = np.linalg.norm(n)
    if nn > 1e-12:
        n = n / nn
    for cid, dist, rnorm in sorted(candidates, key=lambda c: c[1]):
        if max_distance is not None and dist > max_distance:
            return None  # nearest hit is already too far → unassigned
        rn = np.asarray(rnorm, dtype=float)
        rl = np.linalg.norm(rn)
        if rl > 1e-12:
            rn = rn / rl
        if float(np.dot(n, rn)) >= min_dot:
            return int(cid)
    return None


def project_chart_ids(mesh: MeshGraph, query, *, min_dot: float = 0.2,
                      max_distance: float | None = None) -> dict[int, int]:
    """Assign each adaptive face a ref chart id (T2). ``query(centroid)`` returns a list
    of candidate ``(chart_id, distance, ref_normal)`` (k-nearest). Faces with no
    compatible/near hit are left :data:`UNASSIGNED` for :func:`fill_unassigned`."""
    label: dict[int, int] = {}
    for f in mesh.faces:
        cands = query(face_centroid(mesh, f.id))
        cid = pick_compatible_hit(f.normal, cands, min_dot=min_dot, max_distance=max_distance)
        label[f.id] = UNASSIGNED if cid is None else cid
    return label


def fill_unassigned(mesh: MeshGraph, label: dict[int, int], *, max_rounds: int = 50) -> dict[int, int]:
    """Fill :data:`UNASSIGNED` faces from the majority label of their assigned edge
    neighbours, propagating inward over several rounds (T2 distance-guard backfill)."""
    adjacency = mesh.face_adjacency()
    out = dict(label)
    for _ in range(max_rounds):
        pending = [f for f, c in out.items() if c == UNASSIGNED]
        if not pending:
            break
        changed = False
        for f in pending:
            votes = Counter(out[nb] for nb, _ in adjacency[f] if out[nb] != UNASSIGNED)
            if votes:
                out[f] = votes.most_common(1)[0][0]
                changed = True
        if not changed:
            break
    # Any still-unassigned island (no assigned neighbour at all) → its own residual id.
    leftover = [f for f, c in out.items() if c == UNASSIGNED]
    if leftover:
        fallback = (max(c for c in out.values() if c != UNASSIGNED) + 1) if any(
            c != UNASSIGNED for c in out.values()) else 0
        for f in leftover:
            out[f] = fallback
    return out


def smooth_labels(mesh: MeshGraph, label: dict[int, int], *, rounds: int = 10) -> dict[int, int]:
    """Majority-vote label smoothing (T2 speckle cleanup): any face whose id disagrees
    with the strict majority of its edge-neighbours flips to that majority. Iterated up
    to ``rounds`` times or until stable. Removes single-face speckles at chart borders."""
    adjacency = mesh.face_adjacency()
    out = dict(label)
    for _ in range(max(0, rounds)):
        changed = False
        nxt = dict(out)
        for f in mesh.faces:
            nbs = [out[nb] for nb, _ in adjacency[f.id]]
            if not nbs:
                continue
            votes = Counter(nbs)
            top, cnt = votes.most_common(1)[0]
            if top != out[f.id] and cnt > len(nbs) / 2:
                nxt[f.id] = top
                changed = True
        out = nxt
        if not changed:
            break
    return out


def _components(mesh: MeshGraph, faces: set[int], adjacency) -> list[list[int]]:
    seen: set[int] = set()
    comps: list[list[int]] = []
    for start in faces:
        if start in seen:
            continue
        comp: list[int] = []
        q = deque([start])
        seen.add(start)
        while q:
            cur = q.popleft()
            comp.append(cur)
            for nb, _ in adjacency[cur]:
                if nb in faces and nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        comps.append(comp)
    return comps


def enforce_connected_components(mesh: MeshGraph, label: dict[int, int], *,
                                 minor_frac: float = 0.2) -> tuple[dict[int, int], list[dict]]:
    """Make each chart id one connected component (T2 final cleanup). For an id split
    into several components: minor fragments (< ``minor_frac`` of that id's faces) are
    absorbed into the surrounding chart (the majority id around the fragment); a major
    split (two big components, e.g. mirrored left/right legs) is kept but the smaller
    gets a FRESH id so it can be placed/packed separately into the same reference slot.

    Returns ``(new_label, split_log)``; ``split_log`` records every fresh id and which
    reference id it inherits (so the placement step reuses that slot)."""
    adjacency = mesh.face_adjacency()
    out = dict(label)
    by_id: dict[int, list[int]] = {}
    for f, c in out.items():
        by_id.setdefault(c, []).append(f)

    next_id = max(out.values(), default=-1) + 1
    split_log: list[dict] = []
    for cid, faces in list(by_id.items()):
        comps = _components(mesh, set(faces), adjacency)
        if len(comps) <= 1:
            continue
        comps.sort(key=len, reverse=True)
        total = len(faces)
        for comp in comps[1:]:
            if len(comp) < minor_frac * total:
                # Minor fragment → absorb into the surrounding majority id.
                ring = Counter(out[nb] for f in comp for nb, _ in adjacency[f]
                               if out[nb] != cid)
                target = ring.most_common(1)[0][0] if ring else cid
                for f in comp:
                    out[f] = target
            else:
                # Major split → fresh id inheriting the same reference placement slot.
                for f in comp:
                    out[f] = next_id
                split_log.append({"new_id": next_id, "ref_id": cid, "faces": len(comp)})
                next_id += 1
    return out, split_log


# -- brute-force oracle for the Blender-free tests ---------------------------

def _closest_point_on_triangle(p, a, b, c):
    """Closest point on triangle ``abc`` to point ``p`` (Ericson, Real-Time Collision)."""
    p, a, b, c = (np.asarray(x, float) for x in (p, a, b, c))
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = float(ab @ ap), float(ac @ ap)
    if d1 <= 0 and d2 <= 0:
        return a
    bp = p - b
    d3, d4 = float(ab @ bp), float(ac @ bp)
    if d3 >= 0 and d4 <= d3:
        return b
    cp = p - c
    d5, d6 = float(ab @ cp), float(ac @ cp)
    if d6 >= 0 and d5 <= d6:
        return c
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        return a + (d1 / (d1 - d3)) * ab
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        return a + (d2 / (d2 - d6)) * ac
    va = d3 * d6 - d5 * d4
    if va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
        return b + ((d4 - d3) / ((d4 - d3) + (d5 - d6))) * (c - b)
    denom = 1.0 / (va + vb + vc)
    return a + ab * (vb * denom) + ac * (vc * denom)


def build_brute_oracle(ref_mesh: MeshGraph, ref_face_chart: dict[int, int], *, k: int = 8):
    """A pure (Blender-free) k-nearest reference-surface oracle for the unit tests — the
    same contract :func:`project_chart_ids` expects from the Blender BVH. Returns
    ``query(centroid) -> [(chart_id, distance, ref_normal), ...]`` (k nearest faces)."""
    faces = [(f.id, [ref_mesh.vertex_co(v) for v in f.vertex_ids],
              np.asarray(f.normal, float), ref_face_chart.get(f.id, 0)) for f in ref_mesh.faces]

    def query(centroid):
        c = np.asarray(centroid, float)
        hits = []
        for fid, verts, normal, chart in faces:
            best = None
            for i in range(1, len(verts) - 1):
                cp = _closest_point_on_triangle(c, verts[0], verts[i], verts[i + 1])
                d = float(np.linalg.norm(cp - c))
                if best is None or d < best:
                    best = d
            hits.append((chart, best, normal))
        hits.sort(key=lambda h: h[1])
        return hits[:k]

    return query
