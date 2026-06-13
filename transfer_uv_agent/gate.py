"""T5 — gate + correspondence report (UV_TRANSFER_PLAN §3.T5).

Hard gates are unchanged from the chart engine (raster/flip overlap, [0,1] bounds, no
Smart-UV fallback). Everything design-specific — chart count vs the reference's 39,
the chart→ref correspondence/coverage table, placement IoU, stretch, texel variance —
is **report-only for round 1** (no invented bar; calibrate after the side-by-side
review). The acceptance bar that matters is the side-by-side PNG sign-off.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransferGateConfig:
    raster_overlap_max: float = 0.005   # HARD (unchanged) — overlapping UV breaks baking
    overlap_max: float = 0.001          # HARD — flipped/folded signed area
    # Texel-density uniformity, promoted to HARD this calibration round (density-first T4).
    # Bar = the reference measured by the SAME evaluation code (out/uv_calib/calib.json:
    # reference texel_var = 0.515) × 1.2 margin = 0.62. NOT invented — and proven reachable:
    # the organic engine hit 0.0001 with average_islands_scale + uniform packing, which the
    # density-first placement preserves (rotation+translation only, no per-part scale).
    texel_density_variance_max: float = 0.62
    # GATE PARITY (round 3): a metric that is HARD in one engine must be HARD in every
    # engine (or its waiver recorded in the plan). The chart engine gates packing ≥ 0.50;
    # this gate was silently absent here, which let round 2 ship a 0.029-packing layout
    # as "accepted". Same bar, same evidence (auto-packer floor, ADAPTIVE_LOWPOLY_RESULTS).
    packing_min: float = 0.50
    # uv bounds [0,1] and fallback_used=false are hard, fixed.


@dataclass
class GateCheck:
    name: str
    passed: bool
    value: float
    limit: float
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "value": self.value,
                "limit": self.limit, "detail": self.detail}


@dataclass
class TransferGateResult:
    checks: list

    @property
    def failures(self):
        return [c for c in self.checks if not c.passed]

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def verdict(self) -> str:
        return "accepted" if self.passed else "failed"

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "passed": self.passed,
                "failures": [c.name for c in self.failures],
                "checks": [c.to_dict() for c in self.checks]}


def evaluate_transfer_gate(metrics: dict, *, config: TransferGateConfig = TransferGateConfig()
                           ) -> TransferGateResult:
    """Apply the T5 HARD gates only (the report metrics are computed separately by
    :func:`correspondence_report`). Keys: ``raster_overlap_ratio``, ``overlap_ratio``,
    ``uv_bounds_ok`` (bool), ``fallback_used`` (bool)."""
    c: list[GateCheck] = []
    c.append(GateCheck("raster_overlap_ratio", float(metrics.get("raster_overlap_ratio", 1.0)) <= config.raster_overlap_max,
                       round(float(metrics.get("raster_overlap_ratio", 1.0)), 6), config.raster_overlap_max,
                       "no true pixel overlap (raster) — baking-safe"))
    c.append(GateCheck("overlap_ratio", float(metrics.get("overlap_ratio", 1.0)) <= config.overlap_max,
                       round(float(metrics.get("overlap_ratio", 1.0)), 6), config.overlap_max,
                       "no folded UV area (signed-area)"))
    tv = float(metrics.get("texel_density_variance", 9.0))
    c.append(GateCheck("texel_density_variance", tv <= config.texel_density_variance_max,
                       round(tv, 4), config.texel_density_variance_max,
                       "uniform texel density (density-first, HARD)"))
    pe = float(metrics.get("packing_efficiency", 0.0))
    c.append(GateCheck("packing_efficiency", pe >= config.packing_min,
                       round(pe, 4), config.packing_min,
                       "UV-space utilisation (gate parity with the chart engine)"))
    bounds = bool(metrics.get("uv_bounds_ok", False))
    c.append(GateCheck("uv_bounds", bounds, float(bounds), 1.0, "all UVs in [0,1]"))
    fb = bool(metrics.get("fallback_used", True))
    c.append(GateCheck("fallback_used", not fb, float(fb), 0.0, "Smart-UV fallback may NOT be shipped"))
    return TransferGateResult(checks=c)


def correspondence_report(ref_charts, adaptive_to_ref: dict, placements: list,
                          *, ref_count: int) -> dict:
    """Build the T5 report table (report-only): chart count vs the reference's, the
    chart→ref correspondence + a coverage list (reference charts with no adaptive faces),
    and mean placement IoU. ``adaptive_to_ref`` maps adaptive chart id → reference chart
    id; ``placements`` is the list of :class:`~transfer_uv_agent.placement.Placement`."""
    covered = set(adaptive_to_ref.values())
    uncovered = sorted({c.chart_id for c in ref_charts} - covered)
    ious = [p.iou for p in placements] if placements else []
    return {
        "adaptive_chart_count": len(adaptive_to_ref),
        "reference_chart_count": ref_count,
        "chart_count_delta": len(adaptive_to_ref) - ref_count,
        "correspondence": {int(k): int(v) for k, v in adaptive_to_ref.items()},
        "uncovered_reference_charts": uncovered,
        "uncovered_count": len(uncovered),
        "mean_placement_iou": round(sum(ious) / len(ious), 4) if ious else 0.0,
        "min_placement_iou": round(min(ious), 4) if ious else 0.0,
    }
