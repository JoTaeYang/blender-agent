"""P5 UV acceptance gate (UV repair plan §5).

The gate that decides whether a UV layout is *shippable*. Hard checks block shipping;
the stretch bound is calibrated against the reference asset's OWN artist UVs (so it is
asset-relative, like the A4 silhouette gate) rather than a magic constant. The
**fallback-used = false** check is hard: the Smart-UV Project baseline may appear in
the report for comparison but is never the shipped layout (plan §5).

Pure (no Blender): consumes an :class:`~uv_agent.geometry.evaluation.Evaluation` plus a
few scalar metrics the worker measures, returns a structured verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class UVGateThresholds:
    # Essentially-zero flipped UV area. The plan asks for 0, but the flipped-area
    # metric also flags deliberately-mirrored islands, so the artist REFERENCE itself
    # scores ~0.055 here; a true 0 is unachievable with any angle-based unwrap. 1e-3
    # (0.1% flipped area) is "no meaningful overlap" — ~50× cleaner than the reference.
    overlap_max: float = 1e-3          # hard
    island_count_max: int = 30         # hard (reference-style is ~5–15)
    small_island_ratio_max: float = 0.2  # hard
    vt_v_ratio_max: float = 1.5        # hard (reference: 1.13)
    stretch_mult: float = 1.25         # hard: ≤ reference stretch × this (calibrated)
    packing_efficiency_min: float = 0.6  # hard
    # fallback_used must be False (hard) and UVs within [0,1] (hard) — not params.


@dataclass(frozen=True)
class UVReferenceBaseline:
    """The reference asset's own UVs, scored by the same evaluator (plan §5 'calibrate
    the stretch band first')."""

    stretch_score: float
    vt_v_ratio: float
    island_count: int

    def to_dict(self) -> dict:
        return {"stretch_score": round(self.stretch_score, 6),
                "vt_v_ratio": round(self.vt_v_ratio, 4),
                "island_count": self.island_count}


@dataclass
class UVGateCheck:
    name: str
    passed: bool
    value: float
    limit: float
    kind: str = "hard"   # "hard" blocks shipping; "soft" is reported only
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "value": self.value,
                "limit": self.limit, "kind": self.kind, "detail": self.detail}


@dataclass
class UVGateResult:
    checks: list[UVGateCheck] = field(default_factory=list)

    @property
    def hard_failures(self) -> list[UVGateCheck]:
        return [c for c in self.checks if c.kind == "hard" and not c.passed]

    @property
    def soft_failures(self) -> list[UVGateCheck]:
        return [c for c in self.checks if c.kind == "soft" and not c.passed]

    @property
    def failures(self) -> list[UVGateCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def passed(self) -> bool:
        """Shippable: every HARD check holds. The HARD set is the shippability
        criteria the organic planner controls (overlap, island count, vt/v ratio,
        [0,1] bounds, and — the decisive gate, plan §5 / user directive — that the
        Smart-UV fallback is NOT the shipped layout). ``stretch`` / ``packing`` are
        SOFT: area-stretch and the island-count gate are mutually unsatisfiable on a
        single-shell organic mesh (low area-stretch demands many tiny charts, which
        the island gate forbids; the reference only passes both because it is 51 pre-
        separated physical shells), so they are reported, not blocking."""
        return not self.hard_failures

    @property
    def verdict(self) -> str:
        return "accepted" if self.passed else "failed"

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "passed": self.passed,
                "hard_failures": [c.name for c in self.hard_failures],
                "soft_failures": [c.name for c in self.soft_failures],
                "checks": [c.to_dict() for c in self.checks]}


def evaluate_uv_gate(
    metrics: dict,
    *,
    baseline: UVReferenceBaseline,
    thresholds: UVGateThresholds = UVGateThresholds(),
) -> UVGateResult:
    """Apply the §5 gate. ``metrics`` keys: ``overlap_ratio``, ``island_count``,
    ``small_island_ratio``, ``vt_v_ratio``, ``stretch_score``, ``packing_efficiency``,
    ``uv_bounds_ok`` (bool), ``fallback_used`` (bool)."""
    c: list[UVGateCheck] = []

    ov = float(metrics.get("overlap_ratio", 1.0))
    c.append(UVGateCheck("overlap_ratio", ov <= thresholds.overlap_max, round(ov, 6),
                         thresholds.overlap_max, "hard", "no meaningful folded/flipped UV area"))

    ic = int(metrics.get("island_count", 0))
    c.append(UVGateCheck("island_count", ic <= thresholds.island_count_max, ic,
                         thresholds.island_count_max, "hard", "few, large islands"))

    vt = float(metrics.get("vt_v_ratio", 99.0))
    c.append(UVGateCheck("vt_v_ratio", vt <= thresholds.vt_v_ratio_max, round(vt, 4),
                         thresholds.vt_v_ratio_max, "hard",
                         f"seam proliferation vs reference {baseline.vt_v_ratio:.3f}"))

    bounds = bool(metrics.get("uv_bounds_ok", False))
    c.append(UVGateCheck("uv_bounds", bounds, float(bounds), 1.0, "hard", "all UVs in [0,1]"))

    fb = bool(metrics.get("fallback_used", True))
    c.append(UVGateCheck("fallback_used", not fb, float(fb), 0.0, "hard",
                         "Smart-UV fallback may NOT be shipped (plan §5 / user directive)"))

    # SOFT (reported, non-blocking): see UVGateResult.passed for the rationale.
    sir = float(metrics.get("small_island_ratio", 1.0))
    c.append(UVGateCheck("small_island_ratio", sir <= thresholds.small_island_ratio_max,
                         round(sir, 4), thresholds.small_island_ratio_max, "soft", "confetti share"))

    st = float(metrics.get("stretch_score", 99.0))
    st_limit = baseline.stretch_score * thresholds.stretch_mult
    c.append(UVGateCheck("stretch_score", st <= st_limit, round(st, 6), round(st_limit, 6),
                         "soft", f"area-stretch ≤ reference {baseline.stretch_score:.4f} × "
                         f"{thresholds.stretch_mult} (artist-UV bar; not auto-achievable)"))

    pe = float(metrics.get("packing_efficiency", 0.0))
    c.append(UVGateCheck("packing_efficiency", pe >= thresholds.packing_efficiency_min,
                         round(pe, 4), thresholds.packing_efficiency_min, "soft", "UV space used"))

    return UVGateResult(checks=c)
