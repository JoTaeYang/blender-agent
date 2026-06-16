"""Seam Decision Core — edge-level seam policy (RULE_BASED_UV_SEAM_CORE_PLAN §5.1).

This is the *final seam decision layer* the plan asks for: a pure, deterministic helper
that scores every edge and labels it ``mandatory`` / ``candidate`` / ``forbidden`` /
``ignored`` with a human-readable list of reasons. It does NOT unwrap or mutate the mesh —
it is the layer ``chart_uv_agent`` segmentation/refinement (and a reviewer UI) read to know
*why* an edge is or isn't a seam.

The order the plan mandates (§4):

    mandatory ≥90° fold  →  user forbidden/preferred constraint  →  distortion (later, in
    the refinement loop)  →  visibility / hidden-side cost  →  optional part-template hint

so this module owns the first, second and fourth of those; distortion is measured after the
unwrap by :mod:`artist_uv_agent.seam_refinement`. Geometry decides — never an LLM (§9). The
LLM's role is only to hand us ``forbidden_edges`` / ``preferred_edges`` (and, later, zones as
face sets); here they are plain edge-id sets.

Conflict rule (§5.1, §10): a user *forbidden* edge that is also a ≥90° model fold stays
``mandatory`` (the hard rule wins) and the clash is recorded so the report can surface it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from uv_agent.geometry.mesh_graph import MeshGraph

FOLD_ANGLE = 90.0


@dataclass(frozen=True)
class SeamPolicyConfig:
    """Thresholds for the edge policy (RULE_BASED_UV_SEAM_CORE_PLAN §5.1). ``distortion_*``
    and ``max_islands`` are consumed by :mod:`artist_uv_agent.seam_refinement`; they live here
    so one config object drives the whole Seam Decision Core."""

    mandatory_fold_angle: float = 90.0   # ≥ this dihedral → unconditional seam (Rule 2)
    smooth_preserve_angle: float = 45.0  # ≤ this dihedral → bias AGAINST a seam (Rule 1)
    distortion_threshold: float = 0.35   # worst-island checker distortion bar (refinement)
    min_improvement_ratio: float = 0.15  # accept an added seam only if it improves this much
    max_islands: int = 80                # island safety cap (never a target)
    # Visibility weighting — only applied when a front/up axis is supplied (§5.1, neutral
    # otherwise). A front-facing low-angle edge costs MORE to cut (a visible seam); a
    # back/underside edge costs LESS.
    visibility_weight: float = 0.5


@dataclass
class EdgeSeamDecision:
    """One edge's verdict. ``score`` is the candidate desirability for a *non-mandatory*
    edge (higher = a better place to cut if distortion later forces one); it is unused for
    ``mandatory``/``forbidden``/``ignored``. ``reasons`` is the audit trail for the report."""

    edge_id: int
    decision: str  # "mandatory" | "candidate" | "forbidden" | "ignored"
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"edge_id": self.edge_id, "decision": self.decision,
                "score": round(float(self.score), 6), "reasons": list(self.reasons)}


def _axis_vector(axis: str | None):
    """Map a signed axis token (``+y`` / ``-z`` / ``x``) to a unit vector, or ``None``."""
    if not axis:
        return None
    s = axis.strip().lower()
    if not s:
        return None
    sign = -1.0 if s[0] == "-" else 1.0
    s = s.lstrip("+-")
    idx = {"x": 0, "y": 1, "z": 2}.get(s)
    if idx is None:
        return None
    v = np.zeros(3)
    v[idx] = sign
    return v


def _edge_normal(mesh: MeshGraph, edge) -> np.ndarray:
    """Mean of the (up to two) adjacent face normals — the edge's outward direction."""
    ns = [np.asarray(mesh.faces[f].normal, dtype=float) for f in edge.face_ids]
    if not ns:
        return np.zeros(3)
    m = np.sum(ns, axis=0)
    n = np.linalg.norm(m)
    return m / n if n > 1e-12 else np.zeros(3)


def _visibility_bias(mesh: MeshGraph, edge, front_vec, cfg: SeamPolicyConfig):
    """Signed candidate-score nudge from a front axis (§5.1). Front-facing edge → +reason,
    +score (cutting here is visible, so prefer it only if needed → treated as higher cost,
    i.e. a *less* attractive candidate). Returns ``(delta, reason|None)``.

    Note the plan's wording: a visible front edge has higher seam *cost*, so we LOWER its
    candidate score; a hidden back edge has lower cost, so we RAISE it."""
    if front_vec is None:
        return 0.0, None
    n = _edge_normal(mesh, edge)
    if np.linalg.norm(n) < 1e-9:
        return 0.0, None
    facing = float(np.dot(n, front_vec))  # +1 front, -1 back
    if facing > 0.25:
        return -cfg.visibility_weight * facing, "front_facing_visible"
    if facing < -0.25:
        return +cfg.visibility_weight * (-facing), "hidden_side"
    return 0.0, None


