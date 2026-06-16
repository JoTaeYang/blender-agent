"""Interactive chapter seam planning (GUIDED_UV_CHAPTER_PLAN → interactive front-end).

The guided engine (:mod:`artist_uv_agent.guided`) is a deterministic BACK-END: given a
finished ``GuidedUVSpec`` it builds UVs that respect it and the hard gates. On coarse / messy
assets (``humanstatue_low.obj``) a *fully automatic* whole-object judgement rarely reaches
``guided_complete=True`` — faces, sleeves and hands do not separate cleanly.

This module is the INTERACTIVE FRONT-END that the plan asks for. Instead of judging the whole
object at once, the agent works one body part ("chapter") at a time:

    observe part → propose seam plan (draft) → user approves/edits → save chapter constraint
                                                                      → next part

When the parts that matter are approved, the accumulated, APPROVED chapters are exported to a
``GuidedUVSpec`` and run through the guided back-end; the result is checked against each
approved chapter's constraints (e.g. *the face front keeps zero smooth seams*) and reported
honestly — a failed constraint is never dressed up as success.

Everything here is pure Python on a :class:`~uv_agent.geometry.mesh_graph.MeshGraph`; the
optional Blender screenshot layer lives behind ``import bpy`` in :func:`observe_chapter` so the
data model, draft generation, spec export and constraint check are unit-tested without ``bpy``.

Data model (plan §작업1):
    :class:`ChapterSource`         which faces/parts a chapter covers
    :class:`ChapterIntent`         the human-readable goal + preserve/seam zones
    :class:`ChapterConstraints`    the machine-checkable rules (open vocabulary)
    :class:`ObservationSummary`    what we measured on the mesh for the chapter
    :class:`InteractiveChapterPlan`  one chapter's draft/approved plan (+ revision/notes)
    :class:`InteractiveUVPlan`     the accumulating session plan (load/save/upsert/export)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from chart_uv_agent.segmentation import flood_charts, mandatory_seam_edges
from uv_agent.geometry.mesh_graph import MeshGraph

# Chapter status vocabulary (plan §3 Approval).
STATUS_DRAFT = "draft"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_NEEDS_REVISION = "needs_revision"
CHAPTER_STATUSES = (STATUS_DRAFT, STATUS_APPROVED, STATUS_REJECTED, STATUS_NEEDS_REVISION)


# --- axis helpers (kept local so this module does not hard-depend on guided import order) ---

_AXIS_VECTORS = {
    "+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
}


def axis_vector(axis: str | None):
    """Parse ``"+Z"`` / ``"-Y"`` → unit vector, or ``None`` when empty/unknown."""
    if not axis:
        return None
    v = _AXIS_VECTORS.get(str(axis).strip().lower())
    return np.asarray(v, float) if v is not None else None


def _lateral_axis(front: str, up: str):
    """The world lateral axis ⟂ to both ``front`` and ``up`` (e.g. +Y up, +Z front → ±X).
    Used to define a chapter's CENTRE band for ``max_front_center_seams``. Falls back to +X."""
    f, u = axis_vector(front), axis_vector(up)
    if f is None or u is None:
        return np.asarray((1.0, 0.0, 0.0))
    lat = np.cross(u, f)
    n = np.linalg.norm(lat)
    return lat / n if n > 1e-9 else np.asarray((1.0, 0.0, 0.0))


# --- data model -------------------------------------------------------------------------

@dataclass
class ChapterSource:
    """Which mesh faces a chapter covers (plan §작업1). ``part_ids`` are COARSE
    connected-component ids (what the interactive flow reasons about); ``face_ids`` is an
    explicit override. ``selection_type`` is an annotation for the UI/report."""

    selection_type: str = "part_ids"          # "part_ids" | "face_ids" | "material"
    part_ids: list[int] = field(default_factory=list)
    face_ids: list[int] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ChapterSource":
        d = d or {}
        return cls(selection_type=str(d.get("selection_type", "part_ids")),
                   part_ids=[int(x) for x in d.get("part_ids", [])],
                   face_ids=[int(x) for x in d.get("face_ids", [])])

    def to_dict(self) -> dict:
        return {"selection_type": self.selection_type,
                "part_ids": list(self.part_ids), "face_ids": list(self.face_ids)}

    def resolve_faces(self, mesh: MeshGraph) -> list[int]:
        """Resolve the covered face ids. Explicit ``face_ids`` win; otherwise ``part_ids`` are
        resolved against the COARSE connected-component segmentation (the same parts the guided
        coarse path uses, so ids line up with ``guided_parts.json``)."""
        if self.face_ids:
            return sorted(self.face_ids)
        if not self.part_ids:
            return []
        from artist_uv_agent.guided import coarse_segment_parts
        seg = coarse_segment_parts(mesh)
        want = set(self.part_ids)
        return sorted(f for p in seg.parts if p.part_id in want for f in p.face_ids)


@dataclass
class ChapterIntent:
    """The human-readable goal + zone hints the agent shows the user for approval (plan §2)."""

    summary: str = ""
    preserve_zones: list[str] = field(default_factory=list)
    preferred_seam_zones: list[str] = field(default_factory=list)
    allowed_aux_islands: list[str] = field(default_factory=list)
    forbidden_zones: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ChapterIntent":
        d = d or {}
        return cls(summary=str(d.get("summary", "")),
                   preserve_zones=[str(x) for x in d.get("preserve_zones", [])],
                   preferred_seam_zones=[str(x) for x in d.get("preferred_seam_zones", [])],
                   allowed_aux_islands=[str(x) for x in d.get("allowed_aux_islands", [])],
                   forbidden_zones=[str(x) for x in d.get("forbidden_zones", [])])

    def to_dict(self) -> dict:
        return {"summary": self.summary, "preserve_zones": list(self.preserve_zones),
                "preferred_seam_zones": list(self.preferred_seam_zones),
                "allowed_aux_islands": list(self.allowed_aux_islands),
                "forbidden_zones": list(self.forbidden_zones)}


