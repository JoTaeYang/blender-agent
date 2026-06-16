"""Gate + artist-style report (AUTO_ARTIST_UV_PLAN §6).

Two tiers, kept strictly separate (plan §6 / §12 "keep hard technical gates separate"):

- **Hard gates** — correctness; block shipping and are NEVER weakened for a prettier
  screenshot: in-bounds UVs, no Smart-UV fallback, no raster/flip overlap, uniform-enough
  texel density, and a minimum island size for non-detail charts.
- **Quality gates** — calibrated acceptance bars that *should* pass but may be tuned:
  stretch, packing (a lower floor than the chart engine — readability over packing,
  plan §5.A6), island count, vt/v, no tendrils, chart convexity.

The **artist-style report metrics** (part coverage, charts-per-part, symmetry pairs,
layout groups, orientation/strip/detail/readability scores) are REPORT-ONLY in v1 —
``readability_score`` is not a hard bar until calibrated on several assets (plan §6).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtistGateConfig:
    # --- HARD correctness gates (block shipping; do NOT weaken — plan §6/§11). ---
    overlap_max: float = 0.001            # flipped/folded UV area (signed-area)
    raster_overlap_max: float = 0.005     # TRUE pixel overlap (1024² grid) — breaks baking
    texel_density_variance_max: float = 1.03   # uniform texel density
    min_island_face_floor: int = 5        # a chart below this must be a 'detail' role
    # Packing is HARD (user correction after out/artist_full/t5850 shipped a 0.24-packing
    # layout as "accepted"): a UV that wastes most of the tile is not usable. The final
    # layout is the Blender CONCAVE packer (the band/shelf bbox packer was demoted to
    # debug), which reaches the chart engine's ~0.42 on these seams. Floor 0.40 — at/above
    # the 0.38 minimum, just under chart's 0.42 (artist adds a few part seams).
    packing_min: float = 0.40
    # uv_bounds_ok and fallback_used=false are hard correctness gates (fixed, not tunable).

    # --- Quality gates (calibrated acceptance defaults; recalibrate per §AR7). ---
    stretch_max: float = 0.50
    island_count_max: int = 80            # dynamic-ish cap; artist parts add a few charts
    vt_v_max: float = 2.2
    tendril_count_max: int = 0
    convexity_p10_min: float = 0.50

    def to_dict(self) -> dict:
        return {
            "overlap_max": self.overlap_max, "raster_overlap_max": self.raster_overlap_max,
            "texel_density_variance_max": self.texel_density_variance_max,
            "min_island_face_floor": self.min_island_face_floor,
            "uv_bounds_ok_required": True, "fallback_used_allowed": False,
            "stretch_max": self.stretch_max, "packing_min": self.packing_min,
            "island_count_max": self.island_count_max, "vt_v_max": self.vt_v_max,
            "tendril_count_max": self.tendril_count_max,
            "convexity_p10_min": self.convexity_p10_min,
        }


@dataclass
class ArtistGateCheck:
    name: str
    kind: str            # "hard" | "quality"
    passed: bool
    value: float
    limit: float
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "passed": self.passed,
                "value": self.value, "limit": self.limit, "detail": self.detail}


@dataclass
class ArtistGateResult:
    checks: list

    @property
    def hard_failures(self):
        return [c for c in self.checks if c.kind == "hard" and not c.passed]

    @property
    def quality_failures(self):
        return [c for c in self.checks if c.kind == "quality" and not c.passed]

    @property
    def failures(self):
        return [c for c in self.checks if not c.passed]

    @property
    def passed(self) -> bool:
        """Shipping verdict — HARD gates only (quality misses are reported, not blocking)."""
        return not self.hard_failures

    @property
    def verdict(self) -> str:
        return "accepted" if self.passed else "failed"

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "passed": self.passed,
                "hard_failures": [c.name for c in self.hard_failures],
                "quality_failures": [c.name for c in self.quality_failures],
                "checks": [c.to_dict() for c in self.checks]}


def evaluate_artist_gate(metrics: dict, *, config: ArtistGateConfig = ArtistGateConfig()
                         ) -> ArtistGateResult:
    """Apply the §6 hard + quality gates. ``metrics`` keys mirror the chart engine plus
    ``min_island_faces`` (smallest non-detail chart face count) and ``min_island_role``."""
    g = config
    c: list[ArtistGateCheck] = []

    def hard_le(name, val, lim, detail=""):
        c.append(ArtistGateCheck(name, "hard", float(val) <= lim, round(float(val), 6), lim, detail))

    def qual_le(name, val, lim, detail=""):
        c.append(ArtistGateCheck(name, "quality", float(val) <= lim, round(float(val), 6), lim, detail))

    def qual_ge(name, val, lim, detail=""):
        c.append(ArtistGateCheck(name, "quality", float(val) >= lim, round(float(val), 6), lim, detail))

    # Hard correctness.
    hard_le("overlap_ratio", metrics.get("overlap_ratio", 1.0), g.overlap_max,
            "no folded UV area (signed-area)")
    hard_le("raster_overlap_ratio", metrics.get("raster_overlap_ratio", 1.0), g.raster_overlap_max,
            "no true pixel overlap (raster) — baking-safe")
    hard_le("texel_density_variance", metrics.get("texel_density_variance", 9.0),
            g.texel_density_variance_max, "uniform texel density")
    bounds = bool(metrics.get("uv_bounds_ok", False))
    c.append(ArtistGateCheck("uv_bounds", "hard", bounds, float(bounds), 1.0, "all UVs in [0,1]"))
    fb = bool(metrics.get("fallback_used", True))
    c.append(ArtistGateCheck("fallback_used", "hard", not fb, float(fb), 0.0,
                             "Smart-UV fallback may NOT be shipped"))
    # Minimum island size: a chart below the floor must be a 'detail' (plan §6 hard list).
    min_faces = int(metrics.get("min_nondetail_island_faces", g.min_island_face_floor))
    c.append(ArtistGateCheck("min_island_size", "hard", min_faces >= g.min_island_face_floor,
                             min_faces, g.min_island_face_floor,
                             "no sub-floor island unless role=detail"))
    # Packing is HARD — a tile-wasting layout is not usable (user correction).
    pe = float(metrics.get("packing_efficiency", 0.0))
    c.append(ArtistGateCheck("packing_efficiency", "hard", pe >= g.packing_min, round(pe, 6),
                             g.packing_min, "UV-space utilisation (final = Blender CONCAVE pack)"))
    # Cylinder rectangularity is HARD (user correction: the trident must unwrap to
    # shaft/tine/cap rectangular strips — a blob/fragment cylinder is a FAIL).
    cbc = int(metrics.get("cylinder_blob_count", 0))
    c.append(ArtistGateCheck("cylinder_rectangular", "hard", cbc == 0, cbc, 0,
                             "every cylinder unwraps to a rectangular strip (no blob/fragment)"))

    # Quality.
    qual_le("stretch_score", metrics.get("stretch_score", 9.0), g.stretch_max, "area-stretch bar")
    qual_le("island_count", metrics.get("island_count", 999), g.island_count_max, "chart count cap")
    qual_le("vt_v_ratio", metrics.get("vt_v_ratio", 9.0), g.vt_v_max, "seam proliferation")
    tc = int(metrics.get("tendril_count", 99))
    c.append(ArtistGateCheck("tendril_count", "quality", tc <= g.tendril_count_max, tc,
                             g.tendril_count_max, "no width-≤2 finger chains"))
    qual_ge("convexity_p10", metrics.get("convexity_p10", 0.0), g.convexity_p10_min,
            "worst-decile chart convexity")
    return ArtistGateResult(checks=c)


def artist_report(layout_metrics: dict, seam_result, classes, density_rep: dict) -> dict:
    """Assemble the report-only artist-style block (plan §6 'Artist-Style Report
    Metrics'). Combines the A6 layout metrics, the part-type histogram, and the A7 density
    report. None of these gate shipping in v1 (calibrate ``readability_score`` first)."""
    type_hist: dict[str, int] = {}
    for c in classes:
        type_hist[c.type] = type_hist.get(c.type, 0) + 1
    return {
        **layout_metrics,
        "part_type_histogram": type_hist,
        "unknown_fallback_parts": type_hist.get("unknown", 0),
        "density": density_rep,
        "note": "report-only: part grouping / bands are intended-structure metadata, NOT "
                "forced onto the final UVs (final layout = Blender CONCAVE pack). "
                "orientation_consistency is measured on the final UVs (plan §6).",
    }
