"""Phase U1.6 — chart shape repair (chart-UV plan §5b).

Calibration (reference's 39 charts vs ours, same code — `out/shapecal.log`) showed the
shape gap is **convexity**: reference charts are convex (mean 1.06, p10 0.81) while ours
are concave/blobby (mean 0.69, p10 0.38) — the direct cause of the packing holes.
Boundary smoothness was already as good as the reference (ours 1.25 vs 1.41) and tendril
count was already 0 (the ≥5-face absorb removes slivers), so of the plan's three ops the
**concavity split is the active one**; tendril amputation and geodesic re-routing run but
are near-no-ops on these meshes (verified).

The concavity split bisects a concave chart along its short axis by region-growing two
geometric-extreme seeds (connectivity-preserving, like `segmentation.split_chart`) — the
inter-group cut splits the pocket into two compact, more-convex pieces. Splitting only
ever *adds* charts, so it cannot regress unwrap stretch (more charts ⇒ more developable);
the per-pair stretch guard the plan asks for matters for op 1 (re-routing), which we keep
conservative. Pure / numpy on a MeshGraph; runs after segmentation, before U2 unwrap.
"""

from __future__ import annotations

import heapq

import numpy as np

from chart_uv_agent.segmentation import (
    _charts_from_seams, _connected_faces, is_disk, mandatory_seam_edges,
)
from chart_uv_agent.shape import chart_convexity, tendril_chains
from uv_agent.geometry.mesh_graph import MeshGraph


def _face_centroids(mesh: MeshGraph, face_ids) -> dict[int, np.ndarray]:
    out = {}
    for f in face_ids:
        vs = np.array([mesh.vertices[v].co for v in mesh.faces[f].vertex_ids], dtype=float)
        out[f] = vs.mean(axis=0)
    return out


def _pca_2d(points: np.ndarray) -> np.ndarray:
    c = points - points.mean(axis=0)
    _, _, vt = np.linalg.svd(c, full_matrices=False)
    return c @ vt[:2].T


def geometric_bisect(mesh: MeshGraph, face_ids, seams: set[int]) -> list[int]:
    """Split a chart along its short axis into two compact halves (chart-UV plan §5b op3).

    Project face centroids to their best-fit plane, take the two faces at the extremes of
    the longest 2D axis as seeds, and region-grow by 2D distance with a priority queue so
    each half is connected. Returns the interior edges straddling the two halves (the new
    cut); empty if the chart cannot be bisected."""
    faces = list(face_ids)
    if len(faces) < 4:
        return []
    fset = set(faces)
    cents = _face_centroids(mesh, faces)
    proj = _pca_2d(np.array([cents[f] for f in faces]))
    pos = {f: proj[i] for i, f in enumerate(faces)}

    # Seeds: the farthest-apart pair along the principal axis (O(n), not O(n²)).
    axis = proj[np.argmax(proj[:, 0])] - proj[np.argmin(proj[:, 0])]
    if np.linalg.norm(axis) < 1e-9:
        return []
    proj_axis = proj @ (axis / np.linalg.norm(axis))
    a, b = faces[int(np.argmin(proj_axis))], faces[int(np.argmax(proj_axis))]
    if a == b:
        return []

    adjacency = mesh.face_adjacency()
    seed_pos = {0: pos[a], 1: pos[b]}
    label: dict[int, int] = {a: 0, b: 1}
    pq: list[tuple[float, int, int]] = []

    def push(f, lab):
        for nb, eid in adjacency[f]:
            if nb in fset and nb not in label and eid not in seams:
                heapq.heappush(pq, (float(np.linalg.norm(pos[nb] - seed_pos[lab])), nb, lab))

    push(a, 0)
    push(b, 1)
    while pq:
        _, f, lab = heapq.heappop(pq)
        if f in label:
            continue
        label[f] = lab
        push(f, lab)
    # Connectivity fallback for any face pinched off by seams.
    leftover = [f for f in faces if f not in label]
    progressed = True
    while leftover and progressed:
        progressed = False
        still = []
        for f in leftover:
            lab = next((label[nb] for nb, _ in adjacency[f] if nb in label), None)
            if lab is None:
                still.append(f)
            else:
                label[f] = lab
                progressed = True
        leftover = still
    for f in leftover:
        label[f] = 0

    ga = [f for f in faces if label[f] == 0]
    gb = [f for f in faces if label[f] == 1]
    if not ga or not gb:
        return []
    new_seams = []
    for f in faces:
        for nb, eid in adjacency[f]:
            if nb in fset and eid not in seams and label[f] != label.get(nb) and f < nb:
                new_seams.append(eid)
    return new_seams


