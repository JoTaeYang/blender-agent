"""Minimal Wavefront OBJ loader -> MeshGraph (no Blender required).

Supports ``v`` positions and ``f`` faces (``v``, ``v/vt``, ``v//vn``,
``v/vt/vn``), and tracks ``usemtl`` as per-face material indices. Enough to run
the engine and tests on real OBJ assets offline.
"""

from __future__ import annotations

import os

from uv_agent.geometry.mesh_graph import MeshGraph


def load_obj(path: str, object_id: str | None = None) -> MeshGraph:
    vertices: list[tuple[float, float, float]] = []
    faces: list[list[int]] = []
    materials: list[int] = []
    mat_names: dict[str, int] = {}
    cur_mat = 0

    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if not line or line[0] == "#":
                continue
            parts = line.split()
            if not parts:
                continue
            tag = parts[0]
            if tag == "v":
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif tag == "usemtl":
                name = parts[1] if len(parts) > 1 else "default"
                if name not in mat_names:
                    mat_names[name] = len(mat_names)
                cur_mat = mat_names[name]
            elif tag == "f":
                idx = []
                for token in parts[1:]:
                    v = token.split("/")[0]
                    if not v:
                        continue
                    vi = int(v)
                    # OBJ is 1-based; negative indices are relative to current end.
                    idx.append(vi - 1 if vi > 0 else len(vertices) + vi)
                if len(idx) >= 3:
                    faces.append(idx)
                    materials.append(cur_mat)

    oid = object_id or os.path.splitext(os.path.basename(path))[0]
    return MeshGraph.from_faces(oid, vertices, faces, material_indices=materials)