@dataclass
class ChapterConstraints:
    """The machine-checkable rules for a chapter (plan §2 ``hard_rules`` / §작업1). An OPEN
    vocabulary stored as a flat dict so the agent can name new rules freely; the subset that
    :func:`evaluate_interactive_constraints` knows how to MEASURE is checked against the guided
    result, the rest are reported as advisory (``checkable=false``) — never silently "passed".

    Recognised (machine-checked) keys:
        ``max_front_smooth_seams``   int  — front-facing low-dihedral seams allowed on the part
        ``max_front_center_seams``   int  — same, restricted to the part's CENTRE band
        ``min_panel_count`` / ``max_panel_count``  int — island count bounds for the part
        ``mandatory_folds_must_split``  bool — every ≥90° fold inside the part is a seam
    """

    values: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ChapterConstraints":
        return cls(values=dict(d or {}))

    def to_dict(self) -> dict:
        return dict(self.values)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def __bool__(self) -> bool:
        return bool(self.values)


@dataclass
class ObservationSummary:
    """What we measured on the mesh for a chapter (plan §작업2 / §file2). Pure mesh stats +
    risk flags; the optional ``screenshots`` are filled by the Blender layer."""

    chapter: str = ""
    source: ChapterSource = field(default_factory=ChapterSource)
    face_count: int = 0
    bbox: dict = field(default_factory=dict)               # {"min": [x,y,z], "max": [x,y,z]}
    boundary_loop_count: int = 0
    mandatory_fold_count: int = 0
    front_smooth_edge_count: int = 0
    current_chart_count: int | None = None
    normal_axis_histogram: dict = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)
    front_axis: str = ""
    up_axis: str = ""
    observed_at: str = ""
    screenshots: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ObservationSummary":
        d = d or {}
        return cls(
            chapter=str(d.get("chapter", "")),
            source=ChapterSource.from_dict(d.get("source")),
            face_count=int(d.get("face_count", 0)),
            bbox=dict(d.get("bbox", {})),
            boundary_loop_count=int(d.get("boundary_loop_count", 0)),
            mandatory_fold_count=int(d.get("mandatory_fold_count", 0)),
            front_smooth_edge_count=int(d.get("front_smooth_edge_count", 0)),
            current_chart_count=(None if d.get("current_chart_count") is None
                                 else int(d["current_chart_count"])),
            normal_axis_histogram=dict(d.get("normal_axis_histogram", {})),
            risk_flags=[str(x) for x in d.get("risk_flags", [])],
            front_axis=str(d.get("front_axis", "")),
            up_axis=str(d.get("up_axis", "")),
            observed_at=str(d.get("observed_at", "")),
            screenshots=[str(x) for x in d.get("screenshots", [])],
        )

    def to_dict(self) -> dict:
        return {"chapter": self.chapter, "source": self.source.to_dict(),
                "face_count": self.face_count, "bbox": self.bbox,
                "boundary_loop_count": self.boundary_loop_count,
                "mandatory_fold_count": self.mandatory_fold_count,
                "front_smooth_edge_count": self.front_smooth_edge_count,
                "current_chart_count": self.current_chart_count,
                "normal_axis_histogram": self.normal_axis_histogram,
                "risk_flags": list(self.risk_flags),
                "front_axis": self.front_axis, "up_axis": self.up_axis,
                "observed_at": self.observed_at, "screenshots": list(self.screenshots)}


@dataclass
class InteractiveChapterPlan:
    """One chapter's plan (plan §작업1). ``kind`` selects the seam-plan template;
    ``guided_type`` is the :class:`~artist_uv_agent.guided.GuidedChapter` type the approved plan
    exports to. ``revision`` bumps on every content edit (plan §작업4: revise, don't overwrite);
    ``user_notes`` is preserved across re-drafts."""

    name: str
    kind: str = "generic"
    status: str = STATUS_DRAFT
    revision: int = 1
    guided_type: str = "auto"
    seam_policy: str = ""
    source: ChapterSource = field(default_factory=ChapterSource)
    intent: ChapterIntent = field(default_factory=ChapterIntent)
    constraints: ChapterConstraints = field(default_factory=ChapterConstraints)
    selector: dict | None = None
    observation: ObservationSummary | None = None
    user_notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "InteractiveChapterPlan":
        obs = d.get("observation")
        sel = d.get("selector")
        return cls(
            name=str(d.get("name", "")),
            kind=str(d.get("kind", "generic")),
            status=str(d.get("status", STATUS_DRAFT)),
            revision=int(d.get("revision", 1)),
            guided_type=str(d.get("guided_type", "auto")),
            seam_policy=str(d.get("seam_policy", "")),
            source=ChapterSource.from_dict(d.get("source")),
            intent=ChapterIntent.from_dict(d.get("intent")),
            constraints=ChapterConstraints.from_dict(d.get("constraints")),
            selector=dict(sel) if isinstance(sel, dict) else None,
            observation=ObservationSummary.from_dict(obs) if obs else None,
            user_notes=str(d.get("user_notes", "")),
        )

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "status": self.status,
                "revision": self.revision, "guided_type": self.guided_type,
                "seam_policy": self.seam_policy, "source": self.source.to_dict(),
                "intent": self.intent.to_dict(), "constraints": self.constraints.to_dict(),
                "selector": self.selector,
                "observation": self.observation.to_dict() if self.observation else None,
                "user_notes": self.user_notes}

    def to_guided_chapter_dict(self) -> dict:
        """The :class:`~artist_uv_agent.guided.GuidedChapter` dict this approved plan exports to
        (plan §작업6). Both the coarse ``part_ids`` AND any explicit ``face_ids`` are carried so a
        face-set selection survives to the guided backend (carved into its own part there)."""
        return {"name": self.name, "source_part_ids": list(self.source.part_ids),
                "source_face_ids": list(self.source.face_ids),
                "type": self.guided_type, "seam_policy": self.seam_policy,
                "selector": self.selector}