def repair_shapes(
    mesh: MeshGraph,
    seams: set[int],
    *,
    convexity_min: float = 0.70,
    fold_angle: float = 90.0,
    max_charts: int = 60,
    max_rounds: int = 5,
    min_chart_faces: int = 5,
) -> dict:
    """Iterate the U1.6 ops to a fixed point (chart-UV plan §5b): concavity-split every
    chart below ``convexity_min`` into compact halves, amputate tendrils, then re-run the
    absorb + merge passes. Mutates ``seams``. Returns a per-round history with the
    convexity before/after so any change is auditable."""
    from chart_uv_agent.segmentation import _absorb_small_charts

    history: list[dict] = []

    for rnd in range(max_rounds):
        charts = _charts_from_seams(mesh, seams)
        conv_before = float(np.mean([chart_convexity(mesh, fs) for fs in charts]))
        split = 0

        for fs in sorted(charts, key=lambda c: chart_convexity(mesh, c)):
            if len(_charts_from_seams(mesh, seams)) >= max_charts:
                break
            if chart_convexity(mesh, fs) >= convexity_min or len(fs) < 2 * min_chart_faces:
                continue
            new_seams = geometric_bisect(mesh, fs, seams)
            if not new_seams:
                continue
            cand = seams | set(new_seams)
            halves = [c for c in _charts_from_seams(mesh, cand) if set(c) & set(fs)]
            # Keep the split only if it yields two valid disks that are each MORE convex
            # than the parent (compactness improved), not just differently-shaped.
            if len(halves) == 2 and all(is_disk(mesh, h) and len(h) >= min_chart_faces for h in halves):
                if min(chart_convexity(mesh, h) for h in halves) > chart_convexity(mesh, fs) + 1e-3:
                    seams.update(new_seams)
                    split += 1

        _amputate_tendrils(mesh, seams, fold_angle=fold_angle, min_chart_faces=min_chart_faces)
        _absorb_small_charts(mesh, seams, fold_angle=fold_angle, min_chart_faces=min_chart_faces)
        # NOTE: no merge pass here. Re-merging after a concavity split is antagonistic —
        # two convex developable pieces merge back into a bigger (still "convex") region
        # that is no longer developable, exploding unwrap stretch. The split only adds
        # charts (well under the 60 cap), so leaving them split is correct.

        charts = _charts_from_seams(mesh, seams)
        conv_after = float(np.mean([chart_convexity(mesh, fs) for fs in charts]))
        history.append({"round": rnd, "charts": len(charts), "splits": split,
                        "convexity_before": round(conv_before, 4),
                        "convexity_after": round(conv_after, 4)})
        if split == 0 or conv_after <= conv_before + 1e-3:
            break
    return {"history": history}


def _convex_merge(mesh: MeshGraph, seams: set[int], *, fold_angle: float,
                  convexity_min: float) -> None:
    """Greedily dissolve a non-mandatory shared boundary only when the union stays a
    convex disk (union convexity ≥ ``convexity_min`` AND ≥ both parents) — squeezes the
    chart count after over-eager splits WITHOUT recombining concavity-split pieces."""
    mandatory = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    changed = True
    while changed:
        changed = False
        charts = _charts_from_seams(mesh, seams)
        face_chart = {f: cid for cid, fs in enumerate(charts) for f in fs}
        chart_faces = {cid: fs for cid, fs in enumerate(charts)}
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
            if not is_disk(mesh, union):
                continue
            cu = chart_convexity(mesh, union)
            if cu >= convexity_min and cu >= min(chart_convexity(mesh, chart_faces[ca]),
                                                 chart_convexity(mesh, chart_faces[cb])) - 1e-3:
                seams.difference_update(edges)
                changed = True
                break


