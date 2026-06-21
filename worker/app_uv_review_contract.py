"""Shared worker <-> Electron JSON contract for the MVP 1 UV Review app.

This module is the single source of truth for the JSON shapes exchanged between
the Electron app and the Python/Blender UV-review workers
(``docs/ELECTRON_UV_REVIEW_APP_MVP1_PRODUCTION_PLAN.ko.md`` §5, §6, §9).

Like ``worker/app_job_contract.py`` (the MVP 0 contract) it is intentionally
**pure Python** (no ``bpy``, no NumPy) so it can be imported and unit-tested
outside Blender, and reused by ``worker/review_existing_uv.py`` and the metric
layer. ``app/shared/contracts/uvReview.ts`` is its TypeScript mirror.

Design rules (plan §14 "shared contract 변경은 먼저 문서를 갱신"):

- ``SCHEMA_VERSION`` is pinned; new fields are introduced as optional only.
- The app-facing command names are stable.
- MVP 1 is **read-only review**: nothing here modifies UVs and the mandatory-90
  rule is deliberately absent (plan §6 "Mandatory 90 rule: 계산/gate 안 함").
- Failures are always representable as JSON (never only stdout/stderr).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

# --- App-facing command names (stable contract, plan §5) -------------------
CMD_INSPECT_UV_LAYERS = "inspect_uv_layers"
CMD_REVIEW_EXISTING_UV = "review_existing_uv"
CMD_SET_ACTIVE_UV_LAYER = "set_active_uv_layer"
COMMANDS = (CMD_INSPECT_UV_LAYERS, CMD_REVIEW_EXISTING_UV, CMD_SET_ACTIVE_UV_LAYER)

# --- Run status lifecycle (plan §9 status.json) ----------------------------
# Note the MVP-1-specific ``no_uv`` terminal status: an object with no UV layer
# is NOT a failure, it is a first-class outcome the UI renders as an empty state
# (plan §4 "UV layer가 없으면 실패가 아니라 status: no_uv로 반환한다").
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_ACCEPTED = "accepted"
STATUS_NO_UV = "no_uv"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUSES = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_ACCEPTED,
    STATUS_NO_UV,
    STATUS_FAILED,
    STATUS_CANCELLED,
)

# --- Supported model inputs (plan §5.2 import branching) -------------------
# Superset of the MVP 0 import set because the MVP 1 default input is the MVP 0
# working model, which is a ``.blend``.
SUPPORTED_REVIEW_EXTS = (".blend", ".fbx", ".obj", ".glb", ".gltf")

# --- inspect recommended next steps (plan §5.1 output) ---------------------
NEXT_REVIEW_EXISTING_UV = "review_existing_uv"
NEXT_OPEN_SEAM_OR_GENERATE = "open_seam_editor_or_generate_uv"

# --- Review status (plan §6 "review_status를 표시한다") ---------------------
# MVP 1 shows a review_status, NOT a pass/fail gate. Several problems can be true
# at once: ``issues`` carries them all; ``review_status`` is the highest-priority
# one for a one-glance summary.
REVIEW_CLEAN = "clean"
REVIEW_HAS_OVERLAP = "has_overlap"
REVIEW_HIGH_STRETCH = "high_stretch"
REVIEW_DENSITY_VARIANCE = "density_variance"
REVIEW_OUT_OF_BOUNDS = "out_of_bounds"
REVIEW_NO_UV = "no_uv"
REVIEW_UNKNOWN = "unknown"
REVIEW_STATUSES = (
    REVIEW_CLEAN,
    REVIEW_HAS_OVERLAP,
    REVIEW_HIGH_STRETCH,
    REVIEW_DENSITY_VARIANCE,
    REVIEW_OUT_OF_BOUNDS,
    REVIEW_NO_UV,
    REVIEW_UNKNOWN,
)

# Priority order high -> low: the worst issue wins the headline ``review_status``.
_REVIEW_PRIORITY = (
    REVIEW_HAS_OVERLAP,
    REVIEW_OUT_OF_BOUNDS,
    REVIEW_HIGH_STRETCH,
    REVIEW_DENSITY_VARIANCE,
)

# Issue severities.
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

# --- Required metrics (plan §6) --------------------------------------------
REQUIRED_METRICS = (
    "stretch_score",
    "worst_island_distortion",
    "overlap_ratio",
    "raster_overlap_ratio",
    "self_overlap_ratio",
    "cross_overlap_ratio",
    "texel_density_variance",
    "packing_efficiency",
)

# --- Review thresholds (plan §6 "pass/fail보다 review_status") --------------
# These are *advisory* (reviewer aid, plan §13 "Metrics를 production gate로
# 오해할 위험"): they color the review_status / issues only, they never block.
DEFAULT_THRESHOLDS = {
    # Any genuine interior raster overlap is worth flagging (texture will break).
    "raster_overlap_ratio": 1e-4,
    # Signed/flipped-area overlap (folds).
    "overlap_ratio": 1e-4,
    # Area-distortion: same band the engine uses for "needs_repair".
    "stretch_score": 0.25,
    # Worst single island distortion before we call it high stretch.
    "worst_island_distortion": 0.5,
    # Island-to-island density coefficient-of-variation.
    "texel_density_variance": 0.5,
    # UV out-of-[0,1] tolerance.
    "uv_bounds_tol": 1e-4,
}

# --- Artifact registry (plan §7 / §2 folder layout) ------------------------
# key -> (filename, required). Missing required artifacts become warnings; the
# review still succeeds (plan §7 "image artifact 생성 실패는 warnings로 표시").
# The summary file is not listed here: it is the document being assembled, so it
# does not appear in its own artifacts map (it is read directly via getReviewRun).
ARTIFACT_FILES = {
    "metrics": ("uv_metrics.json", True),
    "uv_layers": ("uv_layers.json", False),
    "uv_bounds": ("uv_bounds.json", False),
    "uv_layout": ("uv_layout.png", True),
    "uv_layout_svg": ("uv_layout.svg", False),
    "checker_front": ("checker_front.png", True),
    "checker_side": ("checker_side.png", True),
    "checker_3q": ("checker_3q.png", False),
    "overlap_mask": ("overlap_mask.png", False),
    "stretch_heatmap": ("stretch_heatmap.png", False),
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
# Error / status envelopes
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
    command: str = CMD_REVIEW_EXISTING_UV,
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
# Review-status classification (plan §6)
# ---------------------------------------------------------------------------
def classify_review(
    metrics: dict | None,
    uv: dict | None,
    *,
    thresholds: dict | None = None,
) -> tuple[str, list[dict]]:
    """Map the computed metrics to a ``(review_status, issues)`` pair (plan §6).

    This is **advisory only** — it never returns a pass/fail and never blocks the
    flow (plan §13). ``issues`` lists every problem found; ``review_status`` is the
    single highest-priority one for the headline. With no metrics (no UV layer) it
    returns ``(no_uv, [])``.
    """
    if metrics is None:
        return REVIEW_NO_UV, []
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    uv = uv or {}
    issues: list[dict] = []

    def _val(name: str) -> float:
        v = metrics.get(name)
        return float(v) if isinstance(v, (int, float)) else 0.0

    # Overlap — the raster check is the trustworthy "texture will break" signal.
    raster = _val("raster_overlap_ratio")
    if raster > thr["raster_overlap_ratio"]:
        issues.append(_issue(
            "raster_overlap", SEVERITY_ERROR,
            "UV islands overlap in the raster check.",
            "raster_overlap_ratio", raster))
    signed = _val("overlap_ratio")
    if signed > thr["overlap_ratio"]:
        issues.append(_issue(
            "overlap", SEVERITY_ERROR,
            "UV contains flipped/folded faces (signed-area overlap).",
            "overlap_ratio", signed))

    # Out of bounds — negative or beyond the [0,1] tile.
    bounds = uv.get("uv_bounds") or {}
    if uv.get("has_negative_uv") or uv.get("has_out_of_tile_uv") or bounds.get("in_0_1") is False:
        issues.append(_issue(
            "out_of_bounds", SEVERITY_WARNING,
            "UV coordinates fall outside the [0,1] tile.",
            "uv_bounds", None))

    # Stretch / distortion.
    stretch = _val("stretch_score")
    worst = _val("worst_island_distortion")
    if stretch > thr["stretch_score"] or worst > thr["worst_island_distortion"]:
        issues.append(_issue(
            "high_stretch", SEVERITY_WARNING,
            "UV has high area/angle stretch.",
            "stretch_score", stretch))

    # Texel-density imbalance across islands.
    density = _val("texel_density_variance")
    if density > thr["texel_density_variance"]:
        issues.append(_issue(
            "density_variance", SEVERITY_WARNING,
            "Texel density is uneven across UV islands.",
            "texel_density_variance", density))

    review_status = _headline_status(issues)
    return review_status, issues


def _issue(code: str, severity: str, message: str, metric: str | None, value) -> dict:
    issue = {"code": code, "severity": severity, "message": message, "metric": metric}
    if isinstance(value, (int, float)):
        issue["value"] = round(float(value), 6)
    return issue


# Issue-code -> review_status it implies.
_CODE_TO_STATUS = {
    "raster_overlap": REVIEW_HAS_OVERLAP,
    "overlap": REVIEW_HAS_OVERLAP,
    "out_of_bounds": REVIEW_OUT_OF_BOUNDS,
    "high_stretch": REVIEW_HIGH_STRETCH,
    "density_variance": REVIEW_DENSITY_VARIANCE,
}


def _headline_status(issues: list[dict]) -> str:
    statuses = {_CODE_TO_STATUS.get(i["code"], REVIEW_UNKNOWN) for i in issues}
    for s in _REVIEW_PRIORITY:
        if s in statuses:
            return s
    return REVIEW_CLEAN


# ---------------------------------------------------------------------------
# Summary builders (plan §5.2 output, §9 primary UI input)
# ---------------------------------------------------------------------------
def build_review_summary(
    *,
    run_id: str,
    model: str,
    object_name: str,
    uv_layer: str,
    mesh: dict,
    uv: dict,
    metrics: dict,
    artifacts: dict,
    review_status: str,
    issues: list[dict],
    warnings: list[str] | None = None,
) -> dict:
    """Assemble ``uv_review_summary.json`` — the renderer's primary input (plan §9)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "command": CMD_REVIEW_EXISTING_UV,
        "status": STATUS_ACCEPTED,
        "model": model,
        "object_name": object_name,
        "uv_layer": uv_layer,
        "mesh": mesh,
        "uv": uv,
        "metrics": metrics,
        "review_status": review_status,
        "issues": issues,
        "artifacts": artifacts,
        "warnings": list(warnings or []),
    }


