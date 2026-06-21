"""Shared worker <-> Electron JSON contract for the MVP 3 Generate + Optimize app.

Single source of truth for the JSON shapes the Electron app and the
Python/Blender ``generate_uv_from_seams`` worker exchange
(``docs/ELECTRON_UV_REVIEW_APP_MVP3_PRODUCTION_PLAN.ko.md`` §4, §5, §6, §9).

Like ``worker/app_uv_review_contract.py`` and ``worker/app_seam_spec_contract.py``
it is intentionally **pure Python** (no ``bpy``, no NumPy, and no
``chart_uv_agent`` import) so it loads stand-alone in unit tests and stays a tiny,
self-contained dependency. ``app/shared/contracts/uvGenerate.ts`` is its
TypeScript mirror.

MVP 3 product rules encoded here (plan §1, §6, §13):

- The MVP 2 ``active_user_seam_spec`` is the source of truth. The worker runs the
  chart engine in STRICT user/reference mode — ``auto_refine_user_seams`` /
  ``repair_user_seams`` / ``enforce_user_mandatory`` / ``gate_user_mandatory`` all
  default **false** so the seam set is never silently changed (plan §1, §6, §14).
- Seam integrity is a HARD acceptance: ``auto_added_seams == 0`` and
  ``final_seam_count == user_seam_count`` (plan §6, §13). A run that breaks it is
  ``needs_user_review`` (or ``failed``) and must NOT replace
  ``work/uv/selected_uv.blend`` (plan §6).
- The mandatory-90 audits are report-only diagnostics here; they never gate-fail a
  user/reference run (plan §1, §6).
- Layout optimization selects the best SAFE candidate (no raster overlap, in
  bounds) over a FIXED seam set, or explicitly keeps the baseline (plan §5, §14).
- Failures are always representable as JSON (never only stdout/stderr).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA_VERSION = 1

# --- App-facing command names (stable contract, plan §4) -------------------
CMD_GENERATE_UV_FROM_SEAMS = "generate_uv_from_seams"
CMD_SELECT_UV_CANDIDATE = "select_uv_candidate"
COMMANDS = (CMD_GENERATE_UV_FROM_SEAMS, CMD_SELECT_UV_CANDIDATE)

# --- Run status lifecycle (plan §9 status.json) ----------------------------
# ``needs_user_review`` is the MVP-3-specific terminal status for a run that
# completed but broke seam integrity or still has blocking overlap: the UI sends
# the user back to the MVP 2 Seam Editor and the selected UV is NOT shipped
# (plan §6 "selected UV model must not replace work/uv/selected_uv.blend").
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_ACCEPTED = "accepted"
STATUS_NEEDS_USER_REVIEW = "needs_user_review"
# ``needs_input`` is the terminal status for a run that could not even resolve a
# seam source — no ``active_user_seam_spec`` AND no usable UV layer to derive one
# from (UV-boundary-fallback revision plan §1 case 3, §4.2). The UI sends the user
# to pick a UV layer / import a UV'd model / open the Seam Editor; nothing ships.
STATUS_NEEDS_INPUT = "needs_input"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUSES = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_ACCEPTED,
    STATUS_NEEDS_USER_REVIEW,
    STATUS_NEEDS_INPUT,
    STATUS_FAILED,
    STATUS_CANCELLED,
)
TERMINAL_STATUSES = (
    STATUS_ACCEPTED,
    STATUS_NEEDS_USER_REVIEW,
    STATUS_NEEDS_INPUT,
    STATUS_FAILED,
    STATUS_CANCELLED,
)

# --- Seam source resolution (UV-boundary-fallback revision plan §2, §4) ------
# A Generate run no longer requires the MVP 2 ``active_user_seam_spec``. The seam
# source is resolved by ``decide_seam_source`` with the precedence:
#   1. an existing ``seam_spec`` file -> ``user_seam_spec`` (source of truth),
#   2. else a selected/active ``uv_layer`` -> ``uv_boundary_derived`` fallback
#      (the existing UV island boundaries become ``user_seam_edges``),
#   3. else -> ``needs_input``.
# The derived path is NOT auto-seam generation: it reads the reference UV the
# user (or an external DCC) already made (revision plan §2.2).
SEAM_SOURCE_USER_SPEC = "user_seam_spec"
SEAM_SOURCE_UV_BOUNDARY = "uv_boundary_derived"
SEAM_SOURCE_TYPES = (SEAM_SOURCE_USER_SPEC, SEAM_SOURCE_UV_BOUNDARY)
DEFAULT_SEAM_SOURCE_POLICY = "prefer_spec_then_uv_boundary"

# The ``needs_input`` error the worker emits when neither a spec nor a usable UV
# layer is available (revision plan §4.2 / §5.3 — exact message is part of the
# contract so the TS mirror + tests can assert it).
MISSING_SEAM_SOURCE_CODE = "missing_seam_source"
MISSING_SEAM_SOURCE_MESSAGE = (
    "No user seam spec or usable UV layer was found. "
    "Select a UV layer or create seams."
)

# Canonical UserSeamSpec fields for a derived spec (revision plan §4.3). Kept
# local so this contract stays free of the ``artist_uv_agent`` import.
SEAM_SPEC_MODE = "user_seams"
DEFAULT_FOLD_ANGLE = 90.0

# Same import surface as the MVP 1/2 workers — the MVP 3 default input is the
# MVP 2 working ``.blend`` (plan §2, §4.1).
SUPPORTED_MODEL_EXTS = (".blend", ".fbx", ".obj", ".glb", ".gltf")

# --- Default Generate options (plan §1 strict user/reference defaults) ------
# These hold the seam set FIXED: no auto refine, no repair, no mandatory enforce
# or gate. Layout optimization is on; the preset + candidate cap match the plan.
DEFAULT_UV_ENGINE = "chart"
DEFAULT_LAYOUT_OPT_PRESET = "user_reference"
DEFAULT_LAYOUT_OPT_MAX_CANDIDATES = 24
STRICT_OPTIONS: dict[str, Any] = {
    "uv_engine": DEFAULT_UV_ENGINE,
    "auto_refine_user_seams": False,
    "repair_user_seams": False,
    "enforce_user_mandatory": False,
    "gate_user_mandatory": False,
    "optimize_layout": True,
    "layout_opt_preset": DEFAULT_LAYOUT_OPT_PRESET,
    "layout_opt_max_candidates": DEFAULT_LAYOUT_OPT_MAX_CANDIDATES,
    "render_previews": True,
    "save_selected_blend": True,
}

# The four strict flags that, if flipped on, would let the seam set change. Seam
# integrity (plan §6) requires every one to stay false.
STRICT_FLAGS = (
    "auto_refine_user_seams",
    "repair_user_seams",
    "enforce_user_mandatory",
    "gate_user_mandatory",
)

# Project-relative handoff paths (plan §2, §9). The accepted summary + selected
# UV blend are copied here so MVP 4/5 read one stable file (plan §9).
SELECTED_UV_BLEND_REL = os.path.join("work", "uv", "selected_uv.blend")
SELECTED_UV_SUMMARY_REL = os.path.join("work", "uv", "selected_uv_summary.json")

# --- Quality thresholds (plan §13 layout quality) --------------------------
# A shipped layout must clear true (raster) overlap and stay inside the [0,1]
# tile. Matches the chart gate / layout-optimization reject band.
RASTER_OVERLAP_MAX = 0.005
OVERLAP_MAX = 0.001

# --- Layout score weights (plan §5) ----------------------------------------
# Mirror of ``chart_uv_agent.layout_optimization.default_score_weights`` so the
# candidate_summary records the exact weighting the worker scored with, without
# importing the (Blender-adjacent) module here.
DEFAULT_SCORE_WEIGHTS: dict[str, float] = {
    "stretch_score": 4.0,
    "worst_island_distortion": 3.0,
    "texel_density_variance": 2.0,
    "raster_overlap_ratio": 2.0,
    "overlap_ratio": 1.0,
    "packing_efficiency": -1.5,
    "small_island_ratio": 0.2,
}

# --- Metric subsets (plan §4.1 summary metrics, §5 candidate metrics) -------
# The full metric dict carries many engine-internal keys; the app surfaces only
# these flattened subsets so the renderer never depends on the raw gate shape.
SUMMARY_METRIC_KEYS = (
    "stretch_score",
    "worst_island_distortion",
    "raster_overlap_ratio",
    "overlap_ratio",
    "texel_density_variance",
    "packing_efficiency",
    "island_count",
    "uv_bounds_ok",
)
CANDIDATE_METRIC_KEYS = (
    "stretch_score",
    "worst_island_distortion",
    "raster_overlap_ratio",
    "overlap_ratio",
    "texel_density_variance",
    "packing_efficiency",
    "uv_bounds_ok",
)

# --- Artifact registry (plan §4.1 artifacts / §2 folder layout) ------------
# key -> (filename, required). Missing required image artifacts become warnings;
# the run still succeeds (plan §13 "image artifact 실패는 warning으로 표시").
SUMMARY_FILE = "uv_generate_summary.json"
P5_GATE_FILE = "p5_gate.json"
SEAM_REPORT_FILE = "seam_report.json"
CANDIDATE_SUMMARY_FILE = "candidate_summary.json"
SELECTED_BLEND_FILE = "selected_uv.blend"
# UV-boundary fallback artifacts (revision plan §3.1). The canonical derived spec
# lives project-relative under ``work/seams/`` (so MVP 3+ can point at it); a copy
# + a resolution report land in the run folder so a run is self-describing.
DERIVED_SEAM_SPEC_FILE = "derived_from_uv_boundary.json"
SEAM_SOURCE_RESOLUTION_FILE = "seam_source_resolution.json"
DERIVED_SEAM_SPEC_REL = os.path.join("work", "seams", "derived_from_uv_boundary.json")
REQUIRED_PREVIEWS = (
    "baseline_uv_layout.png",
    "baseline_checker_front.png",
    "baseline_checker_side.png",
    "selected_uv_layout.png",
    "selected_checker_front.png",
    "selected_checker_side.png",
)
ARTIFACT_FILES: dict[str, tuple[str, bool]] = {
    "p5_gate": (P5_GATE_FILE, False),
    "seam_report": (SEAM_REPORT_FILE, False),
    "candidate_summary": (CANDIDATE_SUMMARY_FILE, False),
    "seam_source_resolution": (SEAM_SOURCE_RESOLUTION_FILE, False),
    "derived_seam_spec": (DERIVED_SEAM_SPEC_FILE, False),
    "baseline_uv_layout": ("baseline_uv_layout.png", True),
    "baseline_checker_front": ("baseline_checker_front.png", True),
    "baseline_checker_side": ("baseline_checker_side.png", True),
    "selected_uv_layout": ("selected_uv_layout.png", True),
    "selected_checker_front": ("selected_checker_front.png", True),
    "selected_checker_side": ("selected_checker_side.png", True),
    "selected_blend": (SELECTED_BLEND_FILE, False),
}


# ---------------------------------------------------------------------------
# Time / IO helpers (kept local so the module loads stand-alone in tests)
# ---------------------------------------------------------------------------
def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with millisecond precision (matches project.json)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_json_optional(path: str) -> Any | None:
    """Read JSON if the file exists and parses; otherwise return ``None``."""
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ---------------------------------------------------------------------------
# Error / status envelopes (mirror the MVP 1/2 contracts)
# ---------------------------------------------------------------------------
def error_envelope(command: str, message: str, *, code: str = "worker_error", **extra: Any) -> dict:
    """A JSON error any worker can emit so the app never has to parse stdout."""
    env = {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_FAILED,
        "command": command,
        "error": {"code": code, "message": message},
    }
    env.update(extra)
    return env


def new_status(
    *,
    run_id: str,
    command: str = CMD_GENERATE_UV_FROM_SEAMS,
    status: str = STATUS_QUEUED,
    input: dict | None = None,
) -> dict:
    """Build an initial ``status.json`` document (plan §9)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "command": command,
        "status": status,
        "started_at": utc_now_iso(),
        "finished_at": None,
        "input": input or {},
        "artifacts": {},
        "error": None,
    }


