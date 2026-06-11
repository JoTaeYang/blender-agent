"""Initial UV coordinate generation per island (plan §7.3 / Phase 4).

MVP approach: deterministic projection unwraps (planar + cylindrical). These
produce the *initial* UVs that relaxation and packing then refine. Blender's
native unwrap result can also be imported as an initial solution via the
adapter; the rest of the pipeline does not care where the seed UVs came from.
"""

from __future__ import annotations

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap


def island_vertex_ids(mesh: MeshGraph, face_ids: list[int]) -> list[int]:
    seen: dict[int, None] = {}
    for fid in face_ids:
        for vid in mesh.faces[fid].vertex_ids:
            seen.setdefault(vid, None)
    return list(seen.keys())


def _area_weighted_normal(mesh: MeshGraph, face_ids: list[int]) -> np.ndarray:
    n = np.zeros(3)
    for fid in face_ids:
        f = mesh.faces[fid]
        n += np.asarray(f.normal) * f.area_3d
    norm = np.linalg.norm(n)
    if norm < 1e-9:
        # Degenerate (normals cancel): fall back to the largest face's normal.
        biggest = max(face_ids, key=lambda fid: mesh.faces[fid].area_3d)
        return np.asarray(mesh.faces[biggest].normal, dtype=float)
    return n / norm


def _basis_from_normal(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    up = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(up, normal))) > 0.95:
        up = np.array([0.0, 1.0, 0.0])
    tangent = np.cross(up, normal)
    tangent /= np.linalg.norm(tangent)
    bitangent = np.cross(normal, tangent)
    return tangent, bitangent


def project_island_planar(mesh: MeshGraph, face_ids: list[int], uvmap: UVMap) -> None:
    """Orthographic projection onto the island's average plane.

    Loops sharing a mesh vertex receive identical UVs (the projection is a pure
    function of 3D position), so the island stays connected in UV space.
    """
    if not face_ids:
        return
    normal = _area_weighted_normal(mesh, face_ids)
    tangent, bitangent = _basis_from_normal(normal)
    vids = island_vertex_ids(mesh, face_ids)
    origin = np.mean([mesh.vertex_co(v) for v in vids], axis=0)
    for fid in face_ids:
        for loop_index in mesh.faces[fid].loop_indices:
            p = mesh.vertex_co(mesh.loops[loop_index].vertex_id) - origin
            uvmap.set(loop_index, float(np.dot(p, tangent)), float(np.dot(p, bitangent)))


def project_island_cylindrical(
    mesh: MeshGraph, face_ids: list[int], uvmap: UVMap, axis: str = "z"
) -> None:
    """Cylindrical projection around ``axis`` (good for tubes / bolt rings)."""
    if not face_ids:
        return
    axis_idx = {"x": 0, "y": 1, "z": 2}[axis]
    a, b = [i for i in range(3) if i != axis_idx]
    vids = island_vertex_ids(mesh, face_ids)
    center = np.mean([mesh.vertex_co(v) for v in vids], axis=0)
    # Estimate radius so u uses real arc length (radius * angle). Using arc
    # length and raw height preserves the tube's aspect ratio -> low stretch.
    radius = float(np.mean([np.hypot(mesh.vertex_co(v)[a] - center[a],
                                     mesh.vertex_co(v)[b] - center[b]) for v in vids]))
    radius = radius or 1.0
    for fid in face_ids:
        loop_indices = mesh.faces[fid].loop_indices
        angles = []
        vs = []
        for loop_index in loop_indices:
            co = mesh.vertex_co(mesh.loops[loop_index].vertex_id)
            angles.append(float(np.arctan2(co[b] - center[b], co[a] - center[a])))  # -pi..pi
            vs.append(float(co[axis_idx]))
        # Seam unwrap: faces straddling the +/-pi wrap span the whole circle
        # backward (reads as a fold). Shift low angles by +2pi so each face stays
        # contiguous; this introduces the expected vertical UV seam.
        if max(angles) - min(angles) > np.pi:
            angles = [ang + 2 * np.pi if ang < 0 else ang for ang in angles]
        for loop_index, ang, v in zip(loop_indices, angles, vs):
            uvmap.set(loop_index, ang * radius, v)