def no_uv_summary(
    *,
    run_id: str,
    model: str | None,
    object_name: str | None,
    warnings: list[str] | None = None,
) -> dict:
    """The ``status: no_uv`` summary for an object with no UV layer (plan §5.2)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "command": CMD_REVIEW_EXISTING_UV,
        "status": STATUS_NO_UV,
        "model": model,
        "object_name": object_name,
        "uv_layer": None,
        "mesh": None,
        "uv": None,
        "metrics": None,
        "review_status": REVIEW_NO_UV,
        "issues": [],
        "artifacts": {},
        "warnings": list(warnings or ["Object has no UV layer to review."]),
    }


def collect_review_artifacts(out_dir: str) -> tuple[dict, list[str]]:
    """Return ``(artifacts, warnings)`` for the review artifacts present in ``out_dir``.

    ``artifacts`` maps a stable key -> filename (relative to the run dir) for each
    file that exists. Missing *required* image artifacts become warnings so the app
    can surface partial results instead of treating them as a hard failure
    (plan §7 "image artifact 생성 실패는 warnings로 표시").
    """
    artifacts: dict[str, str] = {}
    warnings: list[str] = []
    for key, (filename, required) in ARTIFACT_FILES.items():
        if os.path.exists(os.path.join(out_dir, filename)):
            artifacts[key] = filename
        elif required:
            warnings.append(f"missing artifact: {filename}")
    return artifacts, warnings