def finalize_status(
    status_doc: dict,
    *,
    status: str,
    artifacts: dict | None = None,
    error: dict | None = None,
) -> dict:
    """Stamp a terminal status onto an existing status doc."""
    status_doc["status"] = status
    status_doc["finished_at"] = utc_now_iso()
    if artifacts is not None:
        status_doc["artifacts"] = artifacts
    if error is not None:
        status_doc["error"] = error
    return status_doc


# ---------------------------------------------------------------------------
# Options (plan §1 strict defaults, §4.1 options block)
# ---------------------------------------------------------------------------
def default_options() -> dict:
    """A fresh copy of the strict user/reference defaults (plan §1)."""
    return dict(STRICT_OPTIONS)


def merge_options(user_options: dict | None) -> dict:
    """Overlay caller-supplied options on the strict defaults (plan §4.1).

    The strict defaults win for any key the caller omits, so a run started with
    no options is fully strict. A caller MAY override (e.g. to lower
    ``layout_opt_max_candidates`` for a fast smoke), but flipping a strict flag on
    is recorded faithfully and then fails seam integrity (plan §6, §14).
    """
    opts = default_options()
    for k, v in (user_options or {}).items():
        opts[k] = v
    return opts


# ---------------------------------------------------------------------------
# Seam source resolution (UV-boundary-fallback revision plan §1, §4)
# ---------------------------------------------------------------------------
def decide_seam_source(
    *,
    seam_spec_path: str | None,
    seam_spec_exists: bool,
    uv_layer: str | None,
    policy: str = DEFAULT_SEAM_SOURCE_POLICY,
) -> dict:
    """Decide which seam source a Generate run should use (revision plan §1, §4).

    Pure — no Blender, no IO — so the three branches are unit-tested without a
    worker run (revision plan §6.1). Precedence for the default
    ``prefer_spec_then_uv_boundary`` policy:

    1. an existing ``seam_spec`` file wins -> ``user_seam_spec`` (explicit, the
       MVP 2 editor output is still the source of truth, revision plan §7),
    2. else a selected/active ``uv_layer`` -> ``uv_boundary_derived`` fallback,
    3. else -> ``needs_input`` (nothing to unwrap from).

    Returns ``{"kind": SEAM_SOURCE_USER_SPEC | SEAM_SOURCE_UV_BOUNDARY |
    STATUS_NEEDS_INPUT, "uv_layer": <str|None>, "error": <dict|None>}``.
    """
    if seam_spec_path and seam_spec_exists:
        return {"kind": SEAM_SOURCE_USER_SPEC, "uv_layer": None, "error": None}
    if uv_layer:
        return {"kind": SEAM_SOURCE_UV_BOUNDARY, "uv_layer": uv_layer, "error": None}
    return {
        "kind": STATUS_NEEDS_INPUT,
        "uv_layer": None,
        "error": {"code": MISSING_SEAM_SOURCE_CODE, "message": MISSING_SEAM_SOURCE_MESSAGE},
    }


