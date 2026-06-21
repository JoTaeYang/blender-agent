"""Shared worker <-> Electron JSON contract for the MVP 2 Seam Spec Editor.

Single source of truth for the JSON shapes the Electron app and the Python/Blender
seam-editor worker exchange (``docs/ELECTRON_UV_REVIEW_APP_MVP2_PRODUCTION_PLAN.ko.md``
§4, §5, §6, §9). Like ``worker/app_uv_review_contract.py`` it is intentionally
**pure Python** (no ``bpy``, no NumPy, and — deliberately — no
``artist_uv_agent`` import) so it loads stand-alone in unit tests and stays a tiny,
self-contained dependency. ``app/shared/contracts/seamEditor.ts`` is its
TypeScript mirror, and the Node side re-implements :func:`normalize_and_validate_spec`
so the same rules apply whether a spec is normalized in Blender or in the app.

MVP 2 product rules encoded here (plan §1, §4, §13):

- The worker NEVER generates or repairs UVs and NEVER auto-adds the mandatory-90
  fold as a seam — the user's seam/protect choices are the source of truth.
- The canonical ``user_seam_spec.json`` carries ONLY the
  :class:`artist_uv_agent.user_seams.UserSeamSpec` schema; UI-only state lives in a
  separate ``seam_editor_state.json``.
- ``user_seam_edges`` win over ``user_protected_edges`` for the same edge (plan §4
  "저장 시 기본 정책은 seam wins"); out-of-range edge ids are dropped and reported
  (plan §4 "Headless save command는 invalid를 제거하고 report에 남긴다").
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA_VERSION = 1
DEFAULT_FOLD_ANGLE = 90.0
SPEC_MODE = "user_seams"

# --- App-facing command names (stable contract, plan §5/§6) ----------------
CMD_EXPORT_EDGE_GEOMETRY = "export_edge_geometry"
CMD_LOAD_USER_SEAM_SPEC = "load_user_seam_spec"
CMD_SAVE_USER_SEAM_SPEC = "save_user_seam_spec"
CMD_VALIDATE_USER_SEAM_SPEC = "validate_user_seam_spec"
CMD_EXTRACT_UV_BOUNDARY = "extract_uv_boundary_as_seams"
COMMANDS = (
    CMD_EXPORT_EDGE_GEOMETRY,
    CMD_LOAD_USER_SEAM_SPEC,
    CMD_SAVE_USER_SEAM_SPEC,
    CMD_VALIDATE_USER_SEAM_SPEC,
    CMD_EXTRACT_UV_BOUNDARY,
)

# --- Run status lifecycle (plan §9 status.json) ----------------------------
# ``no_uv`` is a first-class terminal status for extract_uv_boundary on a model
# with no UV layer (plan §6.4), exactly like the MVP 1 review worker.
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

# Same import surface as the MVP 1 review worker — the MVP 2 default input is the
# MVP 0/1 working ``.blend`` (plan §5).
SUPPORTED_MODEL_EXTS = (".blend", ".fbx", ".obj", ".glb", ".gltf")

# Conflict type emitted when one edge is marked both seam and protected (plan §6.3).
CONFLICT_SEAM_AND_PROTECTED = "seam_and_protected"
RESOLUTION_SEAM_WINS = "seam_wins"


# ---------------------------------------------------------------------------
# Time / IO helpers (kept local so the module loads stand-alone in tests)
# ---------------------------------------------------------------------------
def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with millisecond precision (matches project.json)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ---------------------------------------------------------------------------
# Error / status envelopes (mirror app_uv_review_contract so the app never has
# to parse stdout, plan §13 "worker failure가 앱 crash로 이어지지 않는다")
# ---------------------------------------------------------------------------
def error_envelope(command: str, message: str, *, code: str = "worker_error", **extra: Any) -> dict:
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
    command: str,
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
    status_doc["status"] = status
    status_doc["finished_at"] = utc_now_iso()
    if artifacts is not None:
        status_doc["artifacts"] = artifacts
    if error is not None:
        status_doc["error"] = error
    return status_doc


# ---------------------------------------------------------------------------
# Canonical spec assembly + validation/normalization (plan §4, §6.2, §6.3)
# ---------------------------------------------------------------------------
def _int_set(values: Iterable[Any]) -> set[int]:
    out: set[int] = set()
    for v in values or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


def make_seam_spec(
    *,
    object_name: str,
    user_seam_edges: Iterable[int] = (),
    user_protected_edges: Iterable[int] = (),
    mandatory_fold_angle: float = DEFAULT_FOLD_ANGLE,
    chapters: list | None = None,
    notes: str = "",
) -> dict:
    """A canonical ``user_seam_spec.json`` dict (plan §4 contract).

    The field set is exactly :class:`artist_uv_agent.user_seams.UserSeamSpec`'s, so
    the result round-trips through ``UserSeamSpec.from_dict()`` (plan §13/§16).
    """
    return {
        "version": SCHEMA_VERSION,
        "object": object_name,
        "mode": SPEC_MODE,
        "mandatory_fold_angle": float(mandatory_fold_angle),
        "user_seam_edges": sorted(_int_set(user_seam_edges)),
        "user_protected_edges": sorted(_int_set(user_protected_edges)),
        "chapters": list(chapters or []),
        "notes": notes,
    }


def normalize_and_validate_spec(
    spec: dict,
    *,
    edge_count: int | None = None,
    object_name: str | None = None,
) -> dict:
    """Validate ``spec`` against a mesh and return the normalization report (plan §6.3).

    The returned dict is a superset of every command's ``validation`` block:

    - ``valid``           — clean: no invalid edges, no conflicts, no object mismatch.
    - ``object_mismatch`` — spec's ``object`` differs from the selected object
      (plan §4 "object name이 다르면 load는 가능하되 apply는 막는다").
    - ``invalid_edges``   — edge ids outside ``[0, edge_count)`` (only when
      ``edge_count`` is known); dropped from the normalized spec (plan §4).
    - ``conflicts``       — edges marked both seam and protected; resolved seam-wins
      by removing them from ``user_protected_edges`` (plan §4).
    - ``normalized_spec`` — the cleaned canonical spec, ready to save / hand to MVP 3.
    - ``user_seam_count`` / ``user_protected_count`` — counts after normalization.

    ``chapters`` pass through untouched (the MVP 2 editor never authors them, plan §4);
    only the top-level ``user_seam_edges`` / ``user_protected_edges`` are normalized.
    """
    spec_object = str(spec.get("object", ""))
    raw_seams = _int_set(spec.get("user_seam_edges", []))
    raw_protected = _int_set(spec.get("user_protected_edges", []))

    def in_range(eid: int) -> bool:
        return edge_count is None or (0 <= eid < edge_count)

    invalid_edges = sorted({e for e in (raw_seams | raw_protected) if not in_range(e)})
    seams = {e for e in raw_seams if in_range(e)}
    protected = {e for e in raw_protected if in_range(e)}

    # seam wins: an edge marked both seam and protected ships as a seam (plan §4).
    conflict_ids = sorted(seams & protected)
    conflicts = [
        {"edge_id": e, "type": CONFLICT_SEAM_AND_PROTECTED, "resolution": RESOLUTION_SEAM_WINS}
        for e in conflict_ids
    ]
    protected -= seams

    object_mismatch = bool(object_name and spec_object and spec_object != object_name)

    normalized_spec = make_seam_spec(
        object_name=spec_object,
        user_seam_edges=seams,
        user_protected_edges=protected,
        mandatory_fold_angle=float(spec.get("mandatory_fold_angle", DEFAULT_FOLD_ANGLE)),
        chapters=spec.get("chapters", []),
        notes=str(spec.get("notes", "")),
    )

    return {
        "valid": not invalid_edges and not conflicts and not object_mismatch,
        "object_mismatch": object_mismatch,
        "invalid_edges": invalid_edges,
        "conflicts": conflicts,
        "normalized_spec": normalized_spec,
        "user_seam_count": len(seams),
        "user_protected_count": len(protected),
    }