def _order_quad_strip(mesh: MeshGraph, face_ids: list[int]):
    """If the island is a chain (or loop) of quads, return the ordered face list
    plus per-step shared-edge vertex sets. Otherwise return None.

    A quad strip is a sequence of 4-gons where each face touches <= 2 island
    neighbours (degree-2 path, or a closed loop)."""
    fset = set(face_ids)
    if len(face_ids) < 2:
        return None
    if any(len(mesh.faces[f].vertex_ids) != 4 for f in face_ids):
        return None
    full_adj = mesh.face_adjacency()
    adj = {f: [(n, e) for (n, e) in full_adj[f] if n in fset] for f in face_ids}
    deg = {f: len(adj[f]) for f in face_ids}
    if any(d > 2 for d in deg.values()):
        return None
    ends = [f for f in face_ids if deg[f] == 1]
    n = len(face_ids)
    if len(ends) == 2:
        start = ends[0]
    elif len(ends) == 0 and all(deg[f] == 2 for f in face_ids):
        start = face_ids[0]  # closed loop: cut at an arbitrary face
    else:
        return None
    # Walk the chain.
    order = [start]
    shared = []  # shared edge id between order[k] and order[k+1]
    prev = None
    cur = start
    while len(order) < n:
        nxts = [(nb, e) for (nb, e) in adj[cur] if nb != prev]
        if not nxts:
            break
        nb, e = nxts[0]
        shared.append(e)
        order.append(nb)
        prev, cur = cur, nb
    if len(order) != n:
        return None
    return order, shared


def project_island_strip(mesh: MeshGraph, face_ids: list[int], uvmap: UVMap) -> bool:
    """Unroll a quad strip into a straight ribbon (arc length along U, width
    along V). Straightens curved bevel/cylinder bands that planar projection
    would leave bent. Returns False if the island is not a clean quad strip."""
    walked = _order_quad_strip(mesh, face_ids)
    if walked is None:
        return False
    order, shared = walked

    def co(v):
        return mesh.vertex_co(v)

    def dist(a, b):
        return float(np.linalg.norm(co(a) - co(b)))

    # First face: incoming rung = edge opposite to the shared edge with face[1].
    loop0 = mesh.faces[order[0]].vertex_ids
    e0 = set(mesh.edges[shared[0]].vertex_ids)
    i = next((k for k in range(4) if {loop0[k], loop0[(k + 1) % 4]} == e0), None)
    if i is None:
        return False
    L_in, R_in = loop0[(i + 2) % 4], loop0[(i + 3) % 4]

    pos: dict[int, tuple[float, float]] = {}
    u_cur = 0.0
    vR = dist(L_in, R_in)
    pos[L_in] = (0.0, 0.0)
    pos[R_in] = (0.0, vR)

    for k, f in enumerate(order):
        loop = mesh.faces[f].vertex_ids
        quad_edges = {frozenset((loop[j], loop[(j + 1) % 4])) for j in range(4)}
        out = [v for v in loop if v != L_in and v != R_in]
        if len(out) != 2:
            return False
        if frozenset((L_in, out[0])) in quad_edges:
            L_out, R_out = out[0], out[1]
        elif frozenset((L_in, out[1])) in quad_edges:
            L_out, R_out = out[1], out[0]
        else:
            return False
        # The computed outgoing rung must match the real shared edge with next.
        if k < len(shared) and {L_out, R_out} != set(mesh.edges[shared[k]].vertex_ids):
            return False
        rail = (dist(L_in, L_out) + dist(R_in, R_out)) / 2.0
        u_next = u_cur + rail
        vR_out = dist(L_out, R_out)
        pos[L_out] = (u_next, 0.0)
        pos[R_out] = (u_next, vR_out)
        L_in, R_in, u_cur, vR = L_out, R_out, u_next, vR_out

    # The unroll can come out mirrored (clockwise). Match the face loop winding
    # so signed UV area stays positive, like planar projection, otherwise the
    # evaluator reads every face as folded.
    first = mesh.faces[order[0]].vertex_ids
    area2 = 0.0
    for j in range(len(first)):
        x1, y1 = pos[first[j]]
        x2, y2 = pos[first[(j + 1) % len(first)]]
        area2 += x1 * y2 - x2 * y1
    if area2 < 0:
        pos = {v: (u, -w) for v, (u, w) in pos.items()}

    # Write per-vertex UV to every island loop of that vertex.
    for f in face_ids:
        for li in mesh.faces[f].loop_indices:
            vid = mesh.loops[li].vertex_id
            if vid in pos:
                uvmap.set(li, pos[vid][0], pos[vid][1])
    return True