def tail_round(
    mesh: MeshGraph,
    seams: set[int],
    *,
    convexity_bar: float = 0.55,
    fold_angle: float = 90.0,
    max_charts: int = 60,
    max_rounds: int = 6,
    cone_limit: float = 150.0,
    min_chart_faces: int = 5,
) -> dict:
    """Phase U1.7 — the FINAL shape round (chart-UV plan §5c). Targets the worst-decile
    charts: iterate until every chart with convexity < ``convexity_bar`` is fixed or
    provably stuck. For each below-bar chart try, in order:

      (a) concavity cut — bisect; accept if both halves are disks ≥5 faces and the worse
          half is more convex than the parent (improves the tail);
      (b) spike donation — give the chart's thin protruding faces to the neighbour they
          border most (donor stays a disk ≥5 faces, receiver convexity stays ≥ bar);
      (c) convex merge — merge with a neighbour if the union is MORE convex than this
          chart and still developable (cone ≤ ``cone_limit``, the stretch proxy).

    A chart where all three are rejected by the invariants (disk / ≥5-face / R2 / stretch)
    is reported ``stuck`` with the reason. R2 folds are never crossed. Returns a history
    + the stuck list. This is the last shape round — ship whatever it yields."""
    from chart_uv_agent.segmentation import _face_normals

    mandatory = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    normals = _face_normals(mesh)
    history: list[dict] = []
    stuck: list[dict] = []

    def global_p10(ss):
        cvs = [chart_convexity(mesh, c) for c in _charts_from_seams(mesh, ss)]
        return float(np.percentile(cvs, 10)) if cvs else 1.0

    best_seams = set(seams)
    best_p10 = global_p10(seams)

    for rnd in range(max_rounds):
        charts = _charts_from_seams(mesh, seams)
        below = [fs for fs in charts if chart_convexity(mesh, fs) < convexity_bar]
        if not below:
            break
        progressed = False
        stuck = []
        for fs in sorted(below, key=lambda c: chart_convexity(mesh, c)):
            n_charts = len(_charts_from_seams(mesh, seams))
            cv = chart_convexity(mesh, fs)
            # Merge FIRST: absorbing a below-bar chart into a convex neighbour removes it
            # outright, which lifts the tail; splitting often just yields more below-bar
            # pieces. Then donation, then cut.
            move = (_try_convex_merge(mesh, fs, seams, mandatory, normals, cone_limit,
                                      min_chart_faces, convexity_bar)
                    or _try_spike_donation(mesh, fs, seams, mandatory, convexity_bar, min_chart_faces)
                    or _try_concavity_cut(mesh, fs, seams, min_chart_faces, max_charts, n_charts))
            if move:
                seams.clear()
                seams.update(move)
                progressed = True
                break  # re-flood: chart ids changed
            stuck.append({"size": len(fs), "convexity": round(cv, 3),
                          "reason": "concavity-cut/spike-donation/convex-merge all rejected by "
                                    "disk/≥5-face/R2/stretch invariants"})
        cur_p10 = global_p10(seams)
        if cur_p10 > best_p10 + 1e-6:
            best_p10, best_seams = cur_p10, set(seams)
        history.append({"round": rnd, "charts": len(_charts_from_seams(mesh, seams)),
                        "below_bar": len(below), "stuck": len(stuck),
                        "convexity_p10": round(cur_p10, 4)})
        if not progressed:
            break  # everything left is stuck

    # Keep the best-p10 state (a move must never regress the tail).
    seams.clear()
    seams.update(best_seams)
    return {"history": history, "stuck": stuck, "convexity_bar": convexity_bar,
            "convexity_p10": round(best_p10, 4), "stuck_count": len(stuck)}


def _try_concavity_cut(mesh, fs, seams, min_faces, max_charts, n_charts) -> set[int] | None:
    if n_charts >= max_charts or len(fs) < 2 * min_faces:
        return None
    new = geometric_bisect(mesh, fs, seams)
    if not new:
        return None
    cand = seams | set(new)
    halves = [c for c in _charts_from_seams(mesh, cand) if set(c) & set(fs)]
    if len(halves) != 2 or not all(is_disk(mesh, h) and len(h) >= min_faces for h in halves):
        return None
    if min(chart_convexity(mesh, h) for h in halves) > chart_convexity(mesh, fs) + 1e-3:
        return cand
    return None


