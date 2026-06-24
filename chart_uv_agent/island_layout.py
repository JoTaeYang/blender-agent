"""Island-level UV layout repacking (MVP3_EXISTING_UV_REPACK_OPTIMIZATION_PLAN Goal B / Step 3).

The Blender default ``pack_islands`` barely changes a layout that already came from an
existing UV boundary, so the layout-optimization loop's gains were negligible (plan §0).
This module adds the missing post-pass: on a FIXED seam set it groups the read-back UVs
into islands, normalizes per-island texel density, optionally orients long strip/ring
islands to their principal axis, then re-packs with the geometry-level MaxRects / shelf
packer (:mod:`uv_agent.geometry.packing`) — which fills gaps far better than Blender's
``pack_islands`` and so reaches a much higher packing efficiency (plan §2 Goal B).

Two layers, mirroring the rest of the chart engine:

- PURE (Blender-free, unit-tested): the per-island density summary, the density
  normalization, and the orientation pass all operate on a :class:`MeshGraph` + a
  per-loop :class:`UVMap`, so they unit-test without ``bpy``.
- DRIVER (:func:`repack_uv_islands_custom`): reads the object's active UV layer, runs the
  pure passes, re-packs, and writes the UVs back. The seam set is FIXED — this never
  adds, removes, or re-segments a seam (plan §1.2).
"""

from __future__ import annotations

import numpy as np

from uv_agent.geometry.evaluation import _tri_signed_area_uv, _tris_from_face
from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap

# Aspect ratio above which an island is considered a long strip/ring worth orienting.
DEFAULT_ORIENT_ASPECT_MIN = 2.0


def _island_loops(mesh: MeshGraph, faces) -> list[int]:
    return [li for fid in faces for li in mesh.faces[fid].loop_indices]


def _island_uv_area(mesh: MeshGraph, uvmap: UVMap, faces) -> float:
    """Absolute UV area of an island (sum of |triangle signed UV area|)."""
    total = 0.0
    for fid in faces:
        for l0, l1, l2 in _tris_from_face(mesh.faces[fid].loop_indices):
            total += abs(_tri_signed_area_uv(uvmap.get(l0), uvmap.get(l1), uvmap.get(l2)))
    return total


def island_density_summary(mesh: MeshGraph, uvmap: UVMap, islands_faces) -> list[dict]:
    """Per-island bbox / UV area / 3D area / texel density (plan §2 Goal B detail 2).

    ``density`` is ``area_uv / area_3d`` — the squared texel density. Islands with no
    loops or zero 3D area are skipped."""
    rows: list[dict] = []
    for faces in islands_faces:
        loops = _island_loops(mesh, faces)
        if not loops:
            continue
        p = uvmap.uv[loops]
        area_uv = _island_uv_area(mesh, uvmap, faces)
        area_3d = float(sum(mesh.faces[f].area_3d for f in faces))
        rows.append({
            "faces": list(faces),
            "loops": loops,
            "area_uv": float(area_uv),
            "area_3d": area_3d,
            "density": (area_uv / area_3d) if area_3d > 1e-12 else 0.0,
            "bbox": (float(p[:, 0].min()), float(p[:, 1].min()),
                     float(p[:, 0].max()), float(p[:, 1].max())),
        })
    return rows


def normalize_island_density(mesh: MeshGraph, uvmap: UVMap, islands_faces) -> int:
    """Scale every island about its centroid so all share the area-weighted mean texel
    density (plan §2 Goal B detail 3 — equivalent to Blender ``average_islands_scale`` but
    applied on the read-back UVs so the custom packer sees uniformly-dense islands). Mutates
    ``uvmap`` in place; returns the number of islands rescaled."""
    rows = island_density_summary(mesh, uvmap, islands_faces)
    usable = [r for r in rows if r["area_3d"] > 1e-12 and r["density"] > 1e-12]
    if len(usable) < 2:
        return 0
    w = np.array([r["area_3d"] for r in usable], dtype=float)
    d = np.array([r["density"] for r in usable], dtype=float)
    target = float((w * d).sum() / w.sum())
    if target <= 1e-12:
        return 0
    changed = 0
    for r in usable:
        s = float(np.sqrt(target / r["density"]))
        if abs(s - 1.0) < 1e-9:
            continue
        loops = r["loops"]
        p = uvmap.uv[loops]
        c = p.mean(axis=0)
        uvmap.uv[loops] = c + (p - c) * s
        changed += 1
    return changed


