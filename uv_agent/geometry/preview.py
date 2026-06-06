"""UV layout preview as SVG (plan Phase 6 "preview image").

Renders the packed UV islands into an SVG so a result can be inspected/compared
without a Blender render. The Blender worker can additionally produce a textured
PNG render, but this works everywhere the pure-Python engine runs.
"""

from __future__ import annotations

from uv_agent.geometry.mesh_graph import MeshGraph
from uv_agent.geometry.solution import UVMap
from uv_agent.planner.island_planner import IslandPlan

# Distinct, readable island colors (cycled).
_PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]


def uv_layout_svg(
    mesh: MeshGraph,
    plan: IslandPlan,
    uvmap: UVMap,
    *,
    size: int = 512,
    title: str | None = None,
) -> str:
    """Return an SVG string of the [0,1] UV square with island-colored faces."""
    pad = 16
    inner = size

    def sx(u: float) -> float:
        return pad + u * inner

    def sy(v: float) -> float:
        # SVG y grows downward; flip so v=0 is at the bottom.
        return pad + (1.0 - v) * inner

    parts: list[str] = []
    w = size + 2 * pad
    h = size + 2 * pad + (24 if title else 0)
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">'
    )
    parts.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="#1e1e1e"/>')
    # UV unit square + 0.5 grid lines.
    parts.append(
        f'<rect x="{pad}" y="{pad}" width="{inner}" height="{inner}" '
        f'fill="#2b2b2b" stroke="#555" stroke-width="1"/>'
    )
    for t in (0.25, 0.5, 0.75):
        parts.append(
            f'<line x1="{sx(t)}" y1="{pad}" x2="{sx(t)}" y2="{pad + inner}" stroke="#3a3a3a"/>'
        )
        parts.append(
            f'<line x1="{pad}" y1="{sy(t)}" x2="{pad + inner}" y2="{sy(t)}" stroke="#3a3a3a"/>'
        )

    for idx, isl in enumerate(plan.islands):
        if not isl.face_ids:
            continue
        color = _PALETTE[idx % len(_PALETTE)]
        for fid in isl.face_ids:
            loops = mesh.faces[fid].loop_indices
            pts = " ".join(
                f"{sx(uvmap.get(li)[0]):.2f},{sy(uvmap.get(li)[1]):.2f}" for li in loops
            )
            parts.append(
                f'<polygon points="{pts}" fill="{color}" fill-opacity="0.55" '
                f'stroke="{color}" stroke-width="1"/>'
            )

    if title:
        parts.append(
            f'<text x="{pad}" y="{h - 8}" fill="#ddd" font-family="monospace" '
            f'font-size="13">{_escape(title)}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