def _signed_area_loop(uvs) -> float:
    s = 0.0
    n = len(uvs)
    for i in range(n):
        x1, y1 = uvs[i]
        x2, y2 = uvs[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s


def project_island_grid(mesh: MeshGraph, face_ids: list[int], uvmap: UVMap) -> bool:
    """Unroll a quad *grid* patch (M x N curved band/sheet) into a straight
    rectangle, the 2D generalization of :func:`project_island_strip` (Follow
    Active Quads). Assigns each vertex integer grid coords by BFS over the quad
    lattice, then lays them out with averaged real row/column lengths.

    Returns False if the island is not a consistent quad grid.
    """
    if len(face_ids) < 2:
        return False
    if any(len(mesh.faces[f].vertex_ids) != 4 for f in face_ids):
        return False
    fset = set(face_ids)
    full_adj = mesh.face_adjacency()
    adj = {f: [n for (n, e) in full_adj[f] if n in fset] for f in face_ids}

    gv: dict[int, tuple[int, int]] = {}
    seed = face_ids[0]
    sl = mesh.faces[seed].vertex_ids
    gv[sl[0]], gv[sl[1]], gv[sl[2]], gv[sl[3]] = (0, 0), (1, 0), (1, 1), (0, 1)

    from collections import deque

    placed = {seed}
    queue = deque([seed])

    def rot90(v):
        return (-v[1], v[0])

    while queue:
        f = queue.popleft()
        loop = mesh.faces[f].vertex_ids
        edge = None
        for k in range(4):
            a, b = loop[k], loop[(k + 1) % 4]
            if a in gv and b in gv:
                edge = (k, a, b)
                break
        if edge is None:
            return False
        k, a, b = edge
        ga, gb = gv[a], gv[b]
        e = (gb[0] - ga[0], gb[1] - ga[1])
        if abs(e[0]) + abs(e[1]) != 1:  # must be a unit grid step
            return False
        p = rot90(e)
        c, d = loop[(k + 2) % 4], loop[(k + 3) % 4]
        gc, gd = (gb[0] + p[0], gb[1] + p[1]), (ga[0] + p[0], ga[1] + p[1])
        for vid, gg in ((c, gc), (d, gd)):
            if vid in gv:
                if gv[vid] != gg:
                    return False  # inconsistent -> not a clean grid
            else:
                gv[vid] = gg
        for n in adj[f]:
            if n not in placed:
                placed.add(n)
                queue.append(n)

    all_verts = {v for f in face_ids for v in mesh.faces[f].vertex_ids}
    if not all_verts.issubset(gv):
        return False

    # Averaged real lengths per column (i) and row (j) step.
    from collections import defaultdict

    istep, jstep = defaultdict(list), defaultdict(list)
    for f in face_ids:
        loop = mesh.faces[f].vertex_ids
        for k in range(4):
            a, b = loop[k], loop[(k + 1) % 4]
            ga, gb = gv[a], gv[b]
            dvec = (gb[0] - ga[0], gb[1] - ga[1])
            length = float(np.linalg.norm(mesh.vertex_co(a) - mesh.vertex_co(b)))
            if dvec[1] == 0:
                istep[min(ga[0], gb[0])].append(length)
            else:
                jstep[min(ga[1], gb[1])].append(length)

    imin = min(g[0] for g in gv.values())
    imax = max(g[0] for g in gv.values())
    jmin = min(g[1] for g in gv.values())
    jmax = max(g[1] for g in gv.values())
    avg_i = np.mean([v for vs in istep.values() for v in vs]) if istep else 1.0
    avg_j = np.mean([v for vs in jstep.values() for v in vs]) if jstep else 1.0

    u_of = {imin: 0.0}
    for i in range(imin, imax):
        seg = float(np.mean(istep[i])) if istep[i] else float(avg_i)
        u_of[i + 1] = u_of[i] + seg
    v_of = {jmin: 0.0}
    for j in range(jmin, jmax):
        seg = float(np.mean(jstep[j])) if jstep[j] else float(avg_j)
        v_of[j + 1] = v_of[j] + seg

    pos = {vid: (u_of[g[0]], v_of[g[1]]) for vid, g in gv.items()}

    # Match face winding (avoid the layout coming out mirrored/folded).
    first = mesh.faces[seed].vertex_ids
    if _signed_area_loop([pos[v] for v in first]) < 0:
        pos = {v: (u, -w) for v, (u, w) in pos.items()}

    for f in face_ids:
        for li in mesh.faces[f].loop_indices:
            vid = mesh.loops[li].vertex_id
            uvmap.set(li, pos[vid][0], pos[vid][1])
    return True


def project_island(mesh: MeshGraph, face_ids: list[int], uvmap: UVMap, projection: str) -> None:
    if projection == "cylindrical":
        project_island_cylindrical(mesh, face_ids, uvmap)
    elif projection == "strip":
        if not project_island_strip(mesh, face_ids, uvmap):
            project_island_planar(mesh, face_ids, uvmap)
    else:
        # Default: straighten quad strips/grids (arc -> straight), else planar.
        if project_island_strip(mesh, face_ids, uvmap):
            return
        if project_island_grid(mesh, face_ids, uvmap):
            return
        project_island_planar(mesh, face_ids, uvmap)