@dataclass
class InteractiveUVPlan:
    """The accumulating session plan (plan §file1 ``interactive_uv_plan.json``). Holds every
    chapter the agent has observed/drafted/approved plus the asset-level axes and preserve
    set, and exports the APPROVED subset to a :class:`~artist_uv_agent.guided.GuidedUVSpec`."""

    object: str = ""
    version: int = 1
    front_axis: str = ""
    up_axis: str = ""
    current_chapter: str = ""
    segmentation_mode: str = "coarse"
    mandatory_fold_angle: float = 90.0
    forbidden_edges: list[int] = field(default_factory=list)
    front_preserve_threshold: float = 0.6
    front_preserve_max_dihedral: float = 30.0
    chapters: list[InteractiveChapterPlan] = field(default_factory=list)

    # -- (de)serialisation --
    @classmethod
    def from_dict(cls, d: dict) -> "InteractiveUVPlan":
        return cls(
            object=str(d.get("object", "")),
            version=int(d.get("version", 1)),
            front_axis=str(d.get("front_axis", "")),
            up_axis=str(d.get("up_axis", "")),
            current_chapter=str(d.get("current_chapter", "")),
            segmentation_mode=str(d.get("segmentation_mode", "coarse")),
            mandatory_fold_angle=float(d.get("mandatory_fold_angle", 90.0)),
            forbidden_edges=[int(e) for e in d.get("forbidden_edges", [])],
            front_preserve_threshold=float(d.get("front_preserve_threshold", 0.6)),
            front_preserve_max_dihedral=float(d.get("front_preserve_max_dihedral", 30.0)),
            chapters=[InteractiveChapterPlan.from_dict(c) for c in d.get("chapters", [])],
        )

    @classmethod
    def from_json(cls, text: str) -> "InteractiveUVPlan":
        return cls.from_dict(json.loads(text))

    @classmethod
    def coerce(cls, plan) -> "InteractiveUVPlan":
        """Accept an :class:`InteractiveUVPlan`, a dict, a JSON string, or a path to a JSON
        file (so callers can pass ``--interactive-plan .context/interactive_uv_plan.json``)."""
        if isinstance(plan, InteractiveUVPlan):
            return plan
        if isinstance(plan, dict):
            return cls.from_dict(plan)
        if isinstance(plan, str):
            s = plan.strip()
            if s and s[0] in "{[":
                return cls.from_json(plan)
            return cls.load(plan)
        raise TypeError(f"cannot coerce {type(plan)!r} to InteractiveUVPlan")

    def to_dict(self) -> dict:
        return {"object": self.object, "version": self.version,
                "front_axis": self.front_axis, "up_axis": self.up_axis,
                "current_chapter": self.current_chapter,
                "segmentation_mode": self.segmentation_mode,
                "mandatory_fold_angle": self.mandatory_fold_angle,
                "forbidden_edges": list(self.forbidden_edges),
                "front_preserve_threshold": self.front_preserve_threshold,
                "front_preserve_max_dihedral": self.front_preserve_max_dihedral,
                "chapters": [c.to_dict() for c in self.chapters]}

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def load(cls, path: str) -> "InteractiveUVPlan":
        with open(path, encoding="utf-8") as fh:
            return cls.from_json(fh.read())

    def save(self, path: str) -> None:
        import os
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())

    # -- chapter access / mutation --
    def get_chapter(self, name: str) -> InteractiveChapterPlan | None:
        return next((c for c in self.chapters if c.name == name), None)

    def upsert_chapter(self, chapter: InteractiveChapterPlan) -> InteractiveChapterPlan:
        """Insert or UPDATE a chapter by name (plan §작업4). On update the revision is BUMPED
        (a content edit is a new revision, not an overwrite) and a non-empty existing
        ``user_notes`` is preserved when the incoming plan carries none. Sets
        ``current_chapter``. Returns the stored chapter."""
        existing = self.get_chapter(chapter.name)
        if existing is not None:
            chapter.revision = existing.revision + 1
            if not chapter.user_notes and existing.user_notes:
                chapter.user_notes = existing.user_notes
            self.chapters[self.chapters.index(existing)] = chapter
        else:
            self.chapters.append(chapter)
        self.current_chapter = chapter.name
        return chapter

    def set_status(self, name: str, status: str, *, user_notes: str | None = None
                   ) -> InteractiveChapterPlan:
        """Transition a chapter's status WITHOUT bumping its revision (approve/reject is a
        state change, not a content edit). Raises ``KeyError`` if the chapter is unknown or
        ``ValueError`` on an unknown status."""
        if status not in CHAPTER_STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {CHAPTER_STATUSES}")
        ch = self.get_chapter(name)
        if ch is None:
            raise KeyError(f"no chapter named {name!r}")
        ch.status = status
        if user_notes is not None:
            ch.user_notes = user_notes
        return ch

    def approved_chapters(self) -> list[InteractiveChapterPlan]:
        return [c for c in self.chapters if c.status == STATUS_APPROVED]

    def unapproved_chapters(self) -> list[InteractiveChapterPlan]:
        return [c for c in self.chapters if c.status != STATUS_APPROVED]

    def to_guided_spec(self, *, expected_intents: list[str] | None = None):
        """Build a :class:`~artist_uv_agent.guided.GuidedUVSpec` from the APPROVED chapters only
        (plan §작업6 + success criterion 4: an un-approved draft never reaches the spec).

        ``expected_intents`` defaults to EMPTY so the interactive flow is not penalised for
        canonical parts it has not reached yet (the gate is the per-chapter constraint check,
        not 'did you do every body part'). Pass an explicit list to also demand intent
        coverage."""
        from artist_uv_agent.guided import GuidedUVSpec

        chapters = [c.to_guided_chapter_dict() for c in self.approved_chapters()]
        return GuidedUVSpec.from_dict({
            "version": self.version,
            "object": self.object,
            "forbidden_edges": list(self.forbidden_edges),
            "mandatory_fold_angle": self.mandatory_fold_angle,
            "segmentation_mode": self.segmentation_mode,
            "front_preserve_axis": self.front_axis,
            "front_preserve_threshold": self.front_preserve_threshold,
            "front_preserve_max_dihedral": self.front_preserve_max_dihedral,
            "expected_intents": list(expected_intents or []),
            "chapters": chapters,
        })


