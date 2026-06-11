"""Topology validator (retopology plan §6.6, §10 Phase 2, Ticket 5).

Phase 2 answers, numerically, "is this low-poly usable?" -- without an LLM and
without Blender. It runs on a :class:`~uv_agent.geometry.mesh_graph.MeshGraph`,
so it validates both synthetic meshes and meshes extracted from Blender
(:func:`uv_agent.blender.extract.extract_mesh_graph`).

It measures the §6.6 items -- face count vs target, quad/triangle/n-gon counts,
quad ratio, non-manifold edges, open boundaries, valence outliers -- and reduces
them to an ``accepted`` / ``retry`` / ``failed`` verdict using the §15.6 quality
thresholds. Each gating metric records *why* it landed where it did, so the
Phase 7/8 repair loop can act on the specific failure (§15.7).

Status policy (worst gating metric wins):

    target_error_ratio  <=0.15 accepted | <=0.30 retry | >0.30 failed
    quad_ratio          >=0.98 accepted | >=0.90 retry | <0.90 failed   (if quad_required)
    triangle_ratio      <=0.02 accepted | <=0.10 retry | >0.10 failed
    ngon_count          ==0    accepted | >0 retry (cleanup possible, §15.7) (if not ngon_allowed)
    non_manifold_edges  ==0    accepted | >0 retry (repair possible, §15.7)
    open_boundary_edges ==0    accepted | >0 retry                       (only if expect_closed)

Note (plan §10 "treat n-gon discovery as failure"): an n-gon means the result is
*not accepted* -- it drops to ``retry`` and triggers repair (§15.7). The literal
"failed" band is reserved for n-gons that survive a cleanup pass, signalled via
``ngon_after_cleanup=True``. ``edge_flow_score`` is a Phase 6 metric and is
intentionally not computed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from retopo_agent.geometry.target_search import (
    ACCEPTED_ERROR,
    RETRY_ERROR,
    target_error_ratio,
)
from uv_agent.geometry.mesh_graph import MeshGraph

# §15.6 quad-ratio / triangle-ratio thresholds.
QUAD_RATIO_ACCEPTED = 0.98
QUAD_RATIO_RETRY = 0.90
TRIANGLE_RATIO_ACCEPTED = 0.02
TRIANGLE_RATIO_RETRY = 0.10

_BAND_RANK = {"accepted": 0, "retry": 1, "failed": 2}


def _worst(bands: list[str]) -> str:
    return max(bands, key=lambda b: _BAND_RANK[b]) if bands else "accepted"


def count_face_types(mesh: MeshGraph) -> tuple[int, int, int]:
    """Return ``(triangle_count, quad_count, ngon_count)`` by polygon side count."""
    tris = quads = ngons = 0
    for face in mesh.faces:
        sides = len(face.vertex_ids)
        if sides == 3:
            tris += 1
        elif sides == 4:
            quads += 1
        elif sides >= 5:
            ngons += 1
        # sides < 3 cannot occur: MeshGraph.from_faces drops degenerate faces.
    return tris, quads, ngons


def count_valence_issues(mesh: MeshGraph, *, low: int = 2, high: int = 6) -> int:
    """Interior vertices whose edge valence is extraordinary (``<=low`` or
    ``>=high``). A soft, informational quad-flow signal (ideal interior quad
    valence is 4); not part of the acceptance gate in Phase 2."""
    valence: dict[int, int] = {}
    boundary: set[int] = set()
    for e in mesh.edges:
        a, b = e.vertex_ids
        valence[a] = valence.get(a, 0) + 1
        valence[b] = valence.get(b, 0) + 1
        if e.is_boundary or e.is_non_manifold:
            boundary.add(a)
            boundary.add(b)
    issues = 0
    for vid, val in valence.items():
        if vid in boundary:
            continue
        if val <= low or val >= high:
            issues += 1
    return issues


@dataclass
class ValidationReport:
    face_count: int
    target_face_count: int
    triangle_count: int
    quad_count: int
    ngon_count: int
    non_manifold_edge_count: int
    open_boundary_count: int
    valence_issue_count: int
    status: str
    reasons: list[str] = field(default_factory=list)
    quad_required: bool = True
    ngon_allowed: bool = False

    @property
    def target_error_ratio(self) -> float:
        return target_error_ratio(self.face_count, self.target_face_count)

    @property
    def quad_ratio(self) -> float:
        return self.quad_count / self.face_count if self.face_count else 0.0

    @property
    def triangle_ratio(self) -> float:
        return self.triangle_count / self.face_count if self.face_count else 0.0

    def to_dict(self) -> dict:
        return {
            "face_count": self.face_count,
            "target_face_count": self.target_face_count,
            "target_error_ratio": round(self.target_error_ratio, 4),
            "triangle_count": self.triangle_count,
            "quad_count": self.quad_count,
            "ngon_count": self.ngon_count,
            "quad_ratio": round(self.quad_ratio, 4),
            "triangle_ratio": round(self.triangle_ratio, 4),
            "non_manifold_edge_count": self.non_manifold_edge_count,
            "open_boundary_count": self.open_boundary_count,
            "valence_issue_count": self.valence_issue_count,
            "quad_required": self.quad_required,
            "ngon_allowed": self.ngon_allowed,
            "status": self.status,
            "reasons": self.reasons,
        }


def _face_count_band(actual: int, target: int) -> str:
    err = target_error_ratio(actual, target)
    if err <= ACCEPTED_ERROR:
        return "accepted"
    if err <= RETRY_ERROR:
        return "retry"
    return "failed"


def _quad_ratio_band(ratio: float) -> str:
    if ratio >= QUAD_RATIO_ACCEPTED:
        return "accepted"
    if ratio >= QUAD_RATIO_RETRY:
        return "retry"
    return "failed"


def _triangle_ratio_band(ratio: float) -> str:
    if ratio <= TRIANGLE_RATIO_ACCEPTED:
        return "accepted"
    if ratio <= TRIANGLE_RATIO_RETRY:
        return "retry"
    return "failed"


def validate_topology(
    mesh: MeshGraph,
    target_face_count: int,
    *,
    quad_required: bool = True,
    ngon_allowed: bool = False,
    expect_closed: bool = True,
    ngon_after_cleanup: bool = False,
) -> ValidationReport:
    """Validate ``mesh`` against the Phase 2 criteria and return a report whose
    ``status`` is the worst gating band (plan §6.6 / §15.6).

    ``expect_closed`` gates open boundaries only when the source mesh was closed
    (plan §6.6 "0 if the original mesh is closed"). ``ngon_after_cleanup``
    escalates surviving n-gons from ``retry`` to ``failed`` (§15.6).
    """
    tris, quads, ngons = count_face_types(mesh)
    total = mesh.face_count
    non_manifold = sum(1 for e in mesh.edges if e.is_non_manifold)
    open_boundary = sum(1 for e in mesh.edges if e.is_boundary)
    valence_issues = count_valence_issues(mesh)

    bands: list[str] = []
    reasons: list[str] = []

    if total == 0:
        return ValidationReport(
            0, target_face_count, 0, 0, 0, non_manifold, open_boundary, valence_issues,
            status="failed", reasons=["empty mesh: no faces"],
            quad_required=quad_required, ngon_allowed=ngon_allowed,
        )

    # Closeness to target polycount.
    fc_band = _face_count_band(total, target_face_count)
    bands.append(fc_band)
    if fc_band != "accepted":
        reasons.append(
            f"face_count {total} vs target {target_face_count} "
            f"(error {target_error_ratio(total, target_face_count):.3f}) -> {fc_band}"
        )

    # Quad ratio (only if quads are required).
    quad_ratio = quads / total
    if quad_required:
        qr_band = _quad_ratio_band(quad_ratio)
        bands.append(qr_band)
        if qr_band != "accepted":
            reasons.append(f"quad_ratio {quad_ratio:.3f} below target -> {qr_band}")

    # Triangle proportion.
    tri_band = _triangle_ratio_band(tris / total)
    bands.append(tri_band)
    if tri_band != "accepted":
        reasons.append(f"triangle_count {tris} ({tris / total:.1%} of faces) -> {tri_band}")

    # N-gons.
    if not ngon_allowed and ngons > 0:
        ngon_band = "failed" if ngon_after_cleanup else "retry"
        bands.append(ngon_band)
        reasons.append(
            f"ngon_count {ngons} "
            f"({'still present after cleanup' if ngon_after_cleanup else 'cleanup required'}) -> {ngon_band}"
        )

    # Non-manifold edges.
    if non_manifold > 0:
        bands.append("retry")
        reasons.append(f"non_manifold_edge_count {non_manifold} (repair required) -> retry")

    # Open boundaries (only meaningful when the source was closed).
    if expect_closed and open_boundary > 0:
        bands.append("retry")
        reasons.append(f"open_boundary_count {open_boundary} on an expected-closed mesh -> retry")

    status = _worst(bands)
    if status == "accepted" and not reasons:
        reasons.append("all gating metrics within accepted thresholds")

    return ValidationReport(
        face_count=total,
        target_face_count=target_face_count,
        triangle_count=tris,
        quad_count=quads,
        ngon_count=ngons,
        non_manifold_edge_count=non_manifold,
        open_boundary_count=open_boundary,
        valence_issue_count=valence_issues,
        status=status,
        reasons=reasons,
        quad_required=quad_required,
        ngon_allowed=ngon_allowed,
    )
