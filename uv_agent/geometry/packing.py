"""Island packing into the [0,1] UV square (plan §7 / Phase 5).

MVP packer: per-island bounding box, optional 90 deg rotation, then shelf
packing with a binary-searched global scale so all islands share one scale
(preserving relative texel density) and fit inside the unit square with the
requested padding. Shelf placement guarantees non-overlapping bounding boxes.
"""

from __future__ import annotations

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import IslandTransform, UVMap
from uv_agent.planner.island_planner import Island, IslandPlan

_MIN_SIZE = 1e-6


def _rotate90(p: np.ndarray) -> np.ndarray:
    # (u, v) -> (-v, u)
    out = np.empty_like(p)
    out[:, 0] = -p[:, 1]
    out[:, 1] = p[:, 0]
    return out


def _shelf_pack(sizes: list[tuple[float, float]], scale: float, padding: float):
    """Place rectangles left-to-right into width 1.0, wrapping to new shelves.
    Returns positions (in original order) or None if it does not fit."""
    order = sorted(range(len(sizes)), key=lambda i: sizes[i][1], reverse=True)
    positions = [(0.0, 0.0)] * len(sizes)
    x = padding
    y = padding
    shelf_h = 0.0
    for i in order:
        w, h = sizes[i]
        sw, sh = w * scale, h * scale
        if sw + 2 * padding > 1.0 or sh + 2 * padding > 1.0:
            return None  # single island too big even alone
        if x + sw + padding > 1.0:  # wrap to next shelf
            x = padding
            y += shelf_h + padding
            shelf_h = 0.0
        positions[i] = (x, y)
        x += sw + padding
        shelf_h = max(shelf_h, sh)
    if y + shelf_h + padding > 1.0:
        return None
    return positions


def pack_islands(
    mesh: MeshGraph,
    plan: IslandPlan,
    uvmap: UVMap,
    *,
    padding: float | None = None,
    allow_rotate: bool = True,
) -> list[IslandTransform]:
    """Lay out every island inside [0,1]^2. Mutates ``uvmap`` in place."""
    if padding is None:
        padding = plan.constraints.padding_uv
    padding = float(np.clip(padding, 0.0, 0.1))

    islands: list[Island] = [i for i in plan.islands if i.face_ids]
    if not islands:
        return []

    # Per-island: rotate (optional), translate to local origin, record footprint.
    locals_: list[np.ndarray] = []  # local coords per island, same order as `islands`
    loop_lists: list[list[int]] = []
    sizes: list[tuple[float, float]] = []
    rotations: list[float] = []

    for isl in islands:
        loop_indices = [li for fid in isl.face_ids for li in mesh.faces[fid].loop_indices]
        p = uvmap.uv[loop_indices].copy()
        rot = 0.0
        w = p[:, 0].max() - p[:, 0].min()
        h = p[:, 1].max() - p[:, 1].min()
        if allow_rotate and h > w:  # prefer landscape for shelf packing
            p = _rotate90(p)
            rot = 90.0
        p -= p.min(axis=0)  # translate min corner to origin
        w = max(_MIN_SIZE, float(p[:, 0].max()))
        h = max(_MIN_SIZE, float(p[:, 1].max()))
        locals_.append(p)
        loop_lists.append(loop_indices)
        sizes.append((w, h))
        rotations.append(rot)

    # Binary search the largest global scale that still fits.
    lo, hi = 0.0, 1.0
    best = _shelf_pack(sizes, lo + 1e-9, padding)
    for _ in range(48):
        mid = (lo + hi) / 2
        placed = _shelf_pack(sizes, mid, padding)
        if placed is not None:
            best = placed
            lo = mid
        else:
            hi = mid
    scale = lo
    positions = best if best is not None else [(padding, padding)] * len(islands)

    transforms: list[IslandTransform] = []
    for idx, isl in enumerate(islands):
        px, py = positions[idx]
        packed = locals_[idx] * scale + np.array([px, py])
        uvmap.uv[loop_lists[idx]] = packed
        transforms.append(
            IslandTransform(
                island_id=isl.island_id,
                rotation_deg=rotations[idx],
                scale=scale,
                translation=(float(px), float(py)),
            )
        )
    return transforms


def island_bbox(mesh: MeshGraph, island: Island, uvmap: UVMap) -> tuple[float, float, float, float]:
    loop_indices = [li for fid in island.face_ids for li in mesh.faces[fid].loop_indices]
    p = uvmap.uv[loop_indices]
    return (
        float(p[:, 0].min()),
        float(p[:, 1].min()),
        float(p[:, 0].max()),
        float(p[:, 1].max()),
    )