# --- chapter observation (plan §작업2) ---------------------------------------------------

def _boundary_loop_count(mesh: MeshGraph, faces: set[int]) -> int:
    """Number of boundary loops of the sub-mesh ``faces`` — an edge is on the boundary when it
    has exactly one incident face INSIDE the set (a mesh-boundary edge counts too). Loops are
    the connected components of those edges over their shared vertices."""
    bedges: list[tuple[int, int]] = []
    seen: set[int] = set()
    for f in faces:
        for eid in mesh.faces[f].edge_ids:
            if eid in seen:
                continue
            seen.add(eid)
            e = mesh.edges[eid]
            inside = sum(1 for fid in e.face_ids if fid in faces)
            if inside == 1:
                v = e.vertex_ids
                if len(v) >= 2:
                    bedges.append((v[0], v[1]))
    if not bedges:
        return 0
    # union-find over the boundary edge endpoints → number of connected loops.
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in bedges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    return len({find(a) for a, _ in bedges})


def _normal_axis_histogram(mesh: MeshGraph, faces) -> dict[str, int]:
    """Bin each face's normal into its dominant world axis (+X..-Z) → counts."""
    names = ["+x", "-x", "+y", "-y", "+z", "-z"]
    hist = {n: 0 for n in names}
    for f in faces:
        n = np.asarray(mesh.faces[f].normal, float)
        ax = int(np.argmax(np.abs(n)))
        hist[names[ax * 2 + (0 if n[ax] >= 0 else 1)]] += 1
    return hist


def observe_chapter(mesh: MeshGraph, obj=None, chapter_name: str = "", source=None, *,
                    front_axis: str = "", up_axis: str = "", seams=None,
                    threshold: float = 0.5, max_dihedral: float = 45.0,
                    fold_angle: float = 90.0, observed_at: str = "",
                    camera_hint=None, screenshot_dir: str | None = None) -> ObservationSummary:
    """Observe one chapter on the mesh (plan §작업2). Pure mesh statistics + risk flags; works
    headless (``obj=None``) and from Blender. ``source`` is a :class:`ChapterSource` (or dict);
    ``seams`` (optional) lets ``current_chart_count`` reflect an existing seam set.

    When ``obj`` is a Blender object AND ``screenshot_dir`` is given, front/side/back viewport
    screenshots are saved and their paths recorded (see :func:`observe_chapter_screenshots`);
    this never runs headless and never affects the measured stats."""
    src = source if isinstance(source, ChapterSource) else ChapterSource.from_dict(source or {})
    faces = src.resolve_faces(mesh)
    fset = set(faces)
    fa = axis_vector(front_axis)

    # bbox over the covered faces' vertices.
    bbox: dict = {}
    if faces:
        pts = np.asarray([mesh.vertex_co(v) for f in faces for v in mesh.faces[f].vertex_ids],
                         float)
        bbox = {"min": [float(x) for x in pts.min(0)], "max": [float(x) for x in pts.max(0)]}

    mandatory = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    mand_in = sum(1 for e in mandatory
                  if len(mesh.edges[e].face_ids) == 2
                  and mesh.edges[e].face_ids[0] in fset and mesh.edges[e].face_ids[1] in fset)
    front_smooth = _front_smooth_edges(mesh, fset, axis=fa, threshold=threshold,
                                       max_dihedral=max_dihedral, exclude=mandatory)

    chart_count = None
    if seams is not None and faces:
        charts = flood_charts(mesh, set(seams))
        chart_count = sum(1 for fs in charts if fset.issuperset(fs) or (fset & set(fs)))

    bl = _boundary_loop_count(mesh, fset)
    hist = _normal_axis_histogram(mesh, faces)
    risks = _observation_risks(bl, hist, front_smooth, len(faces), front_axis)

    obs = ObservationSummary(
        chapter=chapter_name, source=src, face_count=len(faces), bbox=bbox,
        boundary_loop_count=bl, mandatory_fold_count=mand_in,
        front_smooth_edge_count=len(front_smooth), current_chart_count=chart_count,
        normal_axis_histogram=hist, risk_flags=risks,
        front_axis=front_axis, up_axis=up_axis, observed_at=observed_at)

    if obj is not None and screenshot_dir is not None:
        try:
            obs.screenshots = observe_chapter_screenshots(
                obj, faces, chapter_name, screenshot_dir, front_axis=front_axis,
                up_axis=up_axis, camera_hint=camera_hint)
        except Exception as exc:  # noqa: BLE001 — screenshots are best-effort, never fatal
            obs.risk_flags.append(f"screenshot_failed:{exc}")
    return obs


