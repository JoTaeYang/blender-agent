"""User-Guided Seam UV Pipeline (USER_GUIDED_SEAM_UV_PIPELINE_PLAN).

The product decision (plan §2): automatic seam/chapter generation is demoted to
experimental / suggestion / draft, and the USER's seam plan becomes the authoritative
source of truth (plan §7). This module loads a *user seam spec*, validates the edge ids
against a :class:`~uv_agent.geometry.mesh_graph.MeshGraph`, resolves the precedence

    mandatory 90° fold  >  user_seam_edges  >  user_protected_edges  >  auto suggested

(mandatory ALWAYS wins — a ≥``mandatory_fold_angle`` fold that the user marked *protected*
still ships as a seam and the clash is recorded as a conflict, plan §7), and assembles the
initial seam set + the *forbidden* (protected) set that
``chart_uv_agent.pipeline.run_chart_uv`` consumes.

Pure: no Blender, no mesh mutation. It reads the mesh's edge table only to validate ids and
to find the mandatory folds, so it is unit-testable without ``bpy``. ``chapters`` are loaded
and surfaced for report/UI grouping (plan §6) but do not drive unwrap — the unwrap is seam-set
driven (plan §6: "unwrap은 edge set 기준으로 수행하고, chapter는 UI/report grouping에 쓴다").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from chart_uv_agent.segmentation import mandatory_seam_edges
from uv_agent.geometry.mesh_graph import MeshGraph

SPEC_VERSION = 1
DEFAULT_FOLD_ANGLE = 90.0


@dataclass
class UserChapter:
    """A user-authored chapter (face/clothing/arm/hand …). ``face_ids`` group the chapter for
    report/UI; ``seam_edges``/``protected_edges`` are folded into the spec-level sets so a
    chapter can carry its own seam intent (plan §6)."""

    name: str
    face_ids: list[int] = field(default_factory=list)
    seam_edges: list[int] = field(default_factory=list)
    protected_edges: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "face_ids": sorted(self.face_ids),
                "seam_edges": sorted(self.seam_edges),
                "protected_edges": sorted(self.protected_edges)}

    @classmethod
    def from_dict(cls, d: dict) -> "UserChapter":
        return cls(name=str(d.get("name", "")),
                   face_ids=[int(x) for x in d.get("face_ids", [])],
                   seam_edges=[int(x) for x in d.get("seam_edges", [])],
                   protected_edges=[int(x) for x in d.get("protected_edges", [])])


@dataclass
class UserSeamSpec:
    """A user-authored seam plan (plan §6). The minimum required content is
    ``user_seam_edges`` + ``user_protected_edges`` + ``mandatory_fold_angle``; ``chapters`` is
    optional grouping. The chapters' own ``seam_edges`` / ``protected_edges`` are merged into
    the effective edge sets by :func:`build_user_seam_set`."""

    object: str = ""
    version: int = SPEC_VERSION
    mode: str = "user_seams"
    mandatory_fold_angle: float = DEFAULT_FOLD_ANGLE
    user_seam_edges: set[int] = field(default_factory=set)
    user_protected_edges: set[int] = field(default_factory=set)
    chapters: list[UserChapter] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "object": self.object,
            "mode": self.mode,
            "mandatory_fold_angle": self.mandatory_fold_angle,
            "user_seam_edges": sorted(self.user_seam_edges),
            "user_protected_edges": sorted(self.user_protected_edges),
            "chapters": [c.to_dict() for c in self.chapters],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserSeamSpec":
        return cls(
            object=str(d.get("object", "")),
            version=int(d.get("version", SPEC_VERSION)),
            mode=str(d.get("mode", "user_seams")),
            mandatory_fold_angle=float(d.get("mandatory_fold_angle", DEFAULT_FOLD_ANGLE)),
            user_seam_edges={int(x) for x in d.get("user_seam_edges", [])},
            user_protected_edges={int(x) for x in d.get("user_protected_edges", [])},
            chapters=[UserChapter.from_dict(c) for c in d.get("chapters", [])],
            notes=str(d.get("notes", "")),
        )

    def effective_seam_edges(self) -> set[int]:
        """Spec-level user seam edges UNION every chapter's ``seam_edges`` (plan §6)."""
        out = set(self.user_seam_edges)
        for c in self.chapters:
            out |= set(c.seam_edges)
        return out

    def effective_protected_edges(self) -> set[int]:
        """Spec-level protected edges UNION every chapter's ``protected_edges``."""
        out = set(self.user_protected_edges)
        for c in self.chapters:
            out |= set(c.protected_edges)
        return out