def build_seam_source(
    *,
    source_type: str,
    path: str | None = None,
    uv_layer: str | None = None,
    user_confirmed: bool,
    derived: bool,
) -> dict:
    """The summary's ``seam_source`` block (revision plan §2.3, §4 contract).

    ``user_confirmed=False`` for a derived spec means "not saved in the MVP 2
    editor, but parsed from an existing UV" — it never blocks a run (revision plan
    §2.3). ``derived`` mirrors the type for a quick boolean check in the UI/tests.
    """
    return {
        "type": source_type,
        "path": path,
        "uv_layer": uv_layer,
        "user_confirmed": bool(user_confirmed),
        "derived": bool(derived),
    }


def make_derived_seam_spec(
    *,
    object_name: str,
    user_seam_edges: Iterable[int],
    uv_layer: str,
    mandatory_fold_angle: float = DEFAULT_FOLD_ANGLE,
) -> dict:
    """A canonical ``UserSeamSpec`` dict derived from a UV island boundary
    (revision plan §4.3).

    The field set is exactly :class:`artist_uv_agent.user_seams.UserSeamSpec`'s so
    the result round-trips through ``UserSeamSpec.from_dict()``.
    ``user_protected_edges`` is ALWAYS empty — a UV boundary only carries cuts, not
    protect intent (revision plan §6.1 "derived spec has user_protected_edges=[]").
    This builds a spec; it never adds a seam beyond the UV island boundaries
    (revision plan §2.2, §4.1 "Do not ... UV layer boundary 외의 새 seam을 추가").
    """
    edges = sorted({int(e) for e in (user_seam_edges or [])})
    return {
        "version": SCHEMA_VERSION,
        "object": object_name,
        "mode": SEAM_SPEC_MODE,
        "mandatory_fold_angle": float(mandatory_fold_angle),
        "user_seam_edges": edges,
        "user_protected_edges": [],
        "chapters": [],
        "notes": f"Derived from UV island boundaries: {uv_layer}",
    }