def decide_edge(mesh: MeshGraph, edge_id: int, *, config: SeamPolicyConfig | None = None,
                forbidden_edges=frozenset(), preferred_edges=frozenset(),
                front_vec=None) -> EdgeSeamDecision:
    """Classify a single edge (pure). Precedence exactly per §4/§5.1:

    1. ≥ ``mandatory_fold_angle`` fold, boundary or non-manifold → ``mandatory`` (Rule 2);
       if the user also forbade it, note the conflict but keep it mandatory.
    2. user ``forbidden`` (and not a mandatory fold) → ``forbidden`` (never a seam).
    3. otherwise a ``candidate`` whose score rises with dihedral, user preference and a
       hidden-side bias, and falls on a smooth/front-facing edge. A near-flat edge with no
       pull becomes ``ignored`` (a seam there would needlessly cut a smooth surface)."""
    cfg = config or SeamPolicyConfig()
    e = mesh.edges[edge_id]
    fold = cfg.mandatory_fold_angle
    reasons: list[str] = []

    # (1) Hard rule: ≥90° folds + topology boundaries are always seams.
    is_fold = len(e.face_ids) == 2 and e.dihedral_angle >= fold
    if is_fold or e.is_boundary or e.is_non_manifold:
        if e.is_boundary:
            reasons.append("boundary")
        if e.is_non_manifold:
            reasons.append("non_manifold")
        if is_fold:
            reasons.append(f"mandatory_fold_>={fold:g}deg")
        if edge_id in forbidden_edges:
            reasons.append("conflict:user_forbidden_overridden_by_mandatory")
        return EdgeSeamDecision(edge_id, "mandatory", 1.0, reasons)

    # (2) User preserve wins over every soft signal.
    if edge_id in forbidden_edges:
        return EdgeSeamDecision(edge_id, "forbidden", 0.0, ["user_forbidden"])

    # (3) Soft candidate scoring (only consulted if distortion later forces a cut).
    d = e.dihedral_angle
    # Sharper non-fold edges are cheaper, better cut lines: 0 at flat → ~1 approaching 90°.
    score = max(0.0, min(1.0, d / fold))
    if d <= cfg.smooth_preserve_angle:
        reasons.append("smooth_preserve_bias")
    else:
        reasons.append("moderate_crease")

    if edge_id in preferred_edges:
        score += 0.5
        reasons.append("user_preferred")

    delta, vreason = _visibility_bias(mesh, e, front_vec, cfg)
    if vreason:
        score += delta
        reasons.append(vreason)

    score = max(0.0, score)
    # A perfectly flat, unpreferred, no-bias edge is not worth cutting → ignored.
    if score <= 1e-9 and not (edge_id in preferred_edges):
        return EdgeSeamDecision(edge_id, "ignored", 0.0, reasons + ["flat_no_pull"])
    return EdgeSeamDecision(edge_id, "candidate", score, reasons)


def decide_edge_seams(mesh: MeshGraph, *, config: SeamPolicyConfig | None = None,
                      forbidden_edges=(), preferred_edges=(),
                      front_axis: str | None = None,
                      up_axis: str | None = None) -> list[EdgeSeamDecision]:
    """Run :func:`decide_edge` over every edge (RULE_BASED_UV_SEAM_CORE_PLAN §5.1). Returns
    one :class:`EdgeSeamDecision` per edge, indexed by edge id. ``up_axis`` is accepted for
    forward-compat (a future signed-dihedral / underside rule) but the 1st-milestone
    visibility bias uses only ``front_axis`` (§5.1: keep visibility simple)."""
    cfg = config or SeamPolicyConfig()
    forbidden = set(forbidden_edges)
    preferred = set(preferred_edges)
    front_vec = _axis_vector(front_axis)
    return [decide_edge(mesh, e.id, config=cfg, forbidden_edges=forbidden,
                        preferred_edges=preferred, front_vec=front_vec)
            for e in mesh.edges]


def material_boundary_edges(mesh: MeshGraph) -> set[int]:
    """Edges whose two faces have different ``material_index`` (§5.1 strong candidate). The
    mesh graph carries ``material_index`` (0 by default), so this is report-only / a candidate
    booster on assets that actually set materials; on single-material meshes it is empty."""
    out: set[int] = set()
    for e in mesh.edges:
        if len(e.face_ids) == 2:
            a, b = e.face_ids
            if mesh.faces[a].material_index != mesh.faces[b].material_index:
                out.add(e.id)
    return out


def policy_summary(decisions: list[EdgeSeamDecision]) -> dict:
    """Count decisions by type + collect the mandatory-vs-forbidden conflicts, for the
    report and a quick gate (RULE_BASED_UV_SEAM_CORE_PLAN §5.3)."""
    counts: dict[str, int] = {}
    conflicts: list[int] = []
    for d in decisions:
        counts[d.decision] = counts.get(d.decision, 0) + 1
        if any(r.startswith("conflict:") for r in d.reasons):
            conflicts.append(d.edge_id)
    return {"counts": counts, "conflict_edge_ids": sorted(conflicts),
            "mandatory_edges": [d.edge_id for d in decisions if d.decision == "mandatory"]}