def load_user_seam_spec(path: str) -> UserSeamSpec:
    """Load a user seam spec JSON file (plan §6) into a :class:`UserSeamSpec`."""
    with open(path, encoding="utf-8") as fh:
        return UserSeamSpec.from_dict(json.load(fh))


def save_user_seam_spec(spec: UserSeamSpec, path: str) -> None:
    """Round-trippable save (plan §11.1: ``UserSeamSpec JSON load/save``)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(spec.to_dict(), fh, indent=2)


@dataclass
class UserSeamResult:
    """The resolved seam decision for one spec + mesh (plan §7).

    ``initial_seams`` is what the unwrap starts from = mandatory folds UNION the (valid) user
    seam edges. ``forbidden_edges`` are the non-mandatory protected edges the engine must never
    cut through (passed to ``run_chart_uv(forbidden_edges=...)``). ``conflicts`` records every
    protected edge that was nonetheless mandatory (mandatory wins). ``invalid_edges`` are edge
    ids in the spec that don't exist on the mesh (plan §11.1: invalid-edge detection)."""

    initial_seams: set[int]
    forbidden_edges: set[int]
    mandatory_edges: set[int]
    user_seam_edges: set[int]        # valid (in-range) only
    user_protected_edges: set[int]   # valid (in-range) only
    conflicts: list[dict]
    invalid_edges: list[int]

    def report(self, *, final_seams: set[int] | None = None, auto_added: int = 0) -> dict:
        """The ``seam_report.json`` ``user_seams`` block (plan §9). ``final_seam_count`` and
        ``auto_added_seams`` come from the actual shipped seam set (the pipeline fills them in
        after the run); before a run they default to the initial seam set / 0."""
        final = set(final_seams) if final_seams is not None else self.initial_seams
        return {
            "user_seam_count": len(self.user_seam_edges),
            "user_protected_count": len(self.user_protected_edges),
            "mandatory_90_edges": len(self.mandatory_edges),
            "final_seam_count": len(final),
            "auto_added_seams": auto_added,
            # Actual edge id lists (not just counts) so the verification harness can assert
            # "every user seam shipped" / "no protected edge shipped" directly (plan §5/§122).
            "user_seam_edges": sorted(self.user_seam_edges),
            "user_protected_edges": sorted(self.user_protected_edges),
            "conflicts": self.conflicts,
            "invalid_edges": self.invalid_edges,
        }


def build_user_seam_set(mesh: MeshGraph, spec: UserSeamSpec) -> UserSeamResult:
    """Apply the plan §7 precedence to a spec + mesh and return a :class:`UserSeamResult`.

    Rules (plan §7):

    - mandatory ≥``spec.mandatory_fold_angle`` fold → always a seam.
    - ``user_seam_edges`` → applied as seams.
    - ``user_protected_edges`` → no seam, UNLESS the edge is also a mandatory fold, in which
      case mandatory wins and a conflict is recorded.
    - auto suggested seams → OFF (this module never adds any).

    Edge ids outside ``[0, edge_count)`` are dropped and listed in ``invalid_edges`` (an edge
    can't be a seam if it doesn't exist). The chapters' own seam/protected edges are merged in
    first (``spec.effective_*``)."""
    n = mesh.edge_count

    def in_range(eid: int) -> bool:
        return 0 <= eid < n

    seam_req = spec.effective_seam_edges()
    protected_req = spec.effective_protected_edges()

    user_seam_valid = {e for e in seam_req if in_range(e)}
    protected_valid = {e for e in protected_req if in_range(e)}
    invalid = sorted({e for e in (seam_req | protected_req) if not in_range(e)})

    mandatory = mandatory_seam_edges(mesh, fold_angle=spec.mandatory_fold_angle)

    # mandatory wins over a protected edge → conflict, the protected edge still ships as a seam.
    conflicts = [{"edge_id": e, "user_rule": "protected", "engine_rule": "mandatory_90",
                  "resolution": "mandatory_wins"}
                 for e in sorted(protected_valid & mandatory)]

    # forbidden = protected edges that are NEITHER mandatory NOR an explicit user seam (a user
    # seam edge that is also marked protected is contradictory; the higher-precedence seam wins).
    forbidden = protected_valid - mandatory - user_seam_valid

    initial_seams = mandatory | user_seam_valid

    return UserSeamResult(
        initial_seams=initial_seams,
        forbidden_edges=forbidden,
        mandatory_edges=mandatory,
        user_seam_edges=user_seam_valid,
        user_protected_edges=protected_valid,
        conflicts=conflicts,
        invalid_edges=invalid,
    )