# ---------------------------------------------------------------------------
# Metric flattening (plan §4.1 / §5)
# ---------------------------------------------------------------------------
def _flatten(metrics: dict | None, keys: Iterable[str]) -> dict:
    """Pick ``keys`` from ``metrics``, rounding floats; ``uv_bounds_ok`` and
    ``island_count`` pass through as bool/int. Missing keys are omitted."""
    out: dict[str, Any] = {}
    for key in keys:
        if not metrics or key not in metrics:
            continue
        val = metrics[key]
        if key == "uv_bounds_ok":
            out[key] = bool(val)
        elif key == "island_count":
            out[key] = int(val) if isinstance(val, (int, float)) else val
        elif isinstance(val, (int, float)):
            out[key] = round(float(val), 6)
        else:
            out[key] = val
    return out


def flatten_summary_metrics(metrics: dict | None) -> dict:
    """The eight summary metrics (plan §4.1 ``metrics`` block)."""
    return _flatten(metrics, SUMMARY_METRIC_KEYS)


def flatten_candidate_metrics(metrics: dict | None) -> dict:
    """The per-candidate metric subset (plan §5 candidate ``metrics``)."""
    return _flatten(metrics, CANDIDATE_METRIC_KEYS)


# ---------------------------------------------------------------------------
# Seam integrity (plan §6 — the MVP 3 hard acceptance)
# ---------------------------------------------------------------------------
def evaluate_seam_integrity(
    user_seams: dict | None,
    options: dict,
    *,
    final_seams: Iterable[int] | None = None,
    invalid_edges: Iterable[int] | None = None,
    object_mismatch: bool = False,
) -> dict:
    """Decide whether the run preserved the user's seam set (plan §6).

    ``user_seams`` is the ``user_seams`` block the chart engine emits
    (``UserSeamResult.report`` augmented with the mandatory flags). Returns::

        {"block": <seam_integrity for the summary>, "violations": [...], "valid": bool}

    ``valid`` is true only when EVERY plan §6 check holds:

    - no invalid edge ids, no object mismatch,
    - all four strict flags are false,
    - ``auto_added_seams == 0``,
    - ``final_seam_count == user_seam_count``,
    - no protected (non-seam) edge leaked into the final seam set.

    Pure — no Blender. ``final_seams`` (the actually-shipped seam ids) is only
    needed for the protected-leak check; when omitted that check is skipped.
    """
    us = user_seams or {}
    user_seam_count = int(us.get("user_seam_count", 0))
    user_protected_count = int(us.get("user_protected_count", 0))
    final_seam_count = int(us.get("final_seam_count", 0))
    auto_added = int(us.get("auto_added_seams", 0))
    spec_invalid = list(us.get("invalid_edges", []) or [])
    seam_edges = {int(e) for e in us.get("user_seam_edges", []) or []}
    protected_edges = {int(e) for e in us.get("user_protected_edges", []) or []}

    invalid = sorted({int(e) for e in (invalid_edges or [])} | {int(e) for e in spec_invalid})

    violations: list[dict] = []
    if invalid:
        violations.append({"code": "invalid_edges", "edges": invalid})
    if object_mismatch:
        violations.append({"code": "object_mismatch"})
    for flag in STRICT_FLAGS:
        if bool(options.get(flag)):
            violations.append({"code": "strict_flag_enabled", "flag": flag})
    if auto_added != 0:
        violations.append({"code": "auto_added_seams", "count": auto_added})
    if final_seam_count != user_seam_count:
        violations.append({
            "code": "seam_count_changed",
            "user_seam_count": user_seam_count,
            "final_seam_count": final_seam_count,
        })
    # Protected leak: a protected edge that is NOT also an explicit user seam must
    # never ship (plan §6). Only checkable with the actual final seam set.
    if final_seams is not None:
        final = {int(e) for e in final_seams}
        leaked = sorted((protected_edges - seam_edges) & final)
        if leaked:
            violations.append({"code": "protected_edge_shipped", "edges": leaked})

    block = {
        "user_seam_count": user_seam_count,
        "user_protected_count": user_protected_count,
        "final_seam_count": final_seam_count,
        "auto_added_seams": auto_added,
        "mandatory_rule_enabled": bool(options.get("enforce_user_mandatory")),
        "mandatory_gate_enabled": bool(options.get("gate_user_mandatory")),
        "valid": not violations,
    }
    return {"block": block, "violations": violations, "valid": not violations}