def _try_spike_donation(mesh, fs, seams, mandatory, convexity_bar, min_faces) -> set[int] | None:
    """Donate the chart's thin protruding faces to the neighbour they border most."""
    from chart_uv_agent.shape import thin_faces

    fset = set(fs)
    spikes = thin_faces(mesh, fset, seams)
    if not spikes or len(fset) - len(spikes) < min_faces:
        return None
    adjacency = mesh.face_adjacency()
    charts = _charts_from_seams(mesh, seams)
    face_chart = {f: i for i, c in enumerate(charts) for f in c}
    # The neighbour chart the spikes border most (across a non-mandatory seam).
    by_nb: dict[int, list[int]] = {}
    for f in spikes:
        for nb, eid in adjacency[f]:
            nc = face_chart.get(nb)
            if nc is not None and nb not in fset and eid in seams and eid not in mandatory:
                by_nb.setdefault(nc, []).append(eid)
    if not by_nb:
        return None
    target = max(by_nb, key=lambda k: len(by_nb[k]))
    # Move the spike faces bordering the target into it: seam off spike↔donor edges,
    # dissolve spike↔target edges.
    donor_rest = fset - spikes
    if not _connected_faces(mesh, donor_rest, adjacency) or not is_disk(mesh, donor_rest) or len(donor_rest) < min_faces:
        return None
    receiver = set(charts[target]) | spikes
    if not is_disk(mesh, receiver) or chart_convexity(mesh, receiver) < convexity_bar:
        return None
    cand = set(seams)
    cand.difference_update(by_nb[target])                      # spike↔target → interior
    for f in spikes:                                           # spike↔donor → seam
        for nb, eid in adjacency[f]:
            if nb in donor_rest and eid not in mandatory:
                cand.add(eid)
    return cand


def _try_convex_merge(mesh, fs, seams, mandatory, normals, cone_limit, min_faces, convexity_bar) -> set[int] | None:
    """Merge the below-bar chart into a neighbour when the union is a developable disk
    whose convexity reaches the bar (cleanly absorbing the bad chart) or strictly
    improves on it. Developability (cone ≤ ``cone_limit``) is the stretch guard."""
    from chart_uv_agent.segmentation import normal_cone_halfangle

    fset = set(fs)
    adjacency = mesh.face_adjacency()
    charts = _charts_from_seams(mesh, seams)
    face_chart = {f: i for i, c in enumerate(charts) for f in c}
    by_nb: dict[int, list[int]] = {}
    for f in fs:
        for nb, eid in adjacency[f]:
            nc = face_chart.get(nb)
            if nc is not None and nb not in fset and eid in seams and eid not in mandatory:
                by_nb.setdefault(nc, []).append(eid)
    cv = chart_convexity(mesh, fs)
    best = None
    for nc, edges in by_nb.items():
        union = list(fset | set(charts[nc]))
        if not is_disk(mesh, union):
            continue
        if normal_cone_halfangle(mesh, union, normals) > cone_limit:  # would explode stretch
            continue
        ucv = chart_convexity(mesh, union)
        if ucv >= convexity_bar or ucv > cv + 1e-3:
            # Prefer the merge that yields the most convex union.
            if best is None or ucv > best[0]:
                best = (ucv, edges)
    return (seams - set(best[1])) if best else None


def _amputate_tendrils(mesh: MeshGraph, seams: set[int], *, fold_angle: float,
                       min_chart_faces: int) -> int:
    """Cut tendril chains (width ≤2 finger faces, >4 long) at the base and let the next
    absorb fold them into the neighbour (chart-UV plan §5b op2). Mandatory seams are
    untouched. Returns the number of tendrils cut. (Near-no-op on these meshes — they
    have 0 tendrils after absorb — but kept for robustness on jaggier inputs.)"""
    mandatory = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    adjacency = mesh.face_adjacency()
    cut = 0
    for fs in _charts_from_seams(mesh, seams):
        for chain in tendril_chains(mesh, set(fs), seams):
            base = set(fs) - set(chain)
            if not base:
                continue
            # Seam off the chain from the chart body so absorb can re-home it.
            for f in chain:
                for nb, eid in adjacency[f]:
                    if nb in base and eid not in mandatory:
                        seams.add(eid)
            cut += 1
    return cut
