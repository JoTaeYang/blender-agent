"""Phase A4 — adaptive-mode quality gate + retry ladder (Adaptive Low-Poly plan §7).

The silhouette gate is **HARD at the decimation stage**, not deferred (plan §4/§7):
the v1 failure was accepting a mesh whose thin extremities had been truncated, hidden
by auto-framed renders. This module is the gate that refuses to ship such a mesh, plus
the pure logic that decides which retry rung to climb next.

Two halves, both Blender-free so they are unit-tested offline (the ``bpy`` work —
measuring the reference baseline and the candidate's coverage/shape — is done by the
A2 metric record and the worker, then fed in here as plain numbers):

- :func:`evaluate_gate` applies the §7 threshold table to a candidate's metrics. The
  thresholds that compare against the *reference* (humanstatue_low measured vs the
  same proxy, same world space) are relative multipliers on a :class:`ReferenceBaseline`
  so the gate calibrates to the asset rather than to magic absolute distances. The
  per-axis bbox coverage and 0-n-gon / 0-non-manifold checks are HARD and explicitly
  NOT calibratable (plan §7 table).
- :func:`next_rung` walks the cheap→expensive retry ladder (plan §7), mapping the gate
  failures to the rung most likely to fix them and never repeating a spent rung, so a
  gate miss escalates deterministically and ends in an explicit ``report_failed``
  rather than a silently-shipped bad mesh.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from retopo_agent.geometry.target_search import target_error_ratio

# Retry-ladder rungs, cheap → expensive (plan §7). ``next_rung`` only ever returns
# one of these; the worker maps a rung to the concrete A2/A3 re-run it triggers.
RUNG_RATIO_REAIM = "ratio_reaim"            # face-count band miss -> re-aim A2 ratio
RUNG_TRIQUAD_TWEAK = "triquad_threshold"    # A3 tris->quads broke a hard gate
RUNG_FEATURE_PROTECT = "feature_protection"  # extremity thinning -> protect features
RUNG_SHRINKWRAP = "shrinkwrap_snap"         # shape metrics -> toggle/strengthen snap
RUNG_DENSER_PROXY = "denser_proxy"          # thin features under-resolved at 1M
RUNG_REPORT_FAILED = "report_failed"        # ladder exhausted -> fail with best attempt

# The ladder in escalation order. ``RUNG_REPORT_FAILED`` is the terminal rung.
LADDER_ORDER = [
    RUNG_RATIO_REAIM,
    RUNG_TRIQUAD_TWEAK,
    RUNG_FEATURE_PROTECT,
    RUNG_SHRINKWRAP,
    RUNG_DENSER_PROXY,
    RUNG_REPORT_FAILED,
]


@dataclass(frozen=True)
class GateThresholds:
    """The §7 acceptance thresholds. Multipliers are applied to a
    :class:`ReferenceBaseline`; ``bbox_axis_min`` and the 0-n-gon / 0-non-manifold
    checks are HARD constants (plan §7 "NOT calibratable")."""

    bbox_axis_min: float = 0.98          # per-axis bbox coverage vs proxy, HARD
    proxy_to_low_max_mult: float = 1.25  # proxy->low max distance vs reference, HARD
    proxy_to_low_p99_mult: float = 1.25  # proxy->low p99 distance vs reference, HARD
    band_tol: float = 0.10               # face count within T_goal ±10%, HARD
    vert_count_mult: float = 1.15        # vert count vs reference, sanity (warn)
    soft_dist_mult: float = 1.5          # low->proxy mean distance vs reference, soft
    soft_normal_mult: float = 1.5        # normal deviation vs reference, soft


@dataclass(frozen=True)
class ReferenceBaseline:
    """The reference mesh (humanstatue_low) measured against the SAME proxy in the
    SAME world space (plan §7 "Baseline first"). The generated mesh is judged
    *relative* to these — it must not be meaningfully worse than the ground truth."""

    proxy_to_ref_max: float          # worst proxy->reference distance
    proxy_to_ref_p99: float          # 99th-pct proxy->reference distance
    ref_to_proxy_mean: float         # mean reference->proxy distance
    ref_to_proxy_normal_dev: float   # mean reference->proxy normal deviation (deg)
    ref_vertex_count: int

    def to_dict(self) -> dict:
        return {
            "proxy_to_ref_max": round(self.proxy_to_ref_max, 6),
            "proxy_to_ref_p99": round(self.proxy_to_ref_p99, 6),
            "ref_to_proxy_mean": round(self.ref_to_proxy_mean, 6),
            "ref_to_proxy_normal_dev": round(self.ref_to_proxy_normal_dev, 3),
            "ref_vertex_count": self.ref_vertex_count,
        }


@dataclass
class GateCheck:
    """One gate check. ``kind`` is ``hard`` (blocks acceptance), ``soft`` (passes
    but triggers a retry rung) or ``sanity`` (warning only)."""

    name: str
    kind: str
    passed: bool
    value: float
    limit: float
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "passed": self.passed,
            "value": self.value,
            "limit": self.limit,
            "detail": self.detail,
        }


@dataclass
class GateResult:
    """The verdict for one candidate (plan §7). ``passed`` ⇔ every HARD check holds
    and no SOFT check failed; sanity checks are warnings and never block."""

    checks: list[GateCheck] = field(default_factory=list)

    @property
    def hard_failures(self) -> list[GateCheck]:
        return [c for c in self.checks if c.kind == "hard" and not c.passed]

    @property
    def soft_failures(self) -> list[GateCheck]:
        return [c for c in self.checks if c.kind == "soft" and not c.passed]

    @property
    def sanity_warnings(self) -> list[GateCheck]:
        return [c for c in self.checks if c.kind == "sanity" and not c.passed]

    @property
    def passed_hard(self) -> bool:
        return not self.hard_failures

    @property
    def passed(self) -> bool:
        """Ships only if every hard check holds AND no soft check failed."""
        return self.passed_hard and not self.soft_failures

    @property
    def verdict(self) -> str:
        return "pass" if self.passed else "retry"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "passed": self.passed,
            "passed_hard": self.passed_hard,
            "hard_failures": [c.name for c in self.hard_failures],
            "soft_failures": [c.name for c in self.soft_failures],
            "sanity_warnings": [c.name for c in self.sanity_warnings],
            "checks": [c.to_dict() for c in self.checks],
        }


def evaluate_gate(
    metrics: dict,
    *,
    target_face_count: int,
    baseline: ReferenceBaseline,
    thresholds: GateThresholds = GateThresholds(),
) -> GateResult:
    """Apply the §7 gate table to a candidate's ``metrics``.

    ``metrics`` is the flat record A2/A3 produce (an :class:`AdaptiveAttempt`-shaped
    dict): ``ngons``, ``non_manifold_edges``, ``faces``, ``vertex_count``,
    ``bbox_per_axis`` (``{"x","y","z"}``), ``proxy_to_low`` (``{"max","p99"}``) and
    ``low_to_proxy`` (``{"mean","normal_dev"}``). Distances are absolute, compared to
    ``baseline`` via the threshold multipliers; the bbox / topology checks are HARD
    constants. Returns a :class:`GateResult` whose ``passed`` decides ship vs retry."""
    checks: list[GateCheck] = []

    # --- HARD: topology invariants (plan §7 table).
    ngons = int(metrics.get("ngons", 0))
    checks.append(GateCheck("ngons", "hard", ngons == 0, ngons, 0, "0 n-gons required"))

    non_manifold = int(metrics.get("non_manifold_edges", 0))
    checks.append(GateCheck(
        "non_manifold_edges", "hard", non_manifold == 0, non_manifold, 0,
        "0 non-manifold edges required",
    ))

    # --- HARD: per-axis bbox coverage vs proxy (the cheap silhouette screen).
    per_axis = metrics.get("bbox_per_axis", {}) or {}
    worst_axis = min(per_axis.values()) if per_axis else 0.0
    checks.append(GateCheck(
        "bbox_coverage", "hard", worst_axis >= thresholds.bbox_axis_min,
        round(worst_axis, 4), thresholds.bbox_axis_min,
        f"per-axis coverage {per_axis} (worst {worst_axis:.4f} >= {thresholds.bbox_axis_min})",
    ))

    # --- HARD: directional proxy->low distance vs reference (the truncation catcher).
    p2l = metrics.get("proxy_to_low", {}) or {}
    max_limit = baseline.proxy_to_ref_max * thresholds.proxy_to_low_max_mult
    p2l_max = float(p2l.get("max", 0.0))
    checks.append(GateCheck(
        "proxy_to_low_max", "hard", p2l_max <= max_limit, round(p2l_max, 6), round(max_limit, 6),
        f"max proxy->low {p2l_max:.5g} <= reference {baseline.proxy_to_ref_max:.5g} x {thresholds.proxy_to_low_max_mult}",
    ))
    p99_limit = baseline.proxy_to_ref_p99 * thresholds.proxy_to_low_p99_mult
    p2l_p99 = float(p2l.get("p99", 0.0))
    checks.append(GateCheck(
        "proxy_to_low_p99", "hard", p2l_p99 <= p99_limit, round(p2l_p99, 6), round(p99_limit, 6),
        f"p99 proxy->low {p2l_p99:.5g} <= reference {baseline.proxy_to_ref_p99:.5g} x {thresholds.proxy_to_low_p99_mult}",
    ))

    # --- HARD: face count within T_goal ±10%.
    faces = int(metrics.get("faces", 0))
    err = target_error_ratio(faces, target_face_count)
    checks.append(GateCheck(
        "face_count_band", "hard", err <= thresholds.band_tol, round(err, 4), thresholds.band_tol,
        f"{faces} faces vs target {target_face_count} (err {err:.4f} <= {thresholds.band_tol})",
    ))

    # --- SANITY: vertex count vs reference (warning only).
    verts = int(metrics.get("vertex_count", 0))
    vert_limit = baseline.ref_vertex_count * thresholds.vert_count_mult
    checks.append(GateCheck(
        "vertex_count", "sanity", verts <= vert_limit, verts, round(vert_limit, 1),
        f"{verts} verts <= reference {baseline.ref_vertex_count} x {thresholds.vert_count_mult}",
    ))

    # --- SOFT: shape fidelity vs reference (passes hard, but triggers a retry).
    l2p = metrics.get("low_to_proxy", {}) or {}
    mean_limit = baseline.ref_to_proxy_mean * thresholds.soft_dist_mult
    l2p_mean = float(l2p.get("mean", 0.0))
    checks.append(GateCheck(
        "low_to_proxy_mean", "soft", l2p_mean <= mean_limit, round(l2p_mean, 6), round(mean_limit, 6),
        f"mean low->proxy {l2p_mean:.5g} <= reference {baseline.ref_to_proxy_mean:.5g} x {thresholds.soft_dist_mult}",
    ))
    nd_limit = baseline.ref_to_proxy_normal_dev * thresholds.soft_normal_mult
    l2p_nd = float(l2p.get("normal_dev", 0.0))
    checks.append(GateCheck(
        "normal_deviation", "soft", l2p_nd <= nd_limit, round(l2p_nd, 3), round(nd_limit, 3),
        f"normal dev {l2p_nd:.3g} <= reference {baseline.ref_to_proxy_normal_dev:.3g} x {thresholds.soft_normal_mult}",
    ))

    return GateResult(checks=checks)


# Which rung most directly addresses each failing check (plan §7 ladder). The first
# matching rung that has not been spent yet is chosen; ``next_rung`` then escalates.
_FAILURE_TO_RUNG = {
    "face_count_band": RUNG_RATIO_REAIM,
    "ngons": RUNG_TRIQUAD_TWEAK,
    "non_manifold_edges": RUNG_TRIQUAD_TWEAK,
    "bbox_coverage": RUNG_FEATURE_PROTECT,
    "proxy_to_low_max": RUNG_FEATURE_PROTECT,
    "proxy_to_low_p99": RUNG_FEATURE_PROTECT,
    "low_to_proxy_mean": RUNG_SHRINKWRAP,
    "normal_deviation": RUNG_SHRINKWRAP,
}


def next_rung(gate: GateResult, attempted_rungs) -> str:
    """Pick the next retry rung for a failing ``gate`` (plan §7 ladder).

    Maps the gate's failures to the cheapest rung that targets them; if that rung
    is already in ``attempted_rungs`` it escalates to the next unspent rung in
    :data:`LADDER_ORDER`. A passing gate needs no retry (returns ``""``). When every
    actionable rung is spent it returns :data:`RUNG_REPORT_FAILED` — the ladder never
    loops and never silently ships a gate-violating mesh.
    """
    if gate.passed:
        return ""
    attempted = set(attempted_rungs)

    # Preferred rungs for the actual failures, in ladder order (hard before soft).
    failing = [c.name for c in gate.hard_failures] + [c.name for c in gate.soft_failures]
    preferred: list[str] = []
    for name in failing:
        rung = _FAILURE_TO_RUNG.get(name)
        if rung and rung not in preferred:
            preferred.append(rung)
    preferred.sort(key=LADDER_ORDER.index)

    for rung in preferred:
        if rung not in attempted:
            return rung

    # Every targeted rung is spent -> escalate to the next unspent ladder rung past
    # the most-advanced one already tried (e.g. the denser-proxy fallback).
    spent_indices = [LADDER_ORDER.index(r) for r in attempted if r in LADDER_ORDER]
    start = (max(spent_indices) + 1) if spent_indices else 0
    for rung in LADDER_ORDER[start:]:
        if rung not in attempted:
            return rung
    return RUNG_REPORT_FAILED