def orient_islands(mesh: MeshGraph, uvmap: UVMap, islands_faces, *,
                   aspect_min: float = DEFAULT_ORIENT_ASPECT_MIN) -> int:
    """Rotate long strip/ring islands so their principal (PCA major) axis is horizontal
    (plan §2 Goal B detail 4). A landscape, axis-aligned strip packs into far less wasted
    bbox than a diagonal one. Only islands whose bbox aspect ratio is ``>= aspect_min`` (or
    where the rotation shrinks the bbox area) are touched, so compact islands are left as-is.
    Mutates ``uvmap`` in place; returns the number of islands rotated."""
    changed = 0
    for faces in islands_faces:
        loops = _island_loops(mesh, faces)
        if len(loops) < 3:
            continue
        p = uvmap.uv[loops]
        c = p.mean(axis=0)
        q = p - c
        ow = float(q[:, 0].max() - q[:, 0].min())
        oh = float(q[:, 1].max() - q[:, 1].min())
        long_side, short_side = max(ow, oh), max(min(ow, oh), 1e-12)
        if long_side / short_side < aspect_min:
            continue
        cov = np.cov(q.T)
        if not np.all(np.isfinite(cov)):
            continue
        evals, evecs = np.linalg.eigh(cov)
        major = evecs[:, int(np.argmax(evals))]
        ang = float(np.arctan2(major[1], major[0]))
        ca, sa = np.cos(-ang), np.sin(-ang)
        rot = q @ np.array([[ca, -sa], [sa, ca]]).T
        nw = float(rot[:, 0].max() - rot[:, 0].min())
        nh = float(rot[:, 1].max() - rot[:, 1].min())
        # Only commit if the oriented bbox is no larger than the original (it should be
        # smaller for a genuine strip; guard against a degenerate PCA on near-square noise).
        if nw * nh <= ow * oh + 1e-12:
            uvmap.uv[loops] = c + rot
            changed += 1
    return changed


def repack_uv_islands_custom(obj, mesh: MeshGraph, *, seams, padding: float,
                             algorithm: str = "maxrects", allow_rotate: bool = True,
                             density_normalize: bool = True,
                             orient_long_islands: bool = False,
                             layer_name: str | None = None) -> dict:
    """Re-pack the object's active UV layer with the custom geometry packer (plan §2 Goal B,
    §5 detail 5–6). The seam set is FIXED — islands are recovered from it, never re-cut.

    Order (plan §2 Goal B): read UVs -> per-island density normalize -> orient long islands
    -> custom MaxRects/shelf pack (single global scale, overlap-free, in-bounds) -> write
    back. ``algorithm`` is ``"maxrects"`` or ``"shelf"``. Returns a small report dict
    (islands packed / normalized / oriented) for the candidate history. Blender-only (reads
    and writes UVs via the object's UV layer)."""
    from chart_uv_agent.unwrap import island_plan_from_seams, read_uvmap
    from uv_agent.blender.organic_unwrap import AI_UV_LAYER
    from uv_agent.geometry.packing import pack_islands

    layer = layer_name or AI_UV_LAYER
    uvmap = read_uvmap(obj, mesh, layer_name=layer)
    plan = island_plan_from_seams(mesh, set(int(e) for e in seams))
    islands_faces = [isl.face_ids for isl in plan.islands if isl.face_ids]

    normalized = normalize_island_density(mesh, uvmap, islands_faces) if density_normalize else 0
    oriented = orient_islands(mesh, uvmap, islands_faces) if orient_long_islands else 0
    pack_islands(mesh, plan, uvmap, padding=float(padding), allow_rotate=bool(allow_rotate),
                 strategy=algorithm)
    _write_uvmap(obj, uvmap, layer)
    return {"algorithm": algorithm, "islands": len(islands_faces),
            "normalized": normalized, "oriented": oriented,
            "density_normalize": bool(density_normalize),
            "orient_long_islands": bool(orient_long_islands)}


def _write_uvmap(obj, uvmap: UVMap, layer_name: str) -> None:
    """Write a :class:`UVMap` back to the object's UV layer (loop order matches
    :func:`uv_agent.blender.extract.extract_mesh_graph`)."""
    layer = obj.data.uv_layers.get(layer_name) or obj.data.uv_layers.active
    flat = np.asarray(uvmap.uv[: len(layer.data)], dtype=np.float64).reshape(-1)
    layer.data.foreach_set("uv", flat)
    obj.data.update()


__all__ = ["island_density_summary", "normalize_island_density", "orient_islands",
           "repack_uv_islands_custom"]