# ---------------------------------------------------------------------------
# Layout quality (plan §13 — selected candidate must clear overlap/bounds)
# ---------------------------------------------------------------------------
def evaluate_layout_quality(
    metrics: dict | None,
    *,
    raster_overlap_max: float = RASTER_OVERLAP_MAX,
    overlap_max: float = OVERLAP_MAX,
) -> dict:
    """Block-ship checks for the SELECTED layout (plan §1, §13).

    Returns ``{"issues": [...], "ok": bool}``. ``ok`` is false when the shipped
    layout has true (raster) overlap, flipped-area overlap, or UVs outside the
    [0,1] tile — none of which may ship (plan §1 "overlap이 있으면 ship 안 함").
    """
    m = metrics or {}
    issues: list[dict] = []

    def _num(key: str, default: float = 0.0) -> float:
        v = m.get(key, default)
        return float(v) if isinstance(v, (int, float)) else default

    raster = _num("raster_overlap_ratio")
    if raster > raster_overlap_max:
        issues.append({"code": "raster_overlap", "value": round(raster, 6),
                       "threshold": raster_overlap_max})
    signed = _num("overlap_ratio")
    if signed > overlap_max:
        issues.append({"code": "overlap", "value": round(signed, 6),
                       "threshold": overlap_max})
    if "uv_bounds_ok" in m and not bool(m.get("uv_bounds_ok")):
        issues.append({"code": "uv_out_of_bounds"})

    return {"issues": issues, "ok": not issues}