def _front_smooth_edges(mesh: MeshGraph, fset: set[int], *, axis, threshold: float,
                        max_dihedral: float, exclude=None) -> list[int]:
    """Interior edges of ``fset`` that face ``axis`` and are LOW-dihedral (smooth), excluding
    mandatory folds. These are the edges the artist wants to KEEP unbroken on the visible
    front — both the observation count and the constraint check use this."""
    if axis is None:
        return []
    exclude = exclude or set()
    out: list[int] = []
    for f in fset:
        for eid in mesh.faces[f].edge_ids:
            e = mesh.edges[eid]
            if eid in exclude or len(e.face_ids) != 2 or e.dihedral_angle >= max_dihedral:
                continue
            a, b = e.face_ids
            if a not in fset or b not in fset or a != f:   # count each interior edge once
                continue
            nv = np.asarray(mesh.faces[a].normal, float) + np.asarray(mesh.faces[b].normal, float)
            nn = np.linalg.norm(nv)
            if nn > 1e-9 and float(np.dot(nv / nn, axis)) > threshold:
                out.append(eid)
    return out


def _observation_risks(boundary_loops: int, hist: dict, front_smooth: list, face_count: int,
                       front_axis: str) -> list[str]:
    risks: list[str] = []
    if boundary_loops == 0:
        risks.append("closed_cap_no_boundary_disk_cut_required")
    elif boundary_loops == 1:
        risks.append("single_boundary_loop_disk_cut_may_need_hidden_back_seam")
    if front_axis:
        fa = axis_vector(front_axis)
        if fa is not None:
            ax = int(np.argmax(np.abs(fa)))
            pos, neg = (("+", "-") if fa[ax] >= 0 else ("-", "+"))
            names = "xyz"
            front_n = hist.get(f"{pos}{names[ax]}", 0)
            back_n = hist.get(f"{neg}{names[ax]}", 0)
            if front_n and back_n and min(front_n, back_n) > 0.25 * max(front_n, back_n):
                risks.append("coarse_part_mixes_front_and_back_faces")
    if face_count and len(front_smooth) > 0.4 * face_count:
        risks.append("high_front_smooth_edge_density")
    return risks


def observe_chapter_screenshots(obj, faces, chapter_name: str, out_dir: str, *,
                                front_axis: str = "+Z", up_axis: str = "+Y",
                                camera_hint=None) -> list[str]:
    """Blender-only: highlight ``faces`` and save front/side/back viewport screenshots (plan
    §작업5). Imports ``bpy`` lazily so the module loads headless. Returns the saved paths.

    Selects the chapter faces (so the user can confirm "is this the right part?"), frames the
    object, and renders three orthographic views. The agent can instead drive this through the
    Blender MCP (``get_viewport_screenshot`` after the same selection) — the selection /
    framing logic is identical."""
    import os

    import bpy  # noqa: F401 — Blender runtime only

    os.makedirs(out_dir, exist_ok=True)
    fa = axis_vector(front_axis)
    ua = axis_vector(up_axis)
    if fa is None:
        fa = np.asarray((0.0, 0.0, 1.0))
    if ua is None:
        ua = np.asarray((0.0, 1.0, 0.0))

    # Highlight the chapter faces in edit mode so the saved view shows exactly the selection.
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="OBJECT")
    for poly in obj.data.polygons:
        poly.select = poly.index in set(faces)

    paths: list[str] = []
    views = {"front": fa, "side": np.cross(ua, fa), "back": -fa}
    for label, direction in views.items():
        path = os.path.join(out_dir, f"{chapter_name}_{label}.png")
        try:
            _render_view(obj, direction, ua, path)
            paths.append(path)
        except Exception:  # noqa: BLE001 — keep going; record whatever rendered
            continue
    return paths


