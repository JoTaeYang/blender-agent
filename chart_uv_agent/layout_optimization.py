"""UV Layout Optimization Loop (UV_LAYOUT_OPTIMIZATION_LOOP_PLAN).

The job here is NOT seam generation. The seam / reference-UV island boundary is already
decided and FIXED. Given that frozen seam set, this module explores several relax /
average-scale / rotate / pack candidates, scores each by checker distortion + texel
density + overlap + packing, and selects the best UV *layout* — without ever changing the
seam set, adding seams, or re-segmenting (plan §2, §3.2, §16).

Two layers:

- PURE (Blender-free, unit-tested): the config + candidate dataclasses, the candidate
  axis-product, the hard-reject rule (``candidate_is_valid``), the layout score
  (``layout_score``), and the baseline-aware best pick (``select_best_candidate``).
- DRIVER (``run_layout_optimization``): runs a caller-supplied ``measure_candidate``
  callback once per candidate spec and assembles a :class:`LayoutOptimizationResult`. The
  callback is where Blender unwrap/pack/measure lives, so this module stays import-safe.

Plan §3.1/§7.4: in ``user_reference`` mode the ``mandatory_90_*`` audits are report-only —
they are NEVER a reject condition here. The user / reference seam set is the source of
truth; mandatory 90° folds and the mandatory UV hard gate are not applied (plan §16).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math


def default_score_weights() -> dict[str, float]:
    """Score weights (plan §7.4 + MVP3 §2 Goal C). Lower score == better layout. Checker
    stretch dominates, then the worst single island; texel-density uniformity is weighted
    heavily so a candidate that worsens density is strongly rejected (MVP3 §2 Goal C item
    2), and packing efficiency carries a larger NEGATIVE weight so a real packing gain wins
    (MVP3 §2 Goal C item 1 — Blender pack deltas were too small to ever swing the prior
    -1.5 weight)."""
    return {
        "stretch_score": 4.0,
        "worst_island_distortion": 3.0,
        "texel_density_variance": 4.0,
        "raster_overlap_ratio": 2.0,
        "overlap_ratio": 1.0,
        "packing_efficiency": -3.0,
        "small_island_ratio": 0.2,
    }


# Meaningful-improvement thresholds (MVP3 §2 Goal C "권장 기준"). A layout change is only
# "meaningful" to the user when packing jumps, texel variance drops sharply, OR the score
# improves by a clear margin — otherwise it is a minor/packing-only change.
MEANINGFUL_PACKING_DELTA = 0.05
MEANINGFUL_TEXEL_FACTOR = 0.75
MEANINGFUL_SCORE_RATIO = 0.10
# Packing efficiency the plan asks the optimizer to reach for the pottery asset (MVP3 §2
# Goal B / §7 acceptance). Used only to phrase the honest UI verdict, never to gate-pass.
PACKING_GOOD_TARGET = 0.65
PACKING_POOR = 0.50


@dataclass(frozen=True)
class LayoutOptimizationConfig:
    """Search space + scoring for the layout loop (plan §6.1). ``enabled=False`` by default
    so the existing single-unwrap path is byte-for-byte unchanged unless a caller opts in."""
    enabled: bool = False
    mode: str = "user_reference"
    unwrap_methods: tuple[str, ...] = ("MINIMUM_STRETCH", "ANGLE_BASED")
    angle_based_minimize_iters: tuple[int, ...] = (0, 10, 30)
    margins: tuple[float, ...] = (0.002, 0.005, 0.01)
    pack_shapes: tuple[str, ...] = ("CONCAVE", "AABB")
    rotate_options: tuple[bool, ...] = (True,)
    average_scale: bool = True
    # Island-level custom packing backends (MVP3 §2 Goal B / §4). ``"blender"`` is the
    # built-in CONCAVE/AABB ``pack_islands`` family (the ``pack_shapes`` product above);
    # ``"maxrects"`` / ``"shelf"`` run the geometry packer over the read-back UVs, with an
    # optional density-normalize + long-island orientation pre-pass.
    pack_backends: tuple[str, ...] = ("maxrects", "shelf")
    custom_unwrap_methods: tuple[str, ...] = ("MINIMUM_STRETCH",)
    custom_margins: tuple[float, ...] = (0.002, 0.005)
    orient_options: tuple[bool, ...] = (False, True)
    density_normalize: bool = True
    max_candidates: int = 24
    require_no_overlap: bool = True
    # Baseline-retention tolerances (plan §7.5): a candidate may only WIN if it does not
    # regress the baseline beyond these factors, and only then if it beats the baseline
    # score by at least ``min_score_improvement`` (1%).
    min_score_improvement: float = 0.01
    stretch_regression_factor: float = 1.05
    worst_regression_factor: float = 1.05
    texel_regression_factor: float = 1.10
    packing_regression_abs: float = 0.02
    score_weights: dict[str, float] = field(default_factory=default_score_weights)


# The default first-unwrap config the user-seam path already ships (plan §7.2 candidate #1).
# Used so the baseline measure shares an identical spec with one candidate, and so a
# "keep baseline" decision re-applies a known config.
BASELINE_SPEC: dict = {
    "unwrap_method": "MINIMUM_STRETCH",
    "minimize_iters": 0,
    "margin": 0.005,
    "pack_shape": "CONCAVE",
    "rotate": True,
    "average_scale": True,
}


@dataclass
class LayoutCandidate:
    id: str
    unwrap_method: str
    minimize_iters: int
    margin: float
    pack_shape: str
    rotate: bool
    metrics: dict
    score: float
    accepted: bool          # passed the hard-reject filter (a valid candidate)
    reason: str = ""        # why selected ("best_score") / rejected (the failing rule)
    pack_backend: str = "blender"        # "blender" | "maxrects" | "shelf" (MVP3 §2 Goal B)
    orient_long_islands: bool = False
    density_normalize: bool = True

    def to_dict(self) -> dict:
        return {"id": self.id, "unwrap_method": self.unwrap_method,
                "minimize_iters": self.minimize_iters, "margin": self.margin,
                "pack_shape": self.pack_shape, "rotate": self.rotate,
                "pack_backend": self.pack_backend,
                "orient_long_islands": self.orient_long_islands,
                "density_normalize": self.density_normalize,
                "metrics": self.metrics, "score": round(float(self.score), 6),
                "accepted": self.accepted, "reason": self.reason}


@dataclass
class LayoutOptimizationResult:
    selected_candidate_id: str
    candidates: list[LayoutCandidate]
    before_metrics: dict
    after_metrics: dict
    score_before: float
    score_after: float
    selected_spec: dict
    kept_baseline: bool

    def improvement(self) -> dict:
        """The packing/stretch/texel deltas + meaningful verdict (MVP3 §2 Goal C)."""
        return compute_improvement(self.before_metrics, self.after_metrics,
                                   self.score_before, self.score_after)

    def verdict(self) -> str:
        """The honest one-word verdict key (MVP3 §2 Goal D)."""
        return improvement_verdict(self.improvement(), kept_baseline=self.kept_baseline,
                                   packing_after=self.after_metrics.get("packing_efficiency"))

    def report(self) -> dict:
        return {"enabled": True, "selected_candidate_id": self.selected_candidate_id,
                "kept_baseline": self.kept_baseline,
                "score_before": round(float(self.score_before), 6),
                "score_after": round(float(self.score_after), 6),
                "before_metrics": self.before_metrics, "after_metrics": self.after_metrics,
                "improvement": self.improvement(), "verdict": self.verdict(),
                "candidates": [c.to_dict() for c in self.candidates]}

    def summary(self) -> dict:
        """Compact summary for ``seam_report.json`` (plan §11)."""
        return {"enabled": True, "selected_candidate_id": self.selected_candidate_id,
                "kept_baseline": self.kept_baseline,
                "candidate_count": len(self.candidates),
                "packing_efficiency_before": _g(self.before_metrics, "packing_efficiency"),
                "packing_efficiency_after": _g(self.after_metrics, "packing_efficiency"),
                "stretch_before": _g(self.before_metrics, "stretch_score"),
                "stretch_after": _g(self.after_metrics, "stretch_score"),
                "worst_island_before": _g(self.before_metrics, "worst_island_distortion"),
                "worst_island_after": _g(self.after_metrics, "worst_island_distortion"),
                "improvement": self.improvement(), "verdict": self.verdict()}


def _g(m: dict, key: str):
    v = m.get(key)
    return round(float(v), 6) if isinstance(v, (int, float)) else v


def make_config(preset: str = "user_reference", *, max_candidates: int | None = None,
                enabled: bool = True) -> LayoutOptimizationConfig:
    """Build a config from a named preset (plan §9.1/§10 — first milestone ships one
    preset, ``user_reference``). ``max_candidates`` overrides the preset cap."""
    base = LayoutOptimizationConfig(enabled=enabled, mode="user_reference")
    if max_candidates is not None:
        base = LayoutOptimizationConfig(
            enabled=enabled, mode=base.mode, unwrap_methods=base.unwrap_methods,
            angle_based_minimize_iters=base.angle_based_minimize_iters, margins=base.margins,
            pack_shapes=base.pack_shapes, rotate_options=base.rotate_options,
            average_scale=base.average_scale, max_candidates=int(max_candidates),
            require_no_overlap=base.require_no_overlap)
    return base


def candidate_specs(config: LayoutOptimizationConfig) -> list[dict]:
    """The candidate axis-product (plan §7.2 + MVP3 §2 Goal B / §4), capped at
    ``config.max_candidates``.

    Two families:

    - BLENDER pack: the original ``unwrap_methods × minimize_iters × margins × pack_shapes``
      product. ``MINIMUM_STRETCH`` (SLIM) is locally injective; we do NOT bolt Blender's
      ``minimize_stretch`` onto it (it is non-injective and would re-fold) — so SLIM
      candidates always carry ``minimize_iters=0``; ``minimize_stretch`` iterations apply
      ONLY to ``ANGLE_BASED`` (plan §7.2 caveat).
    - CUSTOM pack (MVP3 §2 Goal B): ``custom_unwrap_methods × custom_margins × pack_backends
      × orient_options`` with per-island density normalize, run through the MaxRects/shelf
      geometry packer. These yield the meaningful packing gains the plan asks for.

    The first spec is always the baseline (so the baseline measure and candidate #0 share a
    config). Custom candidates are emitted right after the baseline — BEFORE the large
    Blender product — so they survive the ``max_candidates`` slice (the Blender product alone
    already fills the default cap, plan §0)."""
    specs: list[dict] = []
    seen: set[tuple] = set()

    def add(method, iters, margin, shape, rotate, *, backend="blender",
            orient=False, density=None):
        density = config.average_scale if density is None else density
        key = (method, iters, margin, shape, rotate, backend, orient, density)
        if key in seen:
            return
        seen.add(key)
        specs.append({"unwrap_method": method, "minimize_iters": iters, "margin": margin,
                      "pack_shape": shape, "rotate": rotate, "average_scale": config.average_scale,
                      "pack_backend": backend, "orient_long_islands": orient,
                      "density_normalize": density})

    # #0 baseline (Blender CONCAVE).
    add(*[BASELINE_SPEC[k] for k in
          ("unwrap_method", "minimize_iters", "margin", "pack_shape", "rotate")])
    # Custom-pack candidates first (the high-value family, MVP3 §2 Goal B).
    for method in config.custom_unwrap_methods:
        for margin in config.custom_margins:
            for backend in config.pack_backends:
                for orient in config.orient_options:
                    add(method, 0, margin, "CONCAVE", True, backend=backend,
                        orient=orient, density=config.density_normalize)
    # Blender-pack product.
    for method in config.unwrap_methods:
        iter_opts = config.angle_based_minimize_iters if method == "ANGLE_BASED" else (0,)
        for iters in iter_opts:
            for margin in config.margins:
                for shape in config.pack_shapes:
                    for rotate in config.rotate_options:
                        add(method, iters, margin, shape, rotate)
    return specs[:max(1, config.max_candidates)]


def spec_id(spec: dict) -> str:
    """Stable, readable candidate id (plan §11 / MVP3 §2 Goal B examples). Blender candidates
    keep the original ``<method>_<shape>_m<margin>`` key (so ``slim_concave_m005`` is
    unchanged); custom-pack candidates read ``<method>_custom[_orient]_<backend>_m<margin>``
    (e.g. ``slim_custom_maxrects_m002`` / ``slim_custom_orient_maxrects_m002``)."""
    method = "slim" if spec["unwrap_method"] == "MINIMUM_STRETCH" else "abf"
    backend = spec.get("pack_backend", "blender")
    margin = f"m{int(round(spec['margin'] * 1000)):03d}"
    if backend == "blender":
        parts = [method, str(spec["pack_shape"]).lower(), margin]
    else:
        parts = [method, "custom"]
        if spec.get("orient_long_islands"):
            parts.append("orient")
        parts += [backend, margin]
    if spec["minimize_iters"]:
        parts.append(f"min{int(spec['minimize_iters'])}")
    if not spec.get("rotate", True):
        parts.append("norot")
    return "_".join(parts)


def compute_improvement(before_metrics: dict, after_metrics: dict,
                        score_before: float, score_after: float) -> dict:
    """Quantify the optimization gain (MVP3 §2 Goal C). Returns the packing / stretch /
    texel-density deltas plus a ``meaningful`` verdict using the plan's "권장 기준":
    meaningful iff packing rose by ≥ :data:`MEANINGFUL_PACKING_DELTA`, OR texel variance
    dropped to ≤ :data:`MEANINGFUL_TEXEL_FACTOR` of before, OR the score improved by ≥
    :data:`MEANINGFUL_SCORE_RATIO`."""
    def g(m, k):
        v = (m or {}).get(k)
        return float(v) if isinstance(v, (int, float)) else 0.0

    pb, pa = g(before_metrics, "packing_efficiency"), g(after_metrics, "packing_efficiency")
    sb, sa = g(before_metrics, "stretch_score"), g(after_metrics, "stretch_score")
    tb, ta = g(before_metrics, "texel_density_variance"), g(after_metrics, "texel_density_variance")
    packing_delta = pa - pb
    score_gain = float(score_before) - float(score_after)
    score_ratio = score_gain / abs(score_before) if abs(score_before) > 1e-9 else 0.0

    meaningful_packing = packing_delta >= MEANINGFUL_PACKING_DELTA
    meaningful_texel = tb > 1e-9 and ta <= tb * MEANINGFUL_TEXEL_FACTOR
    meaningful_score = score_ratio >= MEANINGFUL_SCORE_RATIO
    return {
        "meaningful": bool(meaningful_packing or meaningful_texel or meaningful_score),
        "packing_delta": round(packing_delta, 6),
        "stretch_delta": round(sa - sb, 6),
        "texel_density_delta": round(ta - tb, 6),
        "score_ratio": round(score_ratio, 6),
        "packing_meaningful": bool(meaningful_packing),
        "texel_meaningful": bool(meaningful_texel),
        "score_meaningful": bool(meaningful_score),
    }


def improvement_verdict(improvement: dict, *, kept_baseline: bool,
                        packing_after: float | None) -> str:
    """Map an :func:`compute_improvement` result to one honest UI verdict key (MVP3 §2 Goal D
    status 문구). One of: ``meaningful`` / ``minor_packing_only`` / ``baseline_retained`` /
    ``needs_better_packing`` / ``consider_seam_edits``."""
    if improvement.get("meaningful"):
        return "meaningful"
    pa = packing_after if isinstance(packing_after, (int, float)) else None
    if pa is not None and pa < PACKING_POOR:
        return "consider_seam_edits"
    if pa is not None and pa < PACKING_GOOD_TARGET:
        return "needs_better_packing"
    if kept_baseline:
        return "baseline_retained"
    if improvement.get("packing_delta", 0.0) > 0:
        return "minor_packing_only"
    return "baseline_retained"


def candidate_is_valid(metrics: dict, *, mode: str = "user_reference",
                       config: LayoutOptimizationConfig | None = None) -> tuple[bool, str]:
    """Hard-reject filter (plan §7.4). Returns ``(valid, reason)``. A candidate is rejected
    on a correctness failure only: out-of-bounds UVs, true (raster) overlap, signed-area
    overlap, or a Smart-UV fallback.

    Plan §3.1/§7.4: in ``user_reference`` mode the ``mandatory_90_missing`` /
    ``mandatory_90_uv_unsplit`` audits are NEVER a reject condition — the user/reference
    seam set is authoritative, so those are report-only here."""
    cfg = config or LayoutOptimizationConfig()
    for key in (
        "raster_overlap_ratio", "overlap_ratio", "stretch_score",
        "worst_island_distortion", "texel_density_variance", "packing_efficiency",
        "small_island_ratio",
    ):
        val = metrics.get(key)
        if isinstance(val, (int, float)) and not math.isfinite(float(val)):
            return False, f"non_finite_{key}"
    if not bool(metrics.get("uv_bounds_ok", False)):
        return False, "uv_bounds_ok_false"
    if bool(metrics.get("fallback_used", False)):
        return False, "fallback_used"
    if cfg.require_no_overlap:
        ro = float(metrics.get("raster_overlap_ratio", 1.0))
        if ro > _raster_max(cfg):
            return False, "raster_overlap"
        if float(metrics.get("overlap_ratio", 1.0)) > _overlap_max(cfg):
            return False, "overlap"
    return True, "valid"


def _raster_max(cfg) -> float:
    return float(getattr(cfg, "raster_overlap_max", 0.005))


def _overlap_max(cfg) -> float:
    return float(getattr(cfg, "overlap_max", 0.001))


def layout_score(metrics: dict, weights: dict[str, float] | None = None) -> float:
    """Weighted layout score (plan §7.4). LOWER is better. Missing metrics contribute 0
    (their weight times 0), so a partial metric dict still scores deterministically."""
    w = weights or default_score_weights()
    return float(sum(coef * float(metrics.get(key, 0.0)) for key, coef in w.items()))


def _no_regression(metrics: dict, baseline: dict, config: LayoutOptimizationConfig) -> bool:
    """Plan §7.5 baseline-retention guard: a candidate may only replace the baseline if it
    does not regress stretch / worst-island / texel-variance beyond the tolerance factors
    and does not drop packing efficiency by more than the absolute slack."""
    def b(key, default=0.0):
        v = baseline.get(key, default)
        return float(v) if isinstance(v, (int, float)) else default

    if float(metrics.get("stretch_score", 9.0)) > b("stretch_score") * config.stretch_regression_factor + 1e-9:
        return False
    if float(metrics.get("worst_island_distortion", 9.0)) > \
            b("worst_island_distortion") * config.worst_regression_factor + 1e-9:
        return False
    if float(metrics.get("texel_density_variance", 9.0)) > \
            b("texel_density_variance") * config.texel_regression_factor + 1e-9:
        return False
    if float(metrics.get("packing_efficiency", 0.0)) < \
            b("packing_efficiency") - config.packing_regression_abs - 1e-9:
        return False
    return True


def select_best_candidate(candidates: list[LayoutCandidate], baseline_metrics: dict,
                          baseline_score: float, config: LayoutOptimizationConfig
                          ) -> tuple[str | None, dict]:
    """Pick the candidate to ship (plan §7.5). Returns ``(selected_id_or_None, info)``.

    1. Keep only hard-valid candidates that also satisfy the no-regression guard vs the
       baseline.
    2. Among those, take the lowest score.
    3. Ship it ONLY if it beats the baseline score by ≥ ``min_score_improvement`` (1%);
       otherwise return ``None`` → keep the baseline layout (a marginal win is not worth
       changing the shipped layout).

    Scores can be negative (packing carries a negative weight), so "≥1% better" is measured
    against the score *gap*, not a ratio of possibly-negative numbers: improvement =
    ``baseline_score - candidate_score`` must clear ``|baseline_score| * min_improvement``."""
    eligible = [c for c in candidates
                if c.accepted and _no_regression(c.metrics, baseline_metrics, config)]
    info = {"eligible_ids": [c.id for c in eligible], "baseline_score": round(baseline_score, 6)}
    if not eligible:
        info["reason"] = "no_eligible_candidate"
        return None, info
    best = min(eligible, key=lambda c: c.score)
    threshold = abs(baseline_score) * config.min_score_improvement
    improvement = baseline_score - best.score
    info["best_id"] = best.id
    info["best_score"] = round(best.score, 6)
    info["improvement"] = round(improvement, 6)
    info["threshold"] = round(threshold, 6)
    if improvement < threshold:
        info["reason"] = "below_min_improvement"
        return None, info
    info["reason"] = "best_score"
    return best.id, info


def run_layout_optimization(measure_candidate, baseline_metrics: dict,
                            config: LayoutOptimizationConfig, *, mode: str | None = None
                            ) -> LayoutOptimizationResult:
    """Driver (plan §7). ``measure_candidate(spec) -> metrics dict`` runs ONE candidate's
    unwrap/pack/measure in Blender and returns the flat metric dict (or ``None`` on failure;
    a failed candidate is dropped). The seam set is fixed by the caller and must not change.

    Returns a :class:`LayoutOptimizationResult`. The caller is responsible for RE-APPLYING
    the selected spec (or the baseline spec) afterwards, because each candidate run
    overwrites the object's UV — the last candidate measured is NOT necessarily the winner."""
    mode = mode or config.mode
    baseline_score = layout_score(baseline_metrics, config.score_weights)
    candidates: list[LayoutCandidate] = []
    for spec in candidate_specs(config):
        metrics = measure_candidate(spec)
        if metrics is None:
            continue
        valid, reason = candidate_is_valid(metrics, mode=mode, config=config)
        candidates.append(LayoutCandidate(
            id=spec_id(spec), unwrap_method=spec["unwrap_method"],
            minimize_iters=spec["minimize_iters"], margin=spec["margin"],
            pack_shape=spec["pack_shape"], rotate=spec["rotate"], metrics=metrics,
            score=layout_score(metrics, config.score_weights),
            accepted=valid, reason=("" if valid else reason),
            pack_backend=spec.get("pack_backend", "blender"),
            orient_long_islands=bool(spec.get("orient_long_islands", False)),
            density_normalize=bool(spec.get("density_normalize", config.average_scale))))

    selected_id, info = select_best_candidate(candidates, baseline_metrics, baseline_score, config)
    by_id = {c.id: c for c in candidates}
    if selected_id is None:
        sel = next((c for c in candidates if c.id == spec_id(BASELINE_SPEC)), None)
        after_metrics = sel.metrics if sel else baseline_metrics
        score_after = sel.score if sel else baseline_score
        selected_spec = dict(BASELINE_SPEC)
        kept_baseline = True
        chosen_id = sel.id if sel else "baseline"
    else:
        sel = by_id[selected_id]
        sel.reason = "best_score"
        after_metrics = sel.metrics
        score_after = sel.score
        selected_spec = {"unwrap_method": sel.unwrap_method, "minimize_iters": sel.minimize_iters,
                         "margin": sel.margin, "pack_shape": sel.pack_shape, "rotate": sel.rotate,
                         "average_scale": config.average_scale,
                         "pack_backend": sel.pack_backend,
                         "orient_long_islands": sel.orient_long_islands,
                         "density_normalize": sel.density_normalize}
        kept_baseline = False
        chosen_id = selected_id

    return LayoutOptimizationResult(
        selected_candidate_id=chosen_id, candidates=candidates,
        before_metrics=baseline_metrics, after_metrics=after_metrics,
        score_before=baseline_score, score_after=score_after,
        selected_spec=selected_spec, kept_baseline=kept_baseline)


__all__ = ["LayoutOptimizationConfig", "LayoutCandidate", "LayoutOptimizationResult",
           "BASELINE_SPEC", "default_score_weights", "make_config", "candidate_specs",
           "spec_id", "candidate_is_valid", "layout_score", "select_best_candidate",
           "run_layout_optimization", "compute_improvement", "improvement_verdict",
           "MEANINGFUL_PACKING_DELTA", "MEANINGFUL_TEXEL_FACTOR", "MEANINGFUL_SCORE_RATIO",
           "PACKING_GOOD_TARGET", "PACKING_POOR"]
