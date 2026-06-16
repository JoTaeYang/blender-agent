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
    """Plan §7.4 score weights. Lower score == better layout. Checker stretch dominates,
    then the worst single island, then texel-density uniformity and overlap; packing
    efficiency carries a NEGATIVE weight (higher packing lowers the score)."""
    return {
        "stretch_score": 4.0,
        "worst_island_distortion": 3.0,
        "texel_density_variance": 2.0,
        "raster_overlap_ratio": 2.0,
        "overlap_ratio": 1.0,
        "packing_efficiency": -1.5,
        "small_island_ratio": 0.2,
    }


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

    def to_dict(self) -> dict:
        return {"id": self.id, "unwrap_method": self.unwrap_method,
                "minimize_iters": self.minimize_iters, "margin": self.margin,
                "pack_shape": self.pack_shape, "rotate": self.rotate,
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

    def report(self) -> dict:
        return {"enabled": True, "selected_candidate_id": self.selected_candidate_id,
                "kept_baseline": self.kept_baseline,
                "score_before": round(float(self.score_before), 6),
                "score_after": round(float(self.score_after), 6),
                "before_metrics": self.before_metrics, "after_metrics": self.after_metrics,
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
                "worst_island_after": _g(self.after_metrics, "worst_island_distortion")}


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
    """The candidate axis-product (plan §7.2), capped at ``config.max_candidates``.

    ``MINIMUM_STRETCH`` (SLIM) is locally injective; we do NOT bolt Blender's
    ``minimize_stretch`` onto it (it is non-injective and would re-fold) — so SLIM
    candidates always carry ``minimize_iters=0``. ``minimize_stretch`` iteration counts
    apply ONLY to ``ANGLE_BASED`` (plan §7.2 caveat). The first spec is the baseline so the
    baseline measure and candidate #0 are the same config."""
    specs: list[dict] = []
    seen: set[tuple] = set()

    def add(method, iters, margin, shape, rotate):
        key = (method, iters, margin, shape, rotate)
        if key in seen:
            return
        seen.add(key)
        specs.append({"unwrap_method": method, "minimize_iters": iters, "margin": margin,
                      "pack_shape": shape, "rotate": rotate,
                      "average_scale": config.average_scale})

    add(*[BASELINE_SPEC[k] for k in
          ("unwrap_method", "minimize_iters", "margin", "pack_shape", "rotate")])
    for method in config.unwrap_methods:
        iter_opts = config.angle_based_minimize_iters if method == "ANGLE_BASED" else (0,)
        for iters in iter_opts:
            for margin in config.margins:
                for shape in config.pack_shapes:
                    for rotate in config.rotate_options:
                        add(method, iters, margin, shape, rotate)
    return specs[:max(1, config.max_candidates)]


def spec_id(spec: dict) -> str:
    """Stable, readable candidate id (plan §11 report key, e.g. ``slim_concave_m005``)."""
    method = "slim" if spec["unwrap_method"] == "MINIMUM_STRETCH" else "abf"
    shape = str(spec["pack_shape"]).lower()
    margin = f"m{int(round(spec['margin'] * 1000)):03d}"
    parts = [method, shape, margin]
    if spec["minimize_iters"]:
        parts.append(f"min{int(spec['minimize_iters'])}")
    if not spec.get("rotate", True):
        parts.append("norot")
    return "_".join(parts)


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
            accepted=valid, reason=("" if valid else reason)))

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
                         "average_scale": config.average_scale}
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
           "run_layout_optimization"]