def classify_generate_status(integrity: dict, quality: dict) -> str:
    """The terminal status for a completed run (plan §6, §13).

    ``accepted`` only when seam integrity AND layout quality both hold; otherwise
    ``needs_user_review`` (the run produced artifacts for inspection but must not
    ship / replace ``work/uv/selected_uv.blend``).
    """
    if integrity.get("valid") and quality.get("ok"):
        return STATUS_ACCEPTED
    return STATUS_NEEDS_USER_REVIEW


# ---------------------------------------------------------------------------
# Candidate summary normalization (plan §5)
# ---------------------------------------------------------------------------
def _normalize_candidate(cand: dict, *, average_scale: bool) -> dict:
    """One UI-friendly candidate row (plan §5 candidate shape)."""
    return {
        "id": cand.get("id"),
        "unwrap_method": cand.get("unwrap_method"),
        "minimize_iters": int(cand.get("minimize_iters", 0) or 0),
        "margin": cand.get("margin"),
        "pack_shape": cand.get("pack_shape"),
        "rotate": bool(cand.get("rotate", True)),
        "average_scale": bool(cand.get("average_scale", average_scale)),
        "accepted": bool(cand.get("accepted", False)),
        "reason": cand.get("reason", "") or "",
        "score": round(float(cand["score"]), 6) if isinstance(cand.get("score"), (int, float)) else cand.get("score"),
        "metrics": flatten_candidate_metrics(cand.get("metrics")),
    }


