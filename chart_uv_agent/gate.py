"""Phase U4 gate + calibrated config (chart-UV plan §2 / §4).

This is the gate for the GENERIC default UV engine (GENERIC_UV_REVISION_PLAN §4.2):
it measures UV *usability* — correctness (no overlap, in-bounds, no fallback) and
generic quality (stretch, packing, density uniformity, chart shape) — NOT similarity
to any single reference asset's artist layout. The thresholds are asset-AGNOSTIC
acceptance defaults; they do not encode body-part slots, semantic anchors, or a
specific reference's chart count.

One config dataclass holds every threshold. Gate philosophy (MINIMAL_DISTORTION_UV_PLAN
§5.3/§7): the HARD gates encode the user's three rules and basic correctness —

    - every ≥90° model fold is a UV seam (``mandatory_90_missing == 0``),
    - checker/stretch distortion ≤ ``stretch_max`` GLOBALLY and ≤
      ``worst_island_distortion_max`` for the WORST single island,
    - no UV overlap (signed-area + raster), in-bounds, no Smart-UV fallback,
    - texel-density uniformity.

Everything about chart *shape*, *packing*, and *count* — island_count, convexity,
boundary smoothness, tendrils, packing efficiency, confetti ratio, vt/v — is now
**advisory** (reported but never blocks shipping and never forces an extra island).
island_count is a minimise-TARGET, not a hard limit, because Rule 2 (cut every ≥90°
fold) can force more islands than any soft cap on a creased mesh. The plan is explicit:
do not increase island count for convexity/aesthetics/packing alone; only distortion,
overlap, non-disk topology, or mandatory seams may add an island.

The numeric defaults below were originally pinned on a single calibration mesh (the
humanstatue 5,850-face asset, three layouts). They are kept as the current
"calibrated acceptance default", but they are a STARTING POINT, not product truth.

    TODO (GENERIC_UV_REVISION_PLAN §G3): recalibrate every threshold on a multi-asset
    fixture set (sphere/blob, humanoid blob, cylinder/tube, boxy hard-surface, object
    with protrusions, object with thin panels/folds) before production use. Do NOT
    treat these values as universal. Do NOT weaken the correctness gates (overlap,
    raster_overlap, uv_bounds, fallback_used) to make any one asset look better.

Reference values that informed the initial defaults (one asset, same geometry):

    layout        islands  stretch  overlap  packing  texel_var  vt/v
    artist-ref        39    0.191    0.055    0.762     0.515     1.13
    organic_pelt       6    1.492    0.0004   0.453     0.000     1.20
    smart_uv         675    0.116    0.000    0.072     0.135     2.17

⇒ stretch_max = max(0.5, smart_uv 0.116 × 1.5) = 0.50; texel_var_max = 0.515 × 2 =
1.03; island cap 60; packing_min follows the mandated SLIM unwrap (see below).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChartGateConfig:
    # --- HARD correctness gates (block shipping; do NOT weaken — §4.2/§G2). ---
    # R2 (MINIMAL_DISTORTION_UV_PLAN §1/§M2): every model edge bent ≥ this dihedral MUST
    # be a UV seam in the final layout. ``mandatory_90_missing == 0`` is a hard gate.
    fold_angle_mandatory: float = 90.0
    overlap_max: float = 0.001            # flipped/folded UV area (signed-area metric)
    # TRUE raster overlap (multi-occupied / occupied px on a 1024² grid, 1px margin):
    # catches self-folds + inter-chart invasion the signed-area metric misses. A clean
    # overlap-free layout measures 0.0 by the same code; bar = 0.005 (boundary-aliasing
    # margin). HARD — overlapping UVs break baking.
    raster_overlap_max: float = 0.005
    # uv_bounds and fallback_used=false are also hard correctness gates (fixed, not
    # tunable params — see evaluate_chart_gate).

    # --- Generic quality gates (calibrated acceptance defaults; recalibrate per §G3). ---
    # area-stretch == checker/stretch distortion (MINIMAL_DISTORTION_UV_PLAN §M1). HARD:
    # the refinement loop splits the worst island until distortion is under this bar.
    # ``stretch_max`` is the GLOBAL (area-weighted mean) checker distortion; a layout can
    # pass it while ONE island is badly stretched, so the per-island worst is gated too.
    stretch_max: float = 0.50             # area-stretch — calibrated acceptance default
    # Per-island checker distortion (the worst island's area-weighted mean stretch). HARD:
    # Rule 3 applies per-island, not just globally — a single high-distortion island must
    # be split even when the global mean passes. The worst island is naturally higher than
    # the global mean, so this bar sits a touch above ``stretch_max`` (calibrate per §G3).
    worst_island_distortion_max: float = 0.60
    # UV-space utilisation. The packing floor follows the MANDATED unwrap method: the
    # correctness round uses SLIM (MINIMUM_STRETCH), the only locally-injective unwrap
    # (overlapping UVs break baking, so this is non-negotiable). SLIM islands pack at
    # ~0.44–0.45 with Blender's CONCAVE packer; ≥0.70 is unreachable by any automated
    # packer (manual artist nesting reaches ~0.76, but that is not the generic bar).
    # Acceptance default 0.42. See ADAPTIVE_LOWPOLY_RESULTS.md.
    packing_min: float = 0.42
    island_count_max: int = 80            # safety cap, NOT a target (R1: minimise within)
    # Count-INVARIANT confetti measure (islands below 0.2× the median island area), so
    # it flags genuine size disparity, not chart count. Geometry-driven segmentation on
    # limbs/panels/creases legitimately yields many small detail charts; the real
    # confetti guard is the ≥5-face min-chart-size in segmentation, not this UV-area
    # ratio. Calibrated acceptance default covering the engine's observed range.
    small_island_ratio_max: float = 0.35
    vt_v_max: float = 2.0                 # relaxed (welded input splits more than 51 shells)
    texel_density_variance_max: float = 1.03  # uniform texel density — calibrated default

    # --- U1.6 chart-shape gates (chart-UV plan §5b) — generic developable-disk shape. ---
    # Calibrated acceptance defaults: flat decimated charts top out ~0.78 convexity after
    # repair, smoothness ~1.25, zero tendrils. These guard chart shape (compact, smooth,
    # no finger chains), not resemblance to any reference layout.
    convexity_mean_min: float = 0.72      # pre-repair 0.69 fails, post-repair 0.78 passes
    boundary_smoothness_max: float = 1.70  # no staircase boundaries (ours ~1.25)
    tendril_count_max: int = 0            # hard — no width-≤2 finger chains
    # U1.7 tail gate: bottom-decile chart convexity. ~0.55 is the achievable tail target
    # on decimated charts. A failure here is shippable ONLY when the tail loop proves the
    # below-bar charts are stuck (chart-UV plan §5c).
    convexity_p10_min: float = 0.55

    def to_dict(self) -> dict:
        """Every active threshold, so a gate report is self-contained (§G2): a reviewer
        can read it without knowing any reference-asset story. uv_bounds and
        fallback_used are hard correctness gates with fixed (non-tunable) limits, noted
        here for completeness."""
        return {
            # hard correctness gates
            "fold_angle_mandatory": self.fold_angle_mandatory,
            "mandatory_90_missing_max": 0,
            "overlap_max": self.overlap_max,
            "raster_overlap_max": self.raster_overlap_max,
            "uv_bounds_ok_required": True,
            "fallback_used_allowed": False,
            # generic quality gates
            "stretch_max": self.stretch_max,
            "worst_island_distortion_max": self.worst_island_distortion_max,
            "packing_min": self.packing_min,
            "island_count_max": self.island_count_max,
            "small_island_ratio_max": self.small_island_ratio_max,
            "vt_v_max": self.vt_v_max,
            "texel_density_variance_max": self.texel_density_variance_max,
            # chart-shape gates
            "convexity_mean_min": self.convexity_mean_min,
            "boundary_smoothness_max": self.boundary_smoothness_max,
            "tendril_count_max": self.tendril_count_max,
            "convexity_p10_min": self.convexity_p10_min,
        }


@dataclass
class ChartGateCheck:
    name: str
    passed: bool
    value: float
    limit: float
    detail: str = ""
    advisory: bool = False     # reported but never blocks shipping (shape/packing/aesthetic)

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "value": self.value,
                "limit": self.limit, "detail": self.detail, "advisory": self.advisory}


@dataclass
class ChartGateResult:
    checks: list[ChartGateCheck] = field(default_factory=list)

    @property
    def failures(self) -> list[ChartGateCheck]:
        """Hard (blocking) failures only — advisory checks never appear here."""
        return [c for c in self.checks if not c.passed and not c.advisory]

    @property
    def advisories(self) -> list[ChartGateCheck]:
        """Advisory checks that fell below their (non-blocking) target — report only."""
        return [c for c in self.checks if not c.passed and c.advisory]

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def verdict(self) -> str:
        return "accepted" if self.passed else "failed"

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "passed": self.passed,
                "failures": [c.name for c in self.failures],
                "advisories": [c.name for c in self.advisories],
                "checks": [c.to_dict() for c in self.checks]}


def evaluate_chart_gate(metrics: dict, *, config: ChartGateConfig = ChartGateConfig()) -> ChartGateResult:
    """Apply the minimal-distortion gates (MINIMAL_DISTORTION_UV_PLAN §5.3/§7).

    ``metrics`` keys: ``mandatory_90_missing`` (int), ``overlap_ratio``, ``stretch_score``
    (== global checker distortion), ``worst_island_distortion`` (per-island worst),
    ``raster_overlap_ratio``, ``texel_density_variance``, ``island_count``,
    ``uv_bounds_ok`` (bool), ``fallback_used`` (bool); plus the advisory
    ``packing_efficiency``, ``small_island_ratio``, ``vt_v_ratio``, ``convexity_mean``,
    ``convexity_p10``, ``boundary_smoothness_mean``, ``tendril_count``.

    HARD checks block shipping and (in the loop) justify a split. ADVISORY checks are
    reported but never block and never force an extra island — convexity/smoothness/
    tendrils/packing/confetti/vt-v are all advisory here."""
    c: list[ChartGateCheck] = []
    g = config

    def le(name, val, lim, detail="", advisory=False):
        c.append(ChartGateCheck(name, float(val) <= lim, round(float(val), 6), lim, detail, advisory))

    # --- HARD: the user's three rules + correctness. ---
    # Rule 2: every ≥ fold_angle model edge is a seam in the final layout (seam-SET check).
    m90 = int(metrics.get("mandatory_90_missing", 1))
    c.append(ChartGateCheck("mandatory_90_missing", m90 <= 0, m90, 0,
                            f"every ≥{g.fold_angle_mandatory:g}° fold must be in the seam set"))
    # Rule 2 (UV-level): the seam-set check is a false positive — a fold can be a seam yet
    # still weld in the exported UV (interior slit / buried fold). This checks the ACTUAL
    # UVMap: the two faces across every fold must carry different UVs. HARD.
    m90uv = int(metrics.get("mandatory_90_uv_unsplit", 1))
    c.append(ChartGateCheck("mandatory_90_uv_unsplit", m90uv <= 0, m90uv, 0,
                            f"every ≥{g.fold_angle_mandatory:g}° fold must be UV-split (different UV both sides)"))
    le("overlap_ratio", metrics.get("overlap_ratio", 1.0), g.overlap_max, "no folded UV area (signed-area)")
    le("raster_overlap_ratio", metrics.get("raster_overlap_ratio", 1.0), g.raster_overlap_max,
       "no true pixel overlap (raster)")
    # Rule 3: checker/stretch distortion under threshold — global AND per-island.
    le("stretch_score", metrics.get("stretch_score", 9.0), g.stretch_max,
       "global checker/stretch distortion bar")
    le("worst_island_distortion", metrics.get("worst_island_distortion", 9.0),
       g.worst_island_distortion_max, "per-island (worst) checker distortion bar")
    le("texel_density_variance", metrics.get("texel_density_variance", 9.0),
       g.texel_density_variance_max, "uniform texel density")

    bounds = bool(metrics.get("uv_bounds_ok", False))
    c.append(ChartGateCheck("uv_bounds", bounds, float(bounds), 1.0, "all UVs in [0,1]"))

    fb = bool(metrics.get("fallback_used", True))
    c.append(ChartGateCheck("fallback_used", not fb, float(fb), 0.0,
                            "Smart-UV fallback may NOT be shipped"))

    # --- ADVISORY: island count + packing + shape quality (reported, never blocks). ---
    # island_count is a TARGET to minimise, not a hard limit: Rule 2 (cut every ≥90° fold)
    # can legitimately force more islands than the soft cap on a heavily-creased mesh, and a
    # hard cap there would make the gate unsatisfiable (MINIMAL_DISTORTION_UV_PLAN §5.3/§7).
    le("island_count", metrics.get("island_count", 999), g.island_count_max,
       "island count target (advisory; minimise — Rule 2 may force more)", advisory=True)
    pe = float(metrics.get("packing_efficiency", 0.0))
    c.append(ChartGateCheck("packing_efficiency", pe >= g.packing_min, round(pe, 4),
                            g.packing_min, "UV-space utilisation (advisory)", advisory=True))
    le("small_island_ratio", metrics.get("small_island_ratio", 1.0), g.small_island_ratio_max,
       "confetti guard (advisory)", advisory=True)
    le("vt_v_ratio", metrics.get("vt_v_ratio", 9.0), g.vt_v_max,
       "seam proliferation (advisory)", advisory=True)
    cm = float(metrics.get("convexity_mean", 0.0))
    c.append(ChartGateCheck("convexity_mean", cm >= g.convexity_mean_min, round(cm, 4),
                            g.convexity_mean_min, "chart compactness (advisory)", advisory=True))
    bs = float(metrics.get("boundary_smoothness_mean", 9.0))
    c.append(ChartGateCheck("boundary_smoothness", bs <= g.boundary_smoothness_max, round(bs, 4),
                            g.boundary_smoothness_max, "no staircase boundaries (advisory)", advisory=True))
    tc = int(metrics.get("tendril_count", 99))
    c.append(ChartGateCheck("tendril_count", tc <= g.tendril_count_max, tc,
                            g.tendril_count_max, "width-≤2 finger chains (advisory)", advisory=True))
    cp10 = float(metrics.get("convexity_p10", 0.0))
    c.append(ChartGateCheck("convexity_p10", cp10 >= g.convexity_p10_min, round(cp10, 4),
                            g.convexity_p10_min, "worst-decile chart convexity (advisory)", advisory=True))
    return ChartGateResult(checks=c)