def _render_view(obj, direction, up, path: str) -> None:
    """Point a temporary camera at ``obj`` along ``-direction`` and render to ``path`` (Blender
    runtime only)."""
    import bpy

    bbox = np.asarray([obj.matrix_world @ v.co for v in obj.data.vertices], float)
    center = bbox.mean(0)
    radius = float(np.linalg.norm(bbox.max(0) - bbox.min(0))) * 0.6 + 1e-3
    d = np.asarray(direction, float)
    d = d / (np.linalg.norm(d) + 1e-9)
    cam_data = bpy.data.cameras.new("interactive_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = radius * 2.2
    cam = bpy.data.objects.new("interactive_cam", cam_data)
    bpy.context.scene.collection.objects.link(cam)
    loc = center + d * radius * 3.0
    cam.location = loc
    fwd = -d
    up_v = np.asarray(up, float)
    right = np.cross(fwd, up_v); right /= (np.linalg.norm(right) + 1e-9)
    true_up = np.cross(right, fwd)
    rot = np.column_stack([right, true_up, -fwd])
    from mathutils import Matrix
    cam.matrix_world = Matrix(((rot[0, 0], rot[0, 1], rot[0, 2], loc[0]),
                              (rot[1, 0], rot[1, 1], rot[1, 2], loc[1]),
                              (rot[2, 0], rot[2, 1], rot[2, 2], loc[2]),
                              (0, 0, 0, 1)))
    scene = bpy.context.scene
    scene.camera = cam
    scene.render.filepath = path
    scene.render.image_settings.file_format = "PNG"
    scene.render.resolution_x = scene.render.resolution_y = 640
    bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(cam, do_unlink=True)


# --- seam-plan draft templates (plan §작업3) ---------------------------------------------

# kind → (guided_type, seam_policy, intent, constraints[, selector]). The CONSTRAINTS are the
# machine-checkable promise; the INTENT is the human-readable plan shown for approval. Tuned to
# the humanstatue chapters in the plan (§권장 부위 진행 순서) but generic enough for any asset.
CHAPTER_TEMPLATES: dict[str, dict] = {
    "face": {
        "guided_type": "face_front_preserve",
        "seam_policy": "single_front_island_back_head_chin_seams",
        "intent": {
            "summary": "Keep the face front as one visible island.",
            "preserve_zones": ["face_front_center", "nose", "eyes", "beard_front"],
            "preferred_seam_zones": ["behind_ears", "under_chin", "hood_inside"],
            "allowed_aux_islands": ["beard_mass_if_deep_boundary"],
        },
        "constraints": {"max_front_smooth_seams": 0, "max_front_center_seams": 0,
                        "mandatory_folds_must_split": True, "allow_beard_island": True},
    },
    "beard": {
        "guided_type": "face_front_preserve",
        "seam_policy": "back_or_under_only",
        "intent": {"summary": "Beard front stays unbroken; cut only behind/under.",
                   "preserve_zones": ["beard_front"],
                   "preferred_seam_zones": ["under_chin", "beard_back"],
                   "allowed_aux_islands": ["beard_mass_if_deep_boundary"]},
        "constraints": {"max_front_smooth_seams": 0, "allow_beard_island": True},
    },
    "hood": {
        "guided_type": "shell",
        "seam_policy": "back_center",
        "intent": {"summary": "Hood is one shell; seam hidden at the back centre.",
                   "preserve_zones": ["hood_front"], "preferred_seam_zones": ["back_center",
                   "neck_back", "hood_inside"], "allowed_aux_islands": []},
        "constraints": {"max_front_smooth_seams": 0},
    },
    "staff": {
        "guided_type": "cylinder_group",
        "seam_policy": "shaft_back_vertical_prongs_separate",
        "intent": {"summary": "Shaft is one back-seamed strip; each prong its own strip.",
                   "preserve_zones": ["shaft_front"],
                   "preferred_seam_zones": ["shaft_back", "prong_junction"],
                   "allowed_aux_islands": ["prong_strips", "junction_island"]},
        "constraints": {"require_tube_strip": True},
    },
    "hand": {
        "guided_type": "blob",
        "seam_policy": "palm_and_contact_seam",
        "intent": {"summary": "Hand split at palm / staff-contact, back of hand protected.",
                   "preserve_zones": ["hand_back"],
                   "preferred_seam_zones": ["palm", "staff_contact", "wrist_inside"],
                   "allowed_aux_islands": ["finger_strips"]},
        "constraints": {},
    },
    "sleeve": {
        "guided_type": "cylinder",
        "seam_policy": "inner_arm_seam_cuff_island",
        "intent": {"summary": "Sleeve is a tube cut on the inner arm; cuff ring may split off.",
                   "preserve_zones": ["sleeve_outer_silhouette"],
                   "preferred_seam_zones": ["inner_arm", "armpit", "body_contact"],
                   "allowed_aux_islands": ["cuff_ring"]},
        "constraints": {"forbid_outer_silhouette_seam": True, "allow_cuff_island": True,
                        "require_tube_strip": True},
    },
    "upper_front_robe": {
        "guided_type": "robe_front_panel",
        "seam_policy": "lapel_and_side_hidden",
        "selector": {"normal_axis": "+Z", "threshold": 0.3},
        "intent": {"summary": "Front torso robe is one panel; seams at lapel / under-arm.",
                   "preserve_zones": ["chest_front"],
                   "preferred_seam_zones": ["lapel", "side", "under_arm"],
                   "allowed_aux_islands": []},
        "constraints": {"max_front_smooth_seams": 0, "forbid_shallow_front_triangles": True},
    },
    "back_cloak": {
        "guided_type": "back_large_panel",
        "seam_policy": "back_center_or_side",
        "selector": {"normal_axis": "-Z", "threshold": 0.3},
        "intent": {"summary": "Back cloak is one large island; seam at back centre or sides.",
                   "preserve_zones": ["back_center"],
                   "preferred_seam_zones": ["back_center", "side"], "allowed_aux_islands": []},
        "constraints": {},
    },
    "belt": {
        "guided_type": "strip",
        "seam_policy": "hidden_under_overlap",
        "intent": {"summary": "Belt is a thin strip; seam hidden under the overlap; knot may split.",
                   "preserve_zones": ["belt_front"],
                   "preferred_seam_zones": ["belt_overlap", "belt_back"],
                   "allowed_aux_islands": ["knot_island"]},
        "constraints": {},
    },
    "lower_robe": {
        "guided_type": "cloth_panels",
        "seam_policy": "deep_valleys_only",
        "intent": {"summary": "Lower robe splits into 3-6 vertical pleat panels on deep valleys.",
                   "preserve_zones": ["pleat_faces"],
                   "preferred_seam_zones": ["deep_valley", "side", "back_center"],
                   "forbidden_zones": ["shallow_front_triangles"], "allowed_aux_islands": []},
        "constraints": {"min_panel_count": 3, "max_panel_count": 6,
                        "forbid_shallow_front_triangles": True},
    },
    "foot": {
        "guided_type": "cap",
        "seam_policy": "sole_and_heel_seam",
        "intent": {"summary": "Foot cut at sole / heel / inner side; instep protected.",
                   "preserve_zones": ["instep"],
                   "preferred_seam_zones": ["sole", "heel", "inner_side"],
                   "allowed_aux_islands": []},
        "constraints": {},
    },
    "generic": {
        "guided_type": "auto",
        "seam_policy": "organic_split",
        "intent": {"summary": "Generic part; organic split with hidden seams.",
                   "preserve_zones": [], "preferred_seam_zones": [], "allowed_aux_islands": []},
        "constraints": {},
    },
}


def draft_seam_plan(observation: ObservationSummary, chapter_kind: str = "",
                    artist_preferences: dict | None = None) -> InteractiveChapterPlan:
    """Propose a seam-plan DRAFT for a chapter from its observation + kind template (plan
    §작업3). The draft is ``status="draft"`` — it never reaches the guided spec until the user
    approves it. ``artist_preferences`` shallow-overrides the template's ``intent`` /
    ``constraints`` / ``guided_type`` / ``seam_policy`` / ``selector``.

    The template is tuned by the observation: e.g. a part that mixes front & back faces gets a
    note that it may need a hidden back reroute; a face with no deep beard boundary drops the
    beard aux-island allowance."""
    kind = chapter_kind or observation.chapter or "generic"
    tpl = CHAPTER_TEMPLATES.get(kind, CHAPTER_TEMPLATES["generic"])
    prefs = artist_preferences or {}

    intent = ChapterIntent.from_dict({**tpl["intent"], **prefs.get("intent", {})})
    constraints = ChapterConstraints.from_dict({**tpl.get("constraints", {}),
                                                **prefs.get("constraints", {})})
    selector = prefs.get("selector", tpl.get("selector"))
    notes = []
    if "coarse_part_mixes_front_and_back_faces" in observation.risk_flags:
        notes.append("Observed part mixes front/back faces — seam should reroute behind, not "
                     "across the visible front.")
    if "single_boundary_loop_disk_cut_may_need_hidden_back_seam" in observation.risk_flags:
        notes.append("Single boundary loop — disk cut may need a hidden back seam.")

    return InteractiveChapterPlan(
        name=observation.chapter or kind,
        kind=kind,
        status=STATUS_DRAFT,
        revision=1,
        guided_type=str(prefs.get("guided_type", tpl["guided_type"])),
        seam_policy=str(prefs.get("seam_policy", tpl["seam_policy"])),
        source=observation.source,
        intent=intent,
        constraints=constraints,
        selector=dict(selector) if isinstance(selector, dict) else None,
        observation=observation,
        user_notes="\n".join(notes),
    )


def describe_plan_for_approval(chapter: InteractiveChapterPlan) -> str:
    """Human-readable summary the agent shows the user before approval (plan §step3). Lists the
    goal, the preserve/seam zones, the allowed aux islands, and the machine-checkable rules."""
    it = chapter.intent
    lines = [f"[{chapter.name}] ({chapter.kind}, rev {chapter.revision}) — {chapter.status}",
             f"  goal: {it.summary}"]
    if it.preserve_zones:
        lines.append(f"  preserve (no seam): {', '.join(it.preserve_zones)}")
    if it.preferred_seam_zones:
        lines.append(f"  prefer seam at: {', '.join(it.preferred_seam_zones)}")
    if it.allowed_aux_islands:
        lines.append(f"  allowed extra islands: {', '.join(it.allowed_aux_islands)}")
    if chapter.constraints:
        rules = ", ".join(f"{k}={v}" for k, v in chapter.constraints.to_dict().items())
        lines.append(f"  rules: {rules}")
    if chapter.user_notes:
        lines.append(f"  notes: {chapter.user_notes}")
    return "\n".join(lines)


# --- constraint verification against the guided result (plan §작업7) ---------------------

def _front_smooth_seam_count(mesh: MeshGraph, faces: set[int], seams: set[int], axis, *,
                             threshold: float, max_dihedral: float,
                             center_band=None) -> int:
    """Count FINAL seams crossing a front-facing low-dihedral edge whose BOTH faces are in
    ``faces`` — the exact "front-smooth seams the artist forbade" measure, restricted to one
    chapter. ``center_band`` (``(lat_axis, lo, hi)``) further restricts to the part's centre."""
    if axis is None:
        return 0
    n = 0
    for e in seams:
        ed = mesh.edges[e]
        if len(ed.face_ids) != 2 or ed.dihedral_angle >= max_dihedral:
            continue
        a, b = ed.face_ids
        if a not in faces or b not in faces:
            continue
        nv = np.asarray(mesh.faces[a].normal, float) + np.asarray(mesh.faces[b].normal, float)
        nn = np.linalg.norm(nv)
        if nn <= 1e-9 or float(np.dot(nv / nn, axis)) <= threshold:
            continue
        if center_band is not None:
            lat, lo, hi = center_band
            mid = np.mean([mesh.vertex_co(v) for v in ed.vertex_ids], axis=0)
            t = float(np.dot(mid, lat))
            if t < lo or t > hi:
                continue
        n += 1
    return n


def _center_band(mesh: MeshGraph, faces, lat_axis, *, frac: float = 0.5):
    """The central ``frac`` slice of ``faces`` along ``lat_axis`` → ``(lat_axis, lo, hi)``."""
    if not faces:
        return None
    ts = [float(np.dot(mesh.vertex_co(v), lat_axis))
          for f in faces for v in mesh.faces[f].vertex_ids]
    lo, hi = min(ts), max(ts)
    mid, half = (lo + hi) / 2.0, (hi - lo) * frac / 2.0
    return (lat_axis, mid - half, mid + half)


def evaluate_interactive_constraints(plan, mesh: MeshGraph, assignment, chapter_charts: dict,
                                     seams, *, front_axis: str = "", up_axis: str = "",
                                     fold_angle: float = 90.0, threshold: float = 0.6,
                                     max_dihedral: float = 30.0) -> dict:
    """Verify the APPROVED chapters' constraints against the guided result (plan §작업7).

    For each approved chapter, the matching guided chapter (by name) supplies the face set and
    island count; each recognised constraint is measured on the FINAL seams and reported
    ``{expected, actual, passed, checkable}``. Un-recognised rules are reported
    ``checkable=false`` (advisory) — counted neither as pass nor fail, never hidden.
    ``interactive_constraints_passed`` is True iff every CHECKABLE constraint passed. Returns
    the ``interactive_plan`` report block (plan §작업7)."""
    plan = InteractiveUVPlan.coerce(plan)
    seams = set(seams)
    front_axis = front_axis or plan.front_axis
    up_axis = up_axis or plan.up_axis
    fa = axis_vector(front_axis)
    lat = _lateral_axis(front_axis, up_axis)
    mandatory = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    by_name = {c.name: c for c in assignment.chapters}

    approved = plan.approved_chapters()
    results: dict[str, dict] = {}
    unresolved: list[str] = []
    checked = 0
    all_pass = True
    for ch in approved:
        rc = by_name.get(ch.name)
        # An APPROVED chapter that does not map to a guided chapter with actual faces is an
        # interactive FAILURE — the approved judgement never reached the result, so it must NOT
        # read as success (e.g. a dropped name, empty source, or invalid part/face ids).
        if rc is None or not rc.face_ids:
            results[ch.name] = {"chapter_resolved": False,
                                "reason": ("no guided chapter of this name in the result"
                                           if rc is None else
                                           "chapter resolved no faces (empty/invalid source)"),
                                "constraints": {}}
            unresolved.append(ch.name)
            all_pass = False
            continue
        faces = set(rc.face_ids)
        charts = list(chapter_charts.get(rc.index, []))
        cres: dict[str, dict] = {}
        for key, expected in ch.constraints.to_dict().items():
            r = _check_constraint(key, expected, mesh=mesh, faces=faces, charts=charts,
                                  seams=seams, mandatory=mandatory, axis=fa, lat=lat,
                                  threshold=threshold, max_dihedral=max_dihedral)
            cres[key] = r
            if r["checkable"]:
                checked += 1
                all_pass = all_pass and r["passed"]
        results[ch.name] = {"chapter_resolved": True, "island_count": len(charts),
                            "face_count": len(faces), "constraints": cres}

    return {
        "approved_chapter_count": len(approved),
        "approved_chapters": [c.name for c in approved],
        "unapproved_chapters": [c.name for c in plan.unapproved_chapters()],
        "unresolved_approved_chapters": unresolved,
        "checked_constraint_count": checked,
        "interactive_constraints_passed": bool(all_pass),
        "front_axis": front_axis, "up_axis": up_axis,
        "constraint_results": results,
    }


def _check_constraint(key: str, expected, *, mesh, faces, charts, seams, mandatory, axis, lat,
                      threshold, max_dihedral) -> dict:
    """Measure one constraint on the final result. Recognised keys are machine-checked
    (``checkable=true``); anything else is advisory (``checkable=false``, never gates)."""
    def res(actual, passed, *, note=""):
        return {"expected": expected, "actual": actual, "passed": bool(passed),
                "checkable": True, "note": note}

    if key == "max_front_smooth_seams":
        actual = _front_smooth_seam_count(mesh, faces, seams, axis, threshold=threshold,
                                          max_dihedral=max_dihedral)
        if axis is None:
            return {"expected": expected, "actual": None, "passed": True, "checkable": False,
                    "note": "no front axis set — front-smooth check skipped"}
        return res(actual, actual <= int(expected))
    if key == "max_front_center_seams":
        if axis is None:
            return {"expected": expected, "actual": None, "passed": True, "checkable": False,
                    "note": "no front axis set — centre check skipped"}
        band = _center_band(mesh, faces, lat)
        actual = _front_smooth_seam_count(mesh, faces, seams, axis, threshold=threshold,
                                          max_dihedral=max_dihedral, center_band=band)
        return res(actual, actual <= int(expected), note="centre 50% band along lateral axis")
    if key == "min_panel_count":
        return res(len(charts), len(charts) >= int(expected))
    if key == "max_panel_count":
        return res(len(charts), len(charts) <= int(expected))
    if key == "mandatory_folds_must_split":
        missing = sum(1 for e in mandatory
                      if len(mesh.edges[e].face_ids) == 2
                      and mesh.edges[e].face_ids[0] in faces
                      and mesh.edges[e].face_ids[1] in faces and e not in seams)
        passed = (missing == 0) if expected else True
        return res(missing, passed, note="count of ≥fold-angle folds inside the part NOT cut")
    # Unrecognised / not-yet-measurable rule: report honestly as advisory.
    return {"expected": expected, "actual": None, "passed": True, "checkable": False,
            "note": "advisory rule — not machine-checked against the seam result"}