def normalize_candidate_summary(
    layout_report: dict | None,
    *,
    baseline_candidate_id: str | None = None,
    score_weights: dict | None = None,
    max_candidates: int | None = None,
    average_scale: bool = True,
) -> dict:
    """Build ``candidate_summary.json`` from a layout-optimization report (plan §5).

    ``layout_report`` is ``LayoutOptimizationResult.report()`` (the
    ``layout_optimization`` block of the chart result). Pure — the report is a
    plain dict, so this is unit-tested without Blender. When no layout
    optimization ran (``layout_report`` is ``None``/empty), an empty-but-valid
    summary is returned so the file always exists (plan §13 acceptance).
    """
    report = layout_report or {}
    candidates = list(report.get("candidates", []) or [])
    if max_candidates is not None and max_candidates >= 0:
        candidates = candidates[:max_candidates]
    rejected = [
        {"id": c.get("id"), "reason": c.get("reason") or "rejected"}
        for c in candidates
        if not bool(c.get("accepted", False))
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_candidate_id": baseline_candidate_id,
        "selected_candidate_id": report.get("selected_candidate_id"),
        "kept_baseline": bool(report.get("kept_baseline", False)),
        "score_weights": dict(score_weights or DEFAULT_SCORE_WEIGHTS),
        "candidates": [_normalize_candidate(c, average_scale=average_scale) for c in candidates],
        "rejected": rejected,
    }


def build_layout_optimization_block(layout_report: dict | None) -> dict:
    """The summary's ``layout_optimization`` block (plan §4.1).

    Flattens the report's before/after metrics into the headline numbers the UI
    shows. ``enabled=False`` when no optimization ran.
    """
    report = layout_report or {}
    if not report:
        return {"enabled": False}
    before = report.get("before_metrics") or {}
    after = report.get("after_metrics") or {}

    def _g(m: dict, key: str):
        v = m.get(key)
        return round(float(v), 6) if isinstance(v, (int, float)) else v

    return {
        "enabled": True,
        "selected_candidate_id": report.get("selected_candidate_id"),
        "kept_baseline": bool(report.get("kept_baseline", False)),
        "candidate_count": len(report.get("candidates", []) or []),
        "score_before": _g(report, "score_before"),
        "score_after": _g(report, "score_after"),
        "packing_efficiency_before": _g(before, "packing_efficiency"),
        "packing_efficiency_after": _g(after, "packing_efficiency"),
        "stretch_before": _g(before, "stretch_score"),
        "stretch_after": _g(after, "stretch_score"),
    }


# ---------------------------------------------------------------------------
# Summary builder (plan §4.1 — the renderer's primary input, §3 "primary input")
# ---------------------------------------------------------------------------
def build_generate_summary(
    *,
    run_id: str,
    status: str,
    model: str | None,
    object_name: str | None,
    seam_spec: str | None,
    metrics: dict | None,
    seam_integrity: dict,
    layout_optimization: dict,
    artifacts: dict,
    seam_source: dict | None = None,
    selected_candidate_id: str | None = None,
    selected_uv_model: str | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """Assemble ``uv_generate_summary.json`` (plan §4.1).

    ``selected_uv_model`` is the project-relative ``work/uv/selected_uv.blend``
    on an accepted run, or ``None`` when nothing shipped (plan §6).
    ``seam_source`` records whether the seam set came from the explicit MVP 2 spec
    or was derived from a UV island boundary (revision plan §2.3, §4).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "command": CMD_GENERATE_UV_FROM_SEAMS,
        "status": status,
        "model": model,
        "object_name": object_name,
        "seam_spec": seam_spec,
        "seam_source": seam_source,
        "selected_candidate_id": selected_candidate_id,
        "selected_uv_model": selected_uv_model,
        "metrics": flatten_summary_metrics(metrics),
        "seam_integrity": seam_integrity,
        "layout_optimization": layout_optimization,
        "artifacts": artifacts,
        "warnings": list(warnings or []),
    }


def collect_generate_artifacts(out_dir: str) -> tuple[dict, list[str]]:
    """Return ``(artifacts, warnings)`` for the run artifacts present in ``out_dir``.

    Maps a stable key -> run-relative filename for each file that exists; the
    ``summary`` key is always included (the summary doc names itself, plan §4.1).
    Missing *required* preview artifacts become warnings so the app surfaces a
    partial result instead of a hard failure (plan §13).
    """
    artifacts: dict[str, str] = {"summary": SUMMARY_FILE}
    warnings: list[str] = []
    for key, (filename, required) in ARTIFACT_FILES.items():
        if os.path.exists(os.path.join(out_dir, filename)):
            artifacts[key] = filename
        elif required:
            warnings.append(f"missing artifact: {filename}")
    return artifacts, warnings
