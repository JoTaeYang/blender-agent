"""Guided UV chapter flow (GUIDED_UV_CHAPTER_PLAN).

A HYBRID entry point: an artist/agent decides — per body part — how the UV should be
split into "chapters" (staff shaft, hood, face/beard, robe panels, …) and writes that
judgement into a deterministic ``chapter_spec``; this module turns the spec into a
concrete seam set and UVs, leaning on ``artist_uv_agent`` for the semantic part layer and
on ``chart_uv_agent`` for the hard correctness layer (mandatory ≥90° folds, no overlap,
forbidden-edge preservation).

It does NOT make the part judgement itself — the agent/LLM does that upstream (Blender MCP
screenshots + mesh inspection) and hands a finished ``GuidedUVSpec`` here. The goal of v1
is exactly: *given a human/agent part judgement as a spec, can we deterministically build
UVs that respect it AND the hard gates?* (auto-drafting the spec is follow-up work.)

Pipeline (``run_guided_uv`` is the Blender entry point; everything down to
:func:`build_guided_seams` is pure / Blender-free so the spec→seam logic is unit-tested
without ``bpy``):

    A1 segment parts → split tube forks → A2 descriptors → A3 classify
      (reused verbatim from artist_uv_agent — the auto part table the agent annotated)
    → assign each part to a spec chapter (or a class-based FALLBACK chapter)
    → build seams per chapter SEAM POLICY on the part-boundary floor, dissolving
      same-chapter internal boundaries, honouring forbidden / no-cut edges
    → SLIM unwrap + pack → mandatory-fold UV audit → minimal welded-fold repair
      (forbidden-aware) → auxiliary-seam prune → hard + quality gate + report.

Seam taxonomy (reported in ``guided_uv_report.json``): ``chapter_boundary``,
``chapter_template``, ``mandatory_90``, ``welded_fold_auxiliary``, ``overlap_repair``,
``distortion_repair``, ``fallback_segmentation``, ``user_forbidden`` (should never ship).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

from artist_uv_agent.classification import PartClass, classify_parts
from artist_uv_agent.descriptors import describe_parts, quiet_fp
from artist_uv_agent.seams import (
    _diskify_and_split, cylinder_template, open_multiloop_tube, uv_is_disk,
)
from artist_uv_agent.segmentation import (
    Part, PartSegmentation, part_seam_edges, segment_parts, split_branched_parts,
)
from chart_uv_agent.segmentation import flood_charts, mandatory_seam_edges
from uv_agent.geometry.mesh_graph import MeshGraph


# --- chapter type vocabulary ------------------------------------------------------------
# Known chapter types and the deterministic SEAM BEHAVIOUR each maps to. An UNKNOWN type is
# NOT an error (plan §1/§step1): it falls back to the chart-style organic split, so the spec
# can name new chapter types freely without breaking generation.
#   cylinder        a single tube (staff shaft, limb)         → cylinder_template
#   cylinder_group  a branched tube fork (trident prongs)     → per-part cylinder_template
#   cloth_panel     one large cloth area (front robe)         → keep intact (diskify only)
#   cloth_panels    a multi-panel cloth region (lower robe)   → split on deep valleys/creases
#   organic_front_preserve  face/beard, front visible         → keep intact, NO front seams
#   panel/strip/cap/detail  flat / thin / small              → keep intact (diskify only)
#   blob/shell/unknown/auto fallback organic mass             → chart organic split
_BEHAVIOR = {
    "cylinder": "cylinder",
    "cylinder_group": "cylinder_group",
    "cloth_panel": "keep_intact",
    "robe_front_panel": "keep_intact",
    "back_large_panel": "keep_intact",
    "panel": "keep_intact",
    "strip": "keep_intact",
    "cap": "keep_intact",
    "detail": "keep_intact",
    "cloth_panels": "organic_split",
    "organic_front_preserve": "front_preserve",
    # Face front island (work plan §1): same SEAM behaviour as front_preserve (keep intact +
    # front-edge protection) but reported under a dedicated face_policy with a centre-band check.
    "face_front_preserve": "front_preserve",
    "blob": "organic_split",
    "shell": "organic_split",
    "unknown": "organic_split",
    "auto": "organic_split",
    # A COARSE connected-component chapter (fast path) is kept intact (diskify only, no
    # stretch-driven cone split) — the goal is a quick reportable result, not a polished auto
    # decomposition (the agent is expected to refine via source_part_ids).
    "coarse": "keep_intact",
}
FALLBACK_BEHAVIOR = "organic_split"      # any chapter type not in _BEHAVIOR
DEFAULT_FOLD_ANGLE = 90.0
DEFAULT_CONE_LIMIT = 55.0
DEFAULT_MAX_CHARTS = 80

# Segmentation modes (plan: compute only as much as the spec needs). "auto" → coarse when no
# chapter fills source_part_ids (nothing to validate against a deep auto decomposition), else
# full. "coarse" = connected-component shells (instant). "full"/"manual_parts" = the deep
# artist watershed (segment_parts) so hand-filled part ids resolve.
SEGMENTATION_MODES = ("auto", "coarse", "full", "manual_parts")

# Map an A3 part class → the fallback chapter type used when the spec does not cover a part.
_CLASS_TO_CHAPTER = {
    "cylinder": "cylinder", "cap": "cap", "panel": "panel", "strip": "strip",
    "blob": "blob", "detail": "detail", "shell": "shell", "unknown": "auto",
    "coarse": "coarse",
}


def chapter_behavior(chapter_type: str) -> str:
    """The deterministic seam behaviour for a chapter type; unknown → fallback (never an
    error, plan §step1)."""
    return _BEHAVIOR.get(chapter_type, FALLBACK_BEHAVIOR)


_AXIS_VECTORS = {
    "+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
}


def front_axis_vector(axis: str):
    """Parse a ``"+Z"`` / ``"-Y"`` world-axis string into a unit vector, or ``None`` when
    empty/unknown (front-preserve disabled)."""
    if not axis:
        return None
    v = _AXIS_VECTORS.get(axis.strip().lower())
    return np.asarray(v, float) if v is not None else None


def compute_front_preserve_edges(mesh: MeshGraph, assignment, front_axis, *,
                                 threshold: float = 0.35, max_dihedral: float = 45.0,
                                 mandatory: set[int] | None = None) -> set[int]:
    """Front-facing low-angle interior edges of every ``front_preserve`` chapter (work plan
    §3). These are auto-added to the preserve (forbidden) set so no seam crosses the visible
    face/robe FRONT; seams are pushed to the back/under instead.

    An edge qualifies when: both faces are in a front_preserve chapter; the two faces' mean
    normal faces ``front_axis`` (dot > ``threshold``); the dihedral is LOW (< ``max_dihedral``
    — a smooth surface, not a crease the artist wants cut); and it is NOT a mandatory ≥90°
    fold (a hard crease must stay cuttable — mandatory always wins)."""
    fa = front_axis_vector(front_axis) if isinstance(front_axis, str) else front_axis
    if fa is None:
        return set()
    mandatory = mandatory or set()
    fp_faces: set[int] = set()
    for ch in assignment.chapters:
        if ch.behavior == "front_preserve":
            fp_faces.update(ch.face_ids)
    if not fp_faces:
        return set()
    protected: set[int] = set()
    for e in mesh.edges:
        if len(e.face_ids) != 2 or e.id in mandatory:
            continue
        a, b = e.face_ids
        if a not in fp_faces or b not in fp_faces:
            continue
        if e.dihedral_angle >= max_dihedral:
            continue                                  # a real crease — leave it cuttable
        n = np.asarray(mesh.faces[a].normal, float) + np.asarray(mesh.faces[b].normal, float)
        nn = np.linalg.norm(n)
        if nn < 1e-9:
            continue
        if float(np.dot(n / nn, fa)) > threshold:     # faces the front
            protected.add(e.id)
    return protected


# --- spec data model --------------------------------------------------------------------

@dataclass
class GuidedChapter:
    """One UV chapter the artist/agent decided on (plan §chapter_spec). ``source_part_ids``
    are A1 part ids (hand-filled in v1; auto-mapping is follow-up). ``type`` selects the
    seam policy; ``seam_policy`` is a free-text annotation echoed into the report."""

    name: str
    source_part_ids: list[int] = field(default_factory=list)
    type: str = "auto"
    seam_policy: str = ""
    # Optional face SELECTOR (work plan §4): carve a SUBSET of the source parts by a normal-axis
    # rule, e.g. {"normal_axis": "-Z", "threshold": 0.35} → only the back-facing faces. Lets two
    # chapters split one part (front robe vs back cloak). None = whole part(s).
    selector: dict | None = None
    # Optional EXPLICIT face-set selection (interactive front-end): absolute mesh face ids this
    # chapter owns regardless of the part decomposition. Carved into their own part by
    # :func:`apply_chapter_face_selection` BEFORE assignment so an artist face-set survives to the
    # part-based backend. Empty = use ``source_part_ids`` / ``selector`` only.
    source_face_ids: list[int] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "GuidedChapter":
        sel = d.get("selector")
        return cls(
            name=str(d.get("name", "")),
            source_part_ids=[int(p) for p in d.get("source_part_ids", [])],
            type=str(d.get("type", "auto")),
            seam_policy=str(d.get("seam_policy", "")),
            selector=dict(sel) if isinstance(sel, dict) else None,
            source_face_ids=[int(f) for f in d.get("source_face_ids", [])],
        )

    def to_dict(self) -> dict:
        return {"name": self.name, "source_part_ids": list(self.source_part_ids),
                "type": self.type, "seam_policy": self.seam_policy, "selector": self.selector,
                "source_face_ids": list(self.source_face_ids)}


@dataclass
class GuidedUVSpec:
    """A complete guided-UV judgement. ``forbidden_edges`` are mesh edge ids the artist
    wants PRESERVED (never a seam, e.g. the smooth robe edge 3054); a forbidden edge that is
    also a mandatory ≥ ``mandatory_fold_angle`` fold is a reported CONFLICT (the fold wins —
    a hard crease must stay a seam, plan §step6)."""

    version: int = 1
    object: str = ""
    forbidden_edges: list[int] = field(default_factory=list)
    mandatory_fold_angle: float = DEFAULT_FOLD_ANGLE
    # How to derive the part table the chapters reference (see SEGMENTATION_MODES). Default
    # "auto": coarse (fast connected-component shells) when no chapter fills source_part_ids,
    # else the deep artist watershed so hand-filled part ids resolve.
    segmentation_mode: str = "auto"
    # Front-preserve (work plan §3): a world axis ("+Z"/"-Y"/…) the model FRONT faces. When
    # set, every front_preserve chapter's front-facing low-angle interior edge is auto-added to
    # the preserve (forbidden) set so no seam crosses the visible face/robe front. Empty ""
    # disables it (the chapter is then a keep-intact label only, honestly reported).
    front_preserve_axis: str = ""
    front_preserve_max_dihedral: float = 45.0     # only LOW-angle (smooth) front edges
    front_preserve_threshold: float = 0.35        # how strongly the edge must face front
    # Canonical artist intents the worker DECLARED for this asset (work plan §7). A declared
    # intent with no chapter counts as ``missing`` → unmet. Empty = only chapters present are
    # judged (a minimal spec is not penalised for intents the worker never raised).
    expected_intents: list[str] = field(default_factory=list)
    chapters: list[GuidedChapter] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "GuidedUVSpec":
        return cls(
            version=int(d.get("version", 1)),
            object=str(d.get("object", "")),
            forbidden_edges=[int(e) for e in d.get("forbidden_edges", [])],
            mandatory_fold_angle=float(d.get("mandatory_fold_angle", DEFAULT_FOLD_ANGLE)),
            segmentation_mode=str(d.get("segmentation_mode", "auto")),
            front_preserve_axis=str(d.get("front_preserve_axis", "")),
            front_preserve_max_dihedral=float(d.get("front_preserve_max_dihedral", 45.0)),
            front_preserve_threshold=float(d.get("front_preserve_threshold", 0.35)),
            expected_intents=[str(x) for x in d.get("expected_intents", [])],
            chapters=[GuidedChapter.from_dict(c) for c in d.get("chapters", [])],
        )

    @classmethod
    def from_json(cls, text: str) -> "GuidedUVSpec":
        return cls.from_dict(json.loads(text))

    @classmethod
    def coerce(cls, spec) -> "GuidedUVSpec":
        """Accept a :class:`GuidedUVSpec`, a dict, or a JSON string."""
        if isinstance(spec, GuidedUVSpec):
            return spec
        if isinstance(spec, str):
            return cls.from_json(spec)
        return cls.from_dict(spec)

    def to_dict(self) -> dict:
        return {"version": self.version, "object": self.object,
                "forbidden_edges": list(self.forbidden_edges),
                "mandatory_fold_angle": self.mandatory_fold_angle,
                "segmentation_mode": self.segmentation_mode,
                "front_preserve_axis": self.front_preserve_axis,
                "front_preserve_max_dihedral": self.front_preserve_max_dihedral,
                "front_preserve_threshold": self.front_preserve_threshold,
                "expected_intents": list(self.expected_intents),
                "chapters": [c.to_dict() for c in self.chapters]}

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# --- chapter assignment -----------------------------------------------------------------

@dataclass
class ResolvedChapter:
    """A spec or fallback chapter resolved against the actual A1 parts."""

    index: int
    name: str
    type: str                 # effective chapter type (declared, or class-derived fallback)
    behavior: str             # deterministic seam behaviour
    seam_policy: str
    part_ids: list[int]
    face_ids: list[int]
    source: str               # "spec" | "fallback"
    note: str = ""

    def to_dict(self) -> dict:
        return {"index": self.index, "name": self.name, "type": self.type,
                "behavior": self.behavior, "seam_policy": self.seam_policy,
                "part_ids": sorted(self.part_ids), "face_count": len(self.face_ids),
                "source": self.source, "note": self.note}


@dataclass
class ChapterAssignment:
    chapters: list[ResolvedChapter]
    part_chapter: dict[int, int]              # part_id → chapter index
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"chapter_count": len(self.chapters),
                "chapters": [c.to_dict() for c in self.chapters],
                "part_chapter": {int(k): int(v) for k, v in self.part_chapter.items()},
                "warnings": self.warnings}


def assign_chapters(seg, descriptors, classes, spec: GuidedUVSpec) -> ChapterAssignment:
    """Map every A1 part to a chapter (plan §step3). Spec chapters claim their
    ``source_part_ids`` (first chapter wins a contested part); every part the spec does not
    cover becomes its OWN class-based FALLBACK chapter, so generation always completes even
    on a partial spec (plan §test8)."""
    class_by = {c.part_id: c.type for c in classes}
    part_faces = {p.part_id: list(p.face_ids) for p in seg.parts}
    valid = set(part_faces)

    part_chapter: dict[int, int] = {}
    chapters: list[ResolvedChapter] = []
    warnings: list[str] = []

    for ch in spec.chapters:
        claimed: list[int] = []
        for pid in ch.source_part_ids:
            if pid not in valid:
                warnings.append(f"chapter '{ch.name}': part {pid} does not exist (skipped)")
                continue
            if pid in part_chapter:
                warnings.append(f"chapter '{ch.name}': part {pid} already claimed by "
                                f"'{chapters[part_chapter[pid]].name}' (skipped)")
                continue
            claimed.append(pid)
        idx = len(chapters)
        faces = [f for pid in claimed for f in part_faces[pid]]
        chapters.append(ResolvedChapter(
            index=idx, name=ch.name or f"chapter_{idx}", type=ch.type,
            behavior=chapter_behavior(ch.type), seam_policy=ch.seam_policy,
            part_ids=claimed, face_ids=faces, source="spec",
            note="" if claimed else "no resolved parts (empty source_part_ids)"))
        for pid in claimed:
            part_chapter[pid] = idx

    # Every uncovered part → its own fallback chapter (class-derived type).
    for p in seg.parts:
        if p.part_id in part_chapter:
            continue
        ctype = _CLASS_TO_CHAPTER.get(class_by.get(p.part_id, "unknown"), "auto")
        idx = len(chapters)
        chapters.append(ResolvedChapter(
            index=idx, name=f"fallback_{ctype}_{p.part_id}", type=ctype,
            behavior=chapter_behavior(ctype), seam_policy="class_fallback",
            part_ids=[p.part_id], face_ids=list(part_faces[p.part_id]),
            source="fallback", note=f"part not in spec → class '{ctype}' fallback"))
        part_chapter[p.part_id] = idx

    return ChapterAssignment(chapters=chapters, part_chapter=part_chapter, warnings=warnings)


# --- coarse (fast-path) segmentation ----------------------------------------------------

def coarse_segment_parts(mesh: MeshGraph, *, by_material: bool = False) -> PartSegmentation:
    """Shallow part decomposition for the guided fast path (plan: don't run the deep auto
    watershed when the spec doesn't reference its part ids). Parts = connected components
    over face adjacency (optionally further split by ``material_index``). O(F+E), no Dijkstra,
    no merge, no per-part descriptors — instant even on large assets.

    The deep :func:`segment_parts` over-segments a decimated mesh into hundreds of watershed
    parts; for a guided run with empty ``source_part_ids`` that work is wasted. Connected
    components give the 10–30 coarse 'chapters' the artist actually reasons about as a fast,
    honest starting point (refine with hand-filled ``source_part_ids`` + ``mode="full"``)."""
    comps = flood_charts(mesh, set())
    if by_material:
        split: list[list[int]] = []
        for comp in comps:
            by_mat: dict[int, list[int]] = {}
            for f in comp:
                by_mat.setdefault(mesh.faces[f].material_index, []).append(f)
            split.extend(by_mat.values())
        comps = split

    face_part: dict[int, int] = {}
    parts: list[Part] = []
    for pid, comp in enumerate(comps):
        faces = sorted(comp)
        for f in faces:
            face_part[f] = pid
        parts.append(Part(part_id=pid, face_ids=faces, seed_face=faces[0],
                          confidence=1.0, neighbors=set(), boundary_edges=set()))
    history = [{"stage": "coarse", "method": "material_components" if by_material
               else "connected_components", "parts": len(parts), "faces": mesh.face_count}]
    return PartSegmentation(mesh, face_part, parts, history)


def _seg_from_face_part(mesh: MeshGraph, face_part: dict[int, int], history) -> PartSegmentation:
    """Build a :class:`PartSegmentation` from a ``face → part`` map. Part ids are PRESERVED
    (sparse is fine) so unchanged chapters' ``source_part_ids`` stay valid after carving."""
    groups: dict[int, list[int]] = {}
    for f, p in face_part.items():
        groups.setdefault(p, []).append(f)
    parts = []
    for p in sorted(groups):
        faces = sorted(groups[p])
        parts.append(Part(part_id=p, face_ids=faces, seed_face=faces[0],
                          confidence=1.0, neighbors=set(), boundary_edges=set()))
    return PartSegmentation(mesh, dict(face_part), parts, history)


def apply_chapter_selectors(mesh: MeshGraph, seg: PartSegmentation, spec: GuidedUVSpec):
    """Carve selector chapters into their own parts (work plan §4). For each part a SELECTOR
    chapter references, split that part's faces by the selector's normal axis into a new part
    per chapter (best-matching axis wins; unmatched faces stay in the original part → fallback).
    Returns ``(seg2, spec2)`` with the spec's ``source_part_ids`` rewritten to the carved ids
    and selectors cleared, so the rest of the (part-based) pipeline is unchanged. A spec with
    no selectors is returned untouched.

    This lets one part become several chapters — e.g. ``upper_front_robe`` (+Z) and
    ``back_cloak`` (−Z) carved from the same torso shell."""
    sel_by_part: dict[int, list[tuple[int, dict]]] = {}
    for ci, ch in enumerate(spec.chapters):
        if ch.selector:
            for pid in ch.source_part_ids:
                sel_by_part.setdefault(pid, []).append((ci, ch.selector))
    if not sel_by_part:
        return seg, spec

    face_part = dict(seg.face_part)
    next_id = (max(face_part.values()) + 1) if face_part else 0
    new_src = {ci: list(ch.source_part_ids) for ci, ch in enumerate(spec.chapters)}
    log: list[dict] = []
    for pid, sels in sel_by_part.items():
        faces = [f for f, p in face_part.items() if p == pid]
        groups: dict[int, list[int]] = {ci: [] for ci, _ in sels}
        for f in faces:
            n = np.asarray(mesh.faces[f].normal, float)
            best, best_dot = None, -2.0
            for ci, sd in sels:
                ax = front_axis_vector(sd.get("normal_axis", ""))
                if ax is None:
                    continue
                d = float(np.dot(n, ax))
                if d > float(sd.get("threshold", 0.35)) and d > best_dot:
                    best, best_dot = ci, d
            if best is not None:
                groups[best].append(f)
        for ci, fs in groups.items():
            if not fs:
                continue
            for f in fs:
                face_part[f] = next_id
            new_src[ci] = [x for x in new_src[ci] if x != pid] + [next_id]
            log.append({"op": "selector_carve", "chapter": ci, "from_part": pid,
                        "new_part": next_id, "faces": len(fs)})
            next_id += 1

    seg2 = _seg_from_face_part(mesh, face_part, seg.history + log)
    # Rewrite the spec: source_part_ids → carved ids; clear the selector (already applied).
    new_chapters = []
    for ci, ch in enumerate(spec.chapters):
        new_chapters.append(GuidedChapter(name=ch.name, source_part_ids=sorted(set(new_src[ci])),
                                          type=ch.type, seam_policy=ch.seam_policy, selector=None,
                                          source_face_ids=list(ch.source_face_ids)))
    spec2 = GuidedUVSpec(
        version=spec.version, object=spec.object, forbidden_edges=list(spec.forbidden_edges),
        mandatory_fold_angle=spec.mandatory_fold_angle, segmentation_mode=spec.segmentation_mode,
        front_preserve_axis=spec.front_preserve_axis,
        front_preserve_max_dihedral=spec.front_preserve_max_dihedral,
        front_preserve_threshold=spec.front_preserve_threshold,
        expected_intents=list(spec.expected_intents), chapters=new_chapters)
    return seg2, spec2


def apply_chapter_face_selection(mesh: MeshGraph, seg: PartSegmentation, spec: GuidedUVSpec):
    """Carve chapters' explicit ``source_face_ids`` into their own parts so an artist FACE-SET
    selection survives to the part-based backend (interactive front-end). For each chapter that
    names absolute mesh face ids, those faces are pulled into a fresh part and the chapter's
    ``source_part_ids`` gains that part id (face ids consumed). Returns ``(seg2, spec2)`` rewritten
    so the rest of the (part-based) pipeline is unchanged; a spec with no ``source_face_ids`` is
    returned untouched.

    Contested faces go to the FIRST (lowest-index) chapter that claims them — mirroring
    :func:`assign_chapters`' first-wins part semantics. Invalid face ids are dropped (the chapter
    then resolves no part → reported as an unmet interactive chapter, never silently 'applied')."""
    face_sel = {ci: ch.source_face_ids for ci, ch in enumerate(spec.chapters)
                if ch.source_face_ids}
    if not face_sel:
        return seg, spec

    face_part = dict(seg.face_part)
    next_id = (max(face_part.values()) + 1) if face_part else 0
    new_src = {ci: list(ch.source_part_ids) for ci, ch in enumerate(spec.chapters)}
    claimed: set[int] = set()
    log: list[dict] = []
    for ci, faces in face_sel.items():
        valid = [f for f in faces if f in face_part and f not in claimed]
        if not valid:
            continue
        for f in valid:
            face_part[f] = next_id
            claimed.add(f)
        new_src[ci] = list(new_src[ci]) + [next_id]
        log.append({"op": "face_selection_carve", "chapter": ci, "new_part": next_id,
                    "faces": len(valid)})
        next_id += 1

    seg2 = _seg_from_face_part(mesh, face_part, seg.history + log)
    new_chapters = [GuidedChapter(name=ch.name, source_part_ids=sorted(set(new_src[ci])),
                                  type=ch.type, seam_policy=ch.seam_policy, selector=ch.selector,
                                  source_face_ids=[])
                    for ci, ch in enumerate(spec.chapters)]
    spec2 = GuidedUVSpec(
        version=spec.version, object=spec.object, forbidden_edges=list(spec.forbidden_edges),
        mandatory_fold_angle=spec.mandatory_fold_angle, segmentation_mode=spec.segmentation_mode,
        front_preserve_axis=spec.front_preserve_axis,
        front_preserve_max_dihedral=spec.front_preserve_max_dihedral,
        front_preserve_threshold=spec.front_preserve_threshold,
        expected_intents=list(spec.expected_intents), chapters=new_chapters)
    return seg2, spec2


def resolve_segmentation_mode(spec: GuidedUVSpec, override: str | None = None) -> str:
    """Resolve the effective segmentation mode. ``override`` (the ``run_guided_uv`` arg) wins
    over ``spec.segmentation_mode``. ``auto`` → ``coarse`` when no chapter fills
    ``source_part_ids`` (nothing references deep part ids), else ``full``. ``manual_parts`` is
    an alias for ``full`` (hand-filled ids need the deep table)."""
    mode = (override or spec.segmentation_mode or "auto").lower()
    if mode == "auto":
        has_manual = any(c.source_part_ids for c in spec.chapters)
        return "full" if has_manual else "coarse"
    if mode == "manual_parts":
        return "full"
    return mode if mode in ("coarse", "full") else "coarse"


def _segment_for_guided(mesh: MeshGraph, mode: str):
    """Build (seg, descriptors, classes) for the resolved ``mode``. Coarse skips the
    Dijkstra-heavy ``segment_parts``/``describe_parts`` entirely; every coarse part gets the
    ``coarse`` class (→ keep-intact chapter, diskify only)."""
    if mode == "coarse":
        seg = coarse_segment_parts(mesh)
        classes = [PartClass(p.part_id, "coarse", 1.0, "coarse connected-component")
                   for p in seg.parts]
        return seg, [], classes
    seg = segment_parts(mesh)
    seg = split_branched_parts(mesh, seg)
    descriptors = describe_parts(mesh, seg)
    classes = classify_parts(descriptors, {p.part_id: p.neighbors for p in seg.parts})
    return seg, descriptors, classes


def _rederive_classes(mesh: MeshGraph, seg: PartSegmentation, mode: str):
    """(descriptors, classes) for a part set after a carve (face-selection / selector). Coarse
    stays descriptor-free; full re-runs A2/A3 so the new parts classify."""
    if mode == "coarse":
        return [], [PartClass(p.part_id, "coarse", 1.0, "coarse") for p in seg.parts]
    descriptors = describe_parts(mesh, seg)
    return descriptors, classify_parts(descriptors, {p.part_id: p.neighbors for p in seg.parts})


def build_guided_assignment(mesh: MeshGraph, spec: GuidedUVSpec, *,
                            segmentation_mode: str | None = None):
    """Segment + carve (explicit face-sets, then normal-axis selectors) + assign chapters →
    ``(seg, descriptors, classes, spec, assignment)``. The single PURE preparation shared by
    :func:`run_guided_uv` and the headless ``interactive_cli verify`` so a ``source_face_ids`` /
    ``selector`` chapter resolves IDENTICALLY everywhere (no Blender). ``spec`` is returned
    rewritten (face/selector carves consumed → ``source_part_ids``)."""
    mode = resolve_segmentation_mode(spec, segmentation_mode)
    seg, descriptors, classes = _segment_for_guided(mesh, mode)
    # Explicit face-set selection first (absolute ids), then normal-axis selectors on what's left.
    seg2, spec = apply_chapter_face_selection(mesh, seg, spec)
    if seg2 is not seg:
        seg = seg2
        descriptors, classes = _rederive_classes(mesh, seg, mode)
    seg3, spec = apply_chapter_selectors(mesh, seg, spec)
    if seg3 is not seg:
        seg = seg3
        descriptors, classes = _rederive_classes(mesh, seg, mode)
    assignment = assign_chapters(seg, descriptors, classes, spec)
    return seg, descriptors, classes, spec, assignment


# --- seam construction (pure) -----------------------------------------------------------

@dataclass
class GuidedSeamResult:
    seams: set[int]
    seam_origin: dict[int, str]               # edge id → seam type tag
    chart_to_chapter: dict[int, int]          # flooded chart id → chapter index
    chapter_charts: dict[int, list[int]]      # chapter index → its chart ids
    forbidden_stripped: list[int]             # non-mandatory forbidden edges removed
    forbidden_conflicts: list[int]            # forbidden edges that are mandatory folds (kept)
    cap_exceeded: bool
    # Forbidden-vs-disk incompatibility (plan §step6): a non-mandatory forbidden edge that
    # diskify NEEDED (re-added) — stripping it can leave a non-disk chart. ``nondisk_charts``
    # are the chart ids still non-disk after the final strip (honest, not silently "fixed").
    forbidden_disk_conflicts: list[int] = field(default_factory=list)
    nondisk_charts: list[int] = field(default_factory=list)
    # Chapter indices whose cylinder template ACTUALLY fired (added template seams). Distinct
    # from "chapters that REQUESTED a cylinder policy" — the template reverts on geometry that
    # is not a clean tube, so this is what was truly REFLECTED in the seams (review item 1/2).
    template_chapters: list[int] = field(default_factory=list)
    # Front-facing low-angle edges auto-preserved for front_preserve chapters (work plan §3).
    front_preserve_edges: list[int] = field(default_factory=list)
    front_preserve_axis: str = ""
    # Front candidates that could NOT be preserved (load-bearing diskify cuts) — honest.
    front_preserve_disk_conflicts: list[int] = field(default_factory=list)
    # Front edges a HARD-gate repair (distortion/overlap) had to cut anyway (hard gate > soft
    # preserve) — set by run_guided_uv after the repair loop.
    front_preserve_relaxed: list[int] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)

    def seam_type_counts(self) -> dict:
        out: dict[str, int] = {}
        for t in self.seam_origin.values():
            out[t] = out.get(t, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {"seam_count": len(self.seams), "seam_type_counts": self.seam_type_counts(),
                "chart_count": len(self.chart_to_chapter),
                "chart_to_chapter": {int(k): int(v) for k, v in self.chart_to_chapter.items()},
                "chapter_charts": {int(k): v for k, v in self.chapter_charts.items()},
                "forbidden_stripped": sorted(self.forbidden_stripped),
                "forbidden_conflicts": sorted(self.forbidden_conflicts),
                "forbidden_disk_conflicts": sorted(self.forbidden_disk_conflicts),
                "nondisk_charts": sorted(self.nondisk_charts),
                "template_chapters": sorted(self.template_chapters),
                "front_preserve_edges": sorted(self.front_preserve_edges),
                "front_preserve_axis": self.front_preserve_axis,
                "front_preserve_disk_conflicts": sorted(self.front_preserve_disk_conflicts),
                "cap_exceeded": self.cap_exceeded, "log": self.log}


def _consolidate_same_chapter_charts(mesh: MeshGraph, seams: set[int], face_part, part_chapter,
                                     mandatory: set[int], *, protect, fold_angle: float,
                                     accept, max_merges: int = 120) -> int:
    """Merge adjacent charts that belong to the SAME chapter by dissolving their whole shared
    NON-mandatory boundary, keeping a merge only when ``accept()`` (a gate re-measure) holds
    (work plan §C). Largest shared boundary first (biggest island reduction). Never removes a
    mandatory ≥ ``fold_angle`` fold or a ``protect`` (hard-preserve) edge; removing a seam can
    only MERGE, never create a front seam. Returns the number of merges applied; mutates
    ``seams`` in place."""
    protect = set(protect)
    merged = 0
    for _ in range(max_merges):
        charts = flood_charts(mesh, seams)
        fc = {f: i for i, fs in enumerate(charts) for f in fs}
        border: dict[tuple[int, int], list[int]] = {}
        for e in seams:
            ed = mesh.edges[e]
            if len(ed.face_ids) != 2 or e in mandatory or e in protect:
                continue
            if ed.dihedral_angle >= fold_angle:
                continue
            a, b = ed.face_ids
            ca, cb = fc.get(a), fc.get(b)
            if ca is None or cb is None or ca == cb:
                continue
            # only merge WITHIN one chapter (never dissolve a real chapter boundary).
            if part_chapter.get(face_part[a]) != part_chapter.get(face_part[b]):
                continue
            border.setdefault((min(ca, cb), max(ca, cb)), []).append(e)
        if not border:
            break
        applied = False
        for _pair, edges in sorted(border.items(), key=lambda kv: -len(kv[1])):
            for e in edges:
                seams.discard(e)
            if accept():
                merged += 1
                applied = True
                break                       # re-flood: chart ids changed
            for e in edges:
                seams.add(e)                # revert — merge worsened the gate
        if not applied:
            break
    return merged


def map_charts_to_chapters(mesh: MeshGraph, seams: set[int], face_part,
                           part_chapter) -> tuple[list, dict, dict]:
    """Flood ``seams`` and map each chart → chapter index (a chart is wholly inside one part
    ⇒ one chapter). MUST be recomputed from the FINAL seams after any repair/prune so the
    report/overlay never label charts from a stale seam set. Returns ``(charts,
    chart_to_chapter, chapter_charts)``."""
    charts = flood_charts(mesh, seams)
    chart_to_chapter: dict[int, int] = {}
    chapter_charts: dict[int, list[int]] = {}
    for cid, fs in enumerate(charts):
        idx = part_chapter.get(face_part[fs[0]], -1)
        chart_to_chapter[cid] = idx
        chapter_charts.setdefault(idx, []).append(cid)
    return charts, chart_to_chapter, chapter_charts


def _interpart_edges_by_chapter_pair(mesh: MeshGraph, face_part, part_chapter):
    """Inter-part interior edges grouped by whether the two parts share a chapter."""
    same: list[int] = []
    for e in mesh.edges:
        if e.is_boundary or e.is_non_manifold or len(e.face_ids) != 2:
            continue
        pa, pb = face_part.get(e.face_ids[0]), face_part.get(e.face_ids[1])
        if pa is None or pb is None or pa == pb:
            continue
        if part_chapter.get(pa) == part_chapter.get(pb):
            same.append(e.id)
    return same


@quiet_fp
def build_guided_seams(mesh: MeshGraph, seg, descriptors, classes, spec: GuidedUVSpec,
                       assignment: ChapterAssignment, *, back_dir=None,
                       cone_limit: float = DEFAULT_CONE_LIMIT,
                       max_charts: int = DEFAULT_MAX_CHARTS,
                       max_diskify_rounds: int | None = None) -> GuidedSeamResult:
    """Build the chapter-driven seam set (plan §step3–§step6, §step7). Pure / Blender-free.

    Floor = part boundaries; dissolve same-chapter internal boundaries; strip non-mandatory
    forbidden edges (never a seam); re-assert the mandatory ≥ fold-angle union (R2);
    apply each chapter's seam policy (cylinder template / keep-intact / organic split);
    diskify every chart (a non-disk self-folds in SLIM). Returns the seam set tagged by
    origin and the chart→chapter map."""
    fold_angle = spec.mandatory_fold_angle
    face_part = seg.face_part
    desc_by = {d.part_id: d for d in descriptors}
    part_chapter = assignment.part_chapter
    forbidden = set(spec.forbidden_edges)
    mandatory = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    log: list[dict] = []

    # 1. Floor: every part boundary is a seam.
    seams = part_seam_edges(mesh, face_part)

    # 2. Dissolve internal (non-mandatory) boundaries between parts of the SAME chapter, so a
    #    multi-part chapter is one continuous region (plan §step3). Mandatory folds stay.
    intra = [e for e in _interpart_edges_by_chapter_pair(mesh, face_part, part_chapter)
             if e not in mandatory]
    seams.difference_update(intra)
    log.append({"op": "dissolve_intra_chapter_boundaries", "edges": len(intra)})

    # 3. Forbidden / no-cut: a non-mandatory forbidden edge must NEVER be a seam (plan
    #    §step6). A forbidden edge that IS a mandatory fold is a CONFLICT — the fold wins.
    #    HARD forbidden (the user's explicit preserve, e.g. 3054) is stripped absolutely.
    #    Front-preserve (work plan §3) is a SOFT preserve: applied disk-safely AFTER diskify so
    #    it can never break disk topology / the hard gate.
    fp_candidates = compute_front_preserve_edges(
        mesh, assignment, spec.front_preserve_axis, threshold=spec.front_preserve_threshold,
        max_dihedral=spec.front_preserve_max_dihedral, mandatory=mandatory)
    if fp_candidates:
        log.append({"op": "front_preserve_candidates", "axis": spec.front_preserve_axis,
                    "edges": len(fp_candidates)})
    conflicts = sorted(e for e in forbidden if e in mandatory)
    nonmand_forbidden = forbidden - set(conflicts)              # HARD preserve only
    pre_strip = sorted(e for e in nonmand_forbidden if e in seams)
    seams.difference_update(nonmand_forbidden)

    # 4. Re-assert the mandatory union (R2) — unconditional, belt-and-suspenders.
    seams |= mandatory

    # 5. Per-chapter seam policy.
    bd = None if back_dir is None else np.asarray(back_dir, float)
    chapter_template_seams: set[int] = set()
    split_faces: set[int] = set()           # faces eligible for the organic cone-split
    part_faces_map = seg.part_faces()
    template_chapters: list[int] = []      # chapters whose tube template ACTUALLY fired
    for ch in assignment.chapters:
        if ch.behavior == "cylinder":
            # ESCALATION (work plan §2): try the clean lengthwise template, then the
            # multi-loop opener (handles a 3+ boundary-loop decimated sleeve), then a
            # branch-split (treat it as a fork). Whatever first yields a valid strip wins.
            fired = False
            for pid in ch.part_ids:
                faces = list(part_faces_map.get(pid, []))
                if not faces:
                    continue
                # COARSE has no precomputed descriptors → derive one (axis + centroid) so the
                # tube opener still works (review: the sleeve was skipped before for lack of a
                # descriptor).
                d = desc_by.get(pid) or _subpart_descriptor(mesh, faces)
                opened = _open_tube_strip(mesh, faces, d, seams, bd, log, ch.index, pid)
                if not opened:
                    opened = _cylinder_group_seams(mesh, faces, seams, bd, log, ch.index, pid)
                if opened:
                    seams.update(opened)
                    chapter_template_seams.update(opened)
                    fired = True
            if fired:
                template_chapters.append(ch.index)
        elif ch.behavior == "cylinder_group":
            # A branched tube (staff + prongs): split each part at its fork into shaft +
            # prongs FIRST, then open each sub-tube into a strip (review item 3).
            fired = False
            for pid in ch.part_ids:
                opened = _cylinder_group_seams(mesh, list(part_faces_map.get(pid, [])),
                                               seams, bd, log, ch.index, pid)
                if opened:
                    seams.update(opened)
                    chapter_template_seams.update(opened)
                    fired = True
            if fired:
                template_chapters.append(ch.index)
        elif ch.behavior == "organic_split":
            split_faces.update(ch.face_ids)
        elif ch.type == "face_front_preserve" and spec.front_preserve_axis:
            # FACE REROUTE (work plan §1): pre-open a multi-loop face chart on the BACK so the
            # generic diskify does not have to cut across the visible front. The opener biases
            # its slit toward ``-front_axis`` (the back of the head), reducing front-smooth seams.
            back = -front_axis_vector(spec.front_preserve_axis)
            for pid in ch.part_ids:
                faces = list(part_faces_map.get(pid, []))
                if not faces:
                    continue
                slits = open_multiloop_tube(mesh, faces, seams, back_dir=back,
                                            desc=_subpart_descriptor(mesh, faces))
                if slits:
                    seams.update(slits)
                    chapter_template_seams.update(slits)
                    log.append({"op": "face_back_open", "chapter": ch.index, "part": pid,
                                "edges": len(slits)})
        # keep_intact / front_preserve add NO other internal seams (front_preserve relies on the
        # forbidden set + the back-biased welded-fold repair to avoid visible seams).

    # 6. Diskify every chart + cone-split only the organic-split chapters (reused artist
    #    machinery). Diskify is unconditional (a non-disk chart self-folds in SLIM), but
    #    ``max_diskify_rounds`` bounds the work — leftover non-disks are audited below.
    cap_exceeded = _diskify_and_split(mesh, seams, split_faces, cone_limit=cone_limit,
                                      max_charts=max_charts, log=log,
                                      max_diskify_rounds=max_diskify_rounds)
    fallback_seams = {e for e in seams
                      if e not in chapter_template_seams and e not in mandatory
                      and not _is_part_boundary(mesh, e, face_part)}

    # 7. Re-strip forbidden (a diskify/cone-split could have re-added one), re-diskify, then
    #    strip once more as the final guarantee. ``split_chart`` is not forbidden-aware, so a
    #    forbidden edge that diskify NEEDS will be re-added; we record those as a
    #    forbidden/disk CONFLICT and audit the still-non-disk charts honestly rather than
    #    silently shipping a non-disk chart (plan §step6: preserve vs disk can be incompatible).
    post_strip = sorted(e for e in nonmand_forbidden if e in seams)
    seams.difference_update(nonmand_forbidden)
    if post_strip:
        _diskify_and_split(mesh, seams, set(), cone_limit=cone_limit,
                           max_charts=max_charts, log=log,
                           max_diskify_rounds=max_diskify_rounds)
        seams.difference_update(nonmand_forbidden)   # final guarantee — never ship forbidden

    # 7b. DISK-SAFE front preservation (work plan §3 주의: relax over-protection). A front
    #     candidate is preserved iff it is not a load-bearing diskify cut — removing it from the
    #     seam set must keep its chart a disk; the load-bearing ones are reverted and reported
    #     as ``front_preserve_disk_conflicts`` so front protection NEVER breaks the hard gate.
    front_preserve_edges, front_disk_conflicts = _preserve_front_edges_disk_safe(
        mesh, seams, fp_candidates)
    if fp_candidates:
        log.append({"op": "front_preserve_applied", "preserved": len(front_preserve_edges),
                    "disk_conflicts": len(front_disk_conflicts)})

    # Final disk audit: any chart still non-disk after the forbidden strip is reported (it
    # would self-fold in SLIM, surfaced honestly by the overlap gate downstream). A genuine
    # forbidden-vs-disk CONFLICT is a non-mandatory forbidden edge whose own chart could not
    # be diskified without it — i.e. an edge interior to a still-non-disk chart. (Diskify may
    # transiently re-add a forbidden edge that turns out NOT to be load-bearing; those are
    # NOT conflicts and are not reported.)
    charts, chart_to_chapter, chapter_charts = map_charts_to_chapters(
        mesh, seams, face_part, part_chapter)
    nondisk = [cid for cid, fs in enumerate(charts) if not uv_is_disk(mesh, fs, seams)]
    nondisk_faces = {f for cid in nondisk for f in charts[cid]}
    disk_conflicts = {e for e in nonmand_forbidden
                      if len(mesh.edges[e].face_ids) == 2
                      and mesh.edges[e].face_ids[0] in nondisk_faces
                      and mesh.edges[e].face_ids[1] in nondisk_faces}
    if nondisk:
        log.append({"op": "nondisk_after_forbidden_strip", "charts": len(nondisk),
                    "forbidden_disk_conflicts": sorted(disk_conflicts)})

    seam_origin = _tag_seams(mesh, seams, mandatory, chapter_template_seams,
                             fallback_seams, forbidden, fold_angle)
    stripped = sorted(set(pre_strip) | set(post_strip))
    return GuidedSeamResult(
        seams=seams, seam_origin=seam_origin, chart_to_chapter=chart_to_chapter,
        chapter_charts=chapter_charts, forbidden_stripped=stripped,
        forbidden_conflicts=conflicts, cap_exceeded=cap_exceeded,
        forbidden_disk_conflicts=sorted(disk_conflicts), nondisk_charts=nondisk,
        template_chapters=sorted(set(template_chapters)),
        front_preserve_edges=sorted(front_preserve_edges),
        front_preserve_axis=spec.front_preserve_axis,
        front_preserve_disk_conflicts=sorted(front_disk_conflicts), log=log)


def _chart_of_face(mesh: MeshGraph, seams: set[int], seed: int) -> list[int]:
    """The flooded chart (face list) containing ``seed``, not crossing ``seams`` — a LOCAL
    flood so a per-edge disk check costs O(chart), not O(mesh)."""
    adjacency = mesh.face_adjacency()
    seen = {seed}
    stack = [seed]
    comp = [seed]
    while stack:
        f = stack.pop()
        for nb, eid in adjacency[f]:
            if nb not in seen and eid not in seams:
                seen.add(nb)
                comp.append(nb)
                stack.append(nb)
    return comp


def _preserve_front_edges_disk_safe(mesh: MeshGraph, seams: set[int],
                                    candidates) -> tuple[list[int], list[int]]:
    """Remove each front candidate from the cut set ONLY if the chart stays a disk (work plan
    §3 주의). A candidate already absent from ``seams`` is preserved for free; one in ``seams``
    is removed and reverted if that breaks its chart's disk topology. Returns
    ``(preserved, disk_conflicts)``; ``seams`` is mutated in place (preserved edges removed)."""
    preserved: list[int] = []
    conflicts: list[int] = []
    # flattest-first so the smoothest (most visible) front edges get preference.
    for e in sorted(candidates, key=lambda x: mesh.edges[x].dihedral_angle):
        if e not in seams:
            preserved.append(e)
            continue
        seams.discard(e)
        comp = _chart_of_face(mesh, seams, mesh.edges[e].face_ids[0])
        if uv_is_disk(mesh, comp, seams):
            preserved.append(e)
        else:
            seams.add(e)                    # load-bearing cut — must stay (reported)
            conflicts.append(e)
    return preserved, conflicts


def _subpart_descriptor(mesh: MeshGraph, faces):
    """A minimal descriptor shim (``principal_axes`` + ``centroid``) for an ad-hoc face group
    — enough for :func:`cylinder_template` (which only reads the long axis and centroid). Used
    for branch-split sub-tubes that have no precomputed :class:`PartDescriptor`."""
    from artist_uv_agent.descriptors import _pca, _part_vertex_coords

    pts = _part_vertex_coords(mesh, faces)
    axes, _extents, centroid = _pca(pts)
    return type("_SubDesc", (), {
        "principal_axes": [list(map(float, a)) for a in axes],
        "centroid": (float(centroid[0]), float(centroid[1]), float(centroid[2])),
    })()


def _open_tube_strip(mesh: MeshGraph, faces, desc, seams: set[int], back_dir, log: list,
                     chapter_idx: int, pid: int) -> list[int]:
    """Open one tube into a strip, escalating opener strength (work plan §2): the clean
    cap-separate + single lengthwise cut (:func:`cylinder_template`) first, then the
    multi-loop opener (:func:`open_multiloop_tube`) for a decimated 3+ boundary-loop tube.
    Returns the seam edges added (``[]`` if neither yields a valid disk)."""
    opened, caps = cylinder_template(mesh, faces, desc, seams, back_dir)
    if opened:
        log.append({"op": "cylinder_template", "chapter": chapter_idx, "part": pid,
                    "edges": len(opened), "caps": len(caps)})
        return list(opened)
    slits = open_multiloop_tube(mesh, faces, seams, back_dir, desc)
    if slits:
        log.append({"op": "open_multiloop_tube", "chapter": chapter_idx, "part": pid,
                    "edges": len(slits), "loops_connected": True})
        return slits
    return []


def _cylinder_group_seams(mesh: MeshGraph, faces, seams: set[int], back_dir, log: list,
                          chapter_idx: int, pid: int) -> list[int]:
    """Branched-tube template (review item 3): split ``faces`` at its fork into shaft + prongs,
    seam the sub-tubes apart, then open each as a strip (escalating opener). Falls back to a
    single-tube open when no clean fork is found. Returns the seam edges it added."""
    from artist_uv_agent.segmentation import (
        BRANCH_MIN_TINE, _branch_split, _face_centroids,
    )

    adjacency = mesh.face_adjacency()
    centroids = _face_centroids(mesh)
    # min_branches=2: accept any genuine fork (a 3-prong staff or a 2-way sleeve split).
    groups = _branch_split(mesh, faces, adjacency, centroids,
                           min_tine=BRANCH_MIN_TINE, min_branches=2)
    added: list[int] = []
    if len(groups) > 1:
        gid = {f: i for i, g in enumerate(groups) for f in g}
        for e in mesh.edges:
            if len(e.face_ids) == 2:
                a, b = e.face_ids
                if a in gid and b in gid and gid[a] != gid[b] and e.id not in seams:
                    added.append(e.id)
        seams.update(added)
        log.append({"op": "cylinder_group_branch_split", "chapter": chapter_idx,
                    "part": pid, "groups": len(groups), "separator_edges": len(added)})
    # Open each sub-tube (or the whole part if no fork) into a strip.
    for g in groups:
        opened = _open_tube_strip(mesh, g, _subpart_descriptor(mesh, g),
                                  seams | set(added), back_dir, log, chapter_idx, pid)
        if opened:
            seams.update(opened)
            added.extend(opened)
    return added


def _is_part_boundary(mesh: MeshGraph, edge_id: int, face_part) -> bool:
    e = mesh.edges[edge_id]
    if e.is_boundary or e.is_non_manifold:
        return True
    if len(e.face_ids) != 2:
        return False
    return face_part.get(e.face_ids[0]) != face_part.get(e.face_ids[1])


def _tag_seams(mesh, seams, mandatory, template_seams, fallback_seams, forbidden,
               fold_angle) -> dict:
    """Tag each shipped seam by origin (plan §step8 seam types). Precedence: a forbidden
    edge that survived (shouldn't) is flagged first; a ≥ fold-angle fold is always
    ``mandatory_90``; then chapter template, fallback split, else chapter boundary."""
    out: dict[int, str] = {}
    for e in seams:
        if e in forbidden:
            out[e] = "user_forbidden"
        elif mesh.edges[e].dihedral_angle >= fold_angle:
            out[e] = "mandatory_90"
        elif e in template_seams:
            out[e] = "chapter_template"
        elif e in fallback_seams:
            out[e] = "fallback_segmentation"
        else:
            out[e] = "chapter_boundary"
    return out


def build_guided_parts_json(seg, descriptors, classes, assignment: ChapterAssignment,
                            seam_result: GuidedSeamResult, *, chapter_charts=None) -> dict:
    """``guided_parts.json`` content (plan §step8): the auto part table + chapter
    assignment + the spec/fallback split. Pure. ``chapter_charts`` (final, post-repair) is
    used for the per-part 'chart_ids' column; falls back to the build-time mapping."""
    from artist_uv_agent.debug import part_debug_rows

    cc = chapter_charts if chapter_charts is not None else seam_result.chapter_charts
    # part_debug_rows wants a seam_result with .part_charts; supply the chapter charts via a
    # tiny shim so the per-part 'chart_ids' column reflects the chapter the part landed in.
    class _Shim:
        part_charts = {p.part_id: cc.get(
            assignment.part_chapter.get(p.part_id, -1), []) for p in seg.parts}

    return {
        "engine": "guided",
        "part_count": len(seg.parts),
        "parts": part_debug_rows(seg.parts, descriptors, classes, _Shim()),
        "assignment": assignment.to_dict(),
        "segmentation_history": seg.history,
    }


# --- Blender entry point ----------------------------------------------------------------

def _write_uvmap(obj, mesh: MeshGraph, uvmap, *, layer_name: str = "AI_UV") -> None:
    layer = obj.data.uv_layers.get(layer_name) or obj.data.uv_layers.active
    flat = np.asarray(uvmap.uv[: len(layer.data)], dtype=np.float64).reshape(-1)
    layer.data.foreach_set("uv", flat)
    obj.data.update()


@quiet_fp
def compute_guided_metrics(mesh: MeshGraph, uvmap, seams: set[int], *,
                           fold_angle: float = DEFAULT_FOLD_ANGLE) -> dict:
    """Flat chart-gate metric dict from a final ``uvmap`` (pure — no Blender). Reuses the
    chart engine's correctness + distortion metrics (plan §step5: 'overlap / raster overlap /
    distortion은 기존 chart gate metric 재사용'). The mandatory-fold audits are computed
    DIRECTLY at the spec ``fold_angle`` (not via the chart pipeline's hardcoded-90° helpers),
    so a spec with a non-90° fold angle is reported and gated correctly."""
    from chart_uv_agent.pipeline import _chart_metrics, _distortion_report, _shape_metrics
    from chart_uv_agent.segmentation import mandatory_seam_audit
    from uv_agent.blender.organic_unwrap import island_plan_from_seams
    from uv_agent.geometry.evaluation import (
        evaluate_uv_solution, mandatory_seam_uv_audit, raster_overlap_diagnosis,
        relative_small_island_ratio,
    )

    charts = flood_charts(mesh, seams)
    plan = island_plan_from_seams(mesh, seams)
    ev = evaluate_uv_solution(mesh, plan, uvmap)
    m = _chart_metrics(mesh, uvmap, ev)
    m["small_island_ratio"] = relative_small_island_ratio(mesh, plan, uvmap)
    face_chart = {f: cid for cid, fs in enumerate(charts) for f in fs}
    m["raster_overlap_ratio"] = raster_overlap_diagnosis(mesh, uvmap, face_chart)["raster_overlap_ratio"]
    mandatory = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    # R2 seam-SET audit at the spec fold angle.
    a = mandatory_seam_audit(mesh, set(seams), fold_angle=fold_angle)
    m["mandatory_90_edges"] = a["mandatory_90_edges"]
    m["mandatory_90_missing"] = a["mandatory_90_missing"]
    # R2 UV-LEVEL audit at the spec fold angle.
    ua = mandatory_seam_uv_audit(mesh, uvmap, fold_angle=fold_angle)
    m["mandatory_90_fold_edges"] = ua["mandatory_90_fold_edges"]
    m["mandatory_90_uv_unsplit"] = ua["mandatory_90_uv_unsplit"]
    m["uv_unsplit_edge_ids"] = ua["uv_unsplit_edge_ids"]
    m.update(_distortion_report(mesh, uvmap, charts))
    m.update(_shape_metrics(mesh, seams, mandatory))
    return m


def _resolve_repair_island_hard_cap(config, explicit: int | None = None,
                                    base_charts: int = 0) -> int:
    """True safety cap for HARD repair splits.

    ``config.island_count_max`` is an advisory/reporting target in the chart gate. Guided
    repair may start FAR above that target (selector splits + face protection on the statue
    start at ~166 charts) and still need to split the worst island a few more times to clear a
    distortion/overlap failure. The cap must therefore sit ABOVE the actual starting chart
    count, not just the advisory target — otherwise a hard failure can never be repaired.
    Caller may override explicitly."""
    if explicit is not None:
        return int(explicit)
    return max(int(config.island_count_max), 160, base_charts + 80)


def run_guided_uv(obj, mesh: MeshGraph, chapter_spec, *, config=None, margin: float = 0.005,
                  back_dir=None, cone_limit: float = DEFAULT_CONE_LIMIT,
                  max_charts: int = DEFAULT_MAX_CHARTS, max_repair_rounds: int = 24,
                  segmentation_mode: str | None = None,
                  max_diskify_rounds: int | None = None,
                  repair_island_hard_cap: int | None = None,
                  consolidate_islands: bool = True, max_merges: int = 120,
                  interactive_plan=None) -> dict:
    """Deterministically build UVs for ``obj`` from an artist/agent ``chapter_spec`` (plan
    §核심설계). Runs inside Blender. ``chapter_spec`` may be a :class:`GuidedUVSpec`, a dict,
    or a JSON string.

    Steps: part table (mode-resolved: COARSE connected-component shells for a spec with no
    hand-filled part ids, else the deep artist watershed) → chapter assignment →
    :func:`build_guided_seams` → SLIM unwrap + CONCAVE pack → a MINIMAL, structure-preserving
    hard-gate repair loop (welded mandatory fold > overlap/flip > worst-island distortion,
    one fix per round) → forbidden strip → auxiliary-seam prune → chart hard/quality gate +
    report. The forbidden set is preserved end-to-end (a non-mandatory forbidden edge never
    ships as a seam; a forbidden mandatory fold is reported as a conflict). ``segmentation_mode``
    ("auto"|"coarse"|"full"|"manual_parts") overrides ``spec.segmentation_mode``;
    ``max_diskify_rounds`` bounds the diskify work (leftover non-disk charts are reported, not
    silently shipped). ``repair_island_hard_cap`` is the true safety cap for HARD repair splits;
    it is intentionally separate from ``config.island_count_max``, which is an advisory target
    in the chart gate and must not block a mandatory/overlap/distortion fix. Returns the gate,
    metrics, seam set, chart→chapter map (recomputed from the FINAL seams), parts/report JSON,
    and ``shippable``; leaves ``obj`` holding the layout."""
    from chart_uv_agent.gate import ChartGateConfig, evaluate_chart_gate
    from chart_uv_agent.pipeline import (
        _classify_seams, _count_types, _prune_seams, _worst_stretch_chart,
    )
    from chart_uv_agent.segmentation import split_chart, split_welded_folds
    from chart_uv_agent.unwrap import flipped_faces, read_uvmap, unwrap_and_pack
    from uv_agent.geometry.evaluation import per_face_stretch, raster_overlap_diagnosis

    spec = GuidedUVSpec.coerce(chapter_spec)
    fold_angle = spec.mandatory_fold_angle
    # Honour a non-90° spec fold angle in the gate's reported limit too (the actual gate
    # check reads the metric, which compute_guided_metrics computes at ``fold_angle``).
    if config is None:
        config = (ChartGateConfig() if fold_angle == 90.0
                  else ChartGateConfig(fold_angle_mandatory=fold_angle))
    # ``config.island_count_max`` is reported as an advisory quality target by the generic
    # chart gate. Do not let that soft target prevent a hard repair (mandatory fold,
    # overlap, or worst-island distortion) from splitting one more island. Keep a separate
    # high safety cap to prevent runaway repair on pathological meshes.
    # Resolved AFTER the build (needs the starting chart count) — see below.
    forbidden = set(spec.forbidden_edges)
    mandatory = mandatory_seam_edges(mesh, fold_angle=fold_angle)
    nonmand_forbidden = forbidden - mandatory

    # Part table — compute only as much as the spec needs (plan: fast path). COARSE skips the
    # deep watershed/descriptors entirely; FULL runs the artist A1–A3 so hand-filled part ids
    # resolve. The shared preparation also carves explicit face-set selections (interactive
    # front-end) and normal-axis selectors (front/back robe) into their own parts before
    # assignment, then rewrites ``spec`` to reference the carved ids.
    seg, descriptors, classes, spec, assignment = build_guided_assignment(
        mesh, spec, segmentation_mode=segmentation_mode)
    built = build_guided_seams(mesh, seg, descriptors, classes, spec, assignment,
                               back_dir=back_dir, cone_limit=cone_limit, max_charts=max_charts,
                               max_diskify_rounds=max_diskify_rounds)
    # Front-preserve is a SOFT preserve that YIELDS to the hard gate (work plan §3 주의): the
    # welded-fold repair AVOIDS it (forbidden=hard|soft), but it is NOT force-stripped at the
    # end — so if a distortion/overlap repair must cut a front edge to pass the gate, that cut
    # stays (reported as "relaxed"). The hard preserve (3054) is always stripped.
    soft_preserve = set(built.front_preserve_edges)
    repair_forbidden = nonmand_forbidden | soft_preserve
    seams = set(built.seams)
    # Hard repair cap sits above the actual starting chart count (selector splits + face
    # protection can start well over the advisory target), so a distortion/overlap fix can
    # always split one more island.
    repair_island_hard_cap = _resolve_repair_island_hard_cap(
        config, repair_island_hard_cap, base_charts=len(flood_charts(mesh, seams)))
    aux_seams: set[int] = set()             # welded-fold-auxiliary (prune candidates)
    overlap_seams: set[int] = set()         # added by flip / raster-overlap repair
    distortion_seams: set[int] = set()      # added by the worst-island distortion split
    repacked = {"margin": False, "aabb": False}
    # PERSISTED pack strategy: ``measure()`` always re-unwraps, so a one-off ``repack()`` for
    # a cross-invasion would be overwritten on the next round. Instead we escalate the pack
    # margin / shape HERE and ``measure()`` applies it on every subsequent unwrap, so the fix
    # actually sticks into the shipped UV.
    pack = {"margin": margin, "shape": "CONCAVE"}

    def measure():
        """Unwrap+pack the current ``seams`` (with the persisted pack strategy) and return
        (metrics, gate, uvmap). Owns the shipped UV — every seam edit here is followed by
        exactly one re-SLIM."""
        unwrap_and_pack(obj, seams, method="MINIMUM_STRETCH", margin=pack["margin"],
                        pack_shape=pack["shape"])
        uvm = read_uvmap(obj, mesh)
        m = compute_guided_metrics(mesh, uvm, seams, fold_angle=fold_angle)
        return m, evaluate_chart_gate(m, config=config), uvm

    metrics, gate, uvmap = measure()
    metrics["repair_island_hard_cap"] = repair_island_hard_cap

    # Hard-gate repair loop (plan §step5) — MINIMAL and structure-preserving: each round
    # applies the single highest-priority fix and re-SLIMs, mirroring the chart engine's main
    # loop but NEVER broadly re-charting the guided seams. Priority: a welded mandatory fold
    # (local, forbidden-aware cut) > a flip/raster overlap (split the folding chart) > the
    # worst-island checker distortion (split that one island). This makes good on the hard
    # "no overlap / fold is a seam / distortion under bar" promise instead of only reporting it.
    for _ in range(max_repair_rounds):
        if gate.passed:
            break
        changed = False

        # (0) a mandatory fold welded in the UV → LOCAL, forbidden-aware min-cost cut. Avoids
        #     BOTH the hard preserve and the soft front preserve where possible.
        if metrics.get("mandatory_90_uv_unsplit", 0) > 0:
            r = split_welded_folds(mesh, seams, metrics["uv_unsplit_edge_ids"],
                                   forbidden=repair_forbidden, fold_angle=fold_angle)
            if r["added"] or r["local_cuts"] or r["fallback"]:
                aux_seams |= r["added"]
                changed = True

        # (1) flipped UV faces → re-split the folding charts (overlap correctness).
        if not changed:
            flips = flipped_faces(mesh, uvmap)
            if flips:
                chs = flood_charts(mesh, seams)
                fc = {f: cid for cid, fs in enumerate(chs) for f in fs}
                for cid in {fc[f] for f in flips if f in fc}:
                    _, _, ns = split_chart(mesh, chs[cid], seams)
                    if ns:
                        seams.update(ns); overlap_seams |= set(ns); changed = True

        # (2) raster overlap → split the self-overlapping charts; for the (rare with CONCAVE)
        #     inter-chart invasion escalate the PERSISTED pack strategy (margin bump → AABB)
        #     so the next unwrap re-packs that way and the fix survives into the shipped UV.
        if not changed and metrics["raster_overlap_ratio"] > config.raster_overlap_max:
            chs = flood_charts(mesh, seams)
            fc = {f: cid for cid, fs in enumerate(chs) for f in fs}
            diag = raster_overlap_diagnosis(mesh, uvmap, fc)
            if diag["cross_charts"] and not repacked["aabb"]:
                if not repacked["margin"]:
                    pack["margin"] = min(0.05, margin * 4); repacked["margin"] = True
                else:
                    pack["shape"] = "AABB"; pack["margin"] = margin; repacked["aabb"] = True
                changed = True
            else:
                for cid in diag["self_charts"]:
                    if cid < len(chs) and len(chs[cid]) >= 10 \
                            and len(flood_charts(mesh, seams)) < repair_island_hard_cap:
                        _, _, ns = split_chart(mesh, chs[cid], seams)
                        if ns:
                            seams.update(ns); overlap_seams |= set(ns); changed = True

        # (3) checker/stretch distortion over threshold → split the ONE worst island.
        if not changed:
            global_over = metrics["stretch_score"] > config.stretch_max
            worst_over = metrics["worst_island_distortion"] > config.worst_island_distortion_max
            chs = flood_charts(mesh, seams)
            if (global_over or worst_over) and len(chs) < repair_island_hard_cap:
                fstr = per_face_stretch(mesh, uvmap)
                worst = _worst_stretch_chart(mesh, chs, fstr)
                _, _, ns = split_chart(mesh, worst, seams)
                if ns:
                    seams.update(ns); distortion_seams |= set(ns); changed = True

        if not changed:
            break
        metrics, gate, uvmap = measure()
        metrics["repair_island_hard_cap"] = repair_island_hard_cap

    # User preserve (§step6): a non-mandatory forbidden edge must never ship. The cut paths
    # avoid them; strip any a repair split placed, then re-measure (honest about any failure
    # the strip reintroduces — preserve wins, the gate reports the rest).
    strip = {e for e in nonmand_forbidden if e in seams}
    if strip:
        seams -= strip
        aux_seams -= strip; overlap_seams -= strip; distortion_seams -= strip
        metrics, gate, uvmap = measure()
        metrics["repair_island_hard_cap"] = repair_island_hard_cap

    # Auxiliary-seam pruning (§step7): drop low-angle welded-fold-auxiliary seams whose
    # removal keeps EVERY hard gate green — minimise robe-surface low-angle seams like 3054.
    pruned: list[int] = []
    if gate.passed:
        cand = sorted((e for e in aux_seams if e in seams
                       and mesh.edges[e].dihedral_angle < fold_angle),
                      key=lambda e: mesh.edges[e].dihedral_angle)[:80]
        pruned = _prune_seams(seams, cand, lambda: measure()[1].passed)
        aux_seams -= set(pruned)
        metrics, gate, uvmap = measure()
        metrics["repair_island_hard_cap"] = repair_island_hard_cap

    # Island CONSOLIDATION (work plan §C): merge adjacent charts of the SAME chapter — the
    # artist wants few large per-part islands, not the fragmentation diskify/repair produced.
    # A merge dissolves a whole non-mandatory shared boundary and is KEPT only if the gate
    # stays green (so it never welds a fold, breaks a disk, or worsens distortion/overlap past
    # the bar) and never touches the hard preserve. Removing a seam can never CREATE a front
    # seam, so front protection is safe.
    islands_before = len(flood_charts(mesh, seams))
    merged_islands = 0
    if gate.passed and consolidate_islands:
        merged_islands = _consolidate_same_chapter_charts(
            mesh, seams, seg.face_part, assignment.part_chapter, mandatory,
            protect=nonmand_forbidden, fold_angle=fold_angle,
            accept=lambda: measure()[1].passed, max_merges=max_merges)
        if merged_islands:
            metrics, gate, uvmap = measure()
            metrics["repair_island_hard_cap"] = repair_island_hard_cap

    # Front-preserve final accounting (work plan §3 주의): a soft-preserve front edge that a
    # hard-gate repair (distortion/overlap) had to cut is RELAXED (hard gate outranks soft
    # preserve); the rest stayed preserved. Update the result so the report counts the FINAL
    # preserved set and flags how many were relaxed.
    built.front_preserve_relaxed = sorted(e for e in soft_preserve if e in seams)
    built.front_preserve_edges = sorted(e for e in soft_preserve if e not in seams)

    # Final seam taxonomy on the shipped set. The chart tagger names the distortion split
    # ``distortion_split``; normalise to the plan's ``distortion_repair`` (§step8).
    seam_types = _classify_seams(mesh, seams, aux_seams, overlap_seams=overlap_seams,
                                 distortion_seams=distortion_seams, forbidden=forbidden,
                                 fold_angle=fold_angle)
    for e, t in list(seam_types.items()):
        if t == "distortion_split":
            seam_types[e] = "distortion_repair"
    # Merge the chapter-level tags from the build (template / boundary / fallback) under the
    # chart engine's generic tags so the report distinguishes guided seam origins.
    for e, t in built.seam_origin.items():
        if e in seams and seam_types.get(e) == "segmentation":
            seam_types[e] = t

    # FIX: recompute chart→chapter from the FINAL seams (post repair/strip/prune) so the
    # report and overlays never mislabel/omit a chart from a stale (pre-repair) seam set.
    charts, chart_to_chapter, chapter_charts = map_charts_to_chapters(
        mesh, seams, seg.face_part, assignment.part_chapter)
    nondisk_charts = [cid for cid, fs in enumerate(charts) if not uv_is_disk(mesh, fs, seams)]

    # INTERACTIVE constraint verification (plan §작업7): when an interactive plan is supplied,
    # measure each APPROVED chapter's constraints on the FINAL seams (e.g. face front keeps 0
    # smooth seams) and fold the result into guided_complete. Computed here where the final
    # seams + assignment + chapter_charts are live.
    interactive = None
    if interactive_plan is not None:
        from artist_uv_agent.interactive_plan import (
            InteractiveUVPlan, evaluate_interactive_constraints,
        )
        iplan = InteractiveUVPlan.coerce(interactive_plan)
        interactive = evaluate_interactive_constraints(
            iplan, mesh, assignment, chapter_charts, seams,
            front_axis=spec.front_preserve_axis, up_axis=iplan.up_axis,
            fold_angle=fold_angle, threshold=spec.front_preserve_threshold,
            max_dihedral=spec.front_preserve_max_dihedral)

    report = build_guided_report(mesh, spec, assignment, built, metrics, gate, seam_types,
                                 pruned=pruned, chart_count=len(charts),
                                 chapter_charts=chapter_charts, nondisk_charts=nondisk_charts,
                                 interactive=interactive)
    report["island_consolidation"] = {"before": islands_before, "after": len(charts),
                                      "merged": merged_islands}
    parts_json = build_guided_parts_json(seg, descriptors, classes, assignment, built,
                                         chapter_charts=chapter_charts)

    return {
        "engine": "guided", "seams": sorted(seams), "chart_count": len(charts),
        "part_count": len(seg.parts), "chapter_count": len(assignment.chapters),
        "metrics": metrics, "gate": gate, "gate_config": config.to_dict(),
        "repair_island_hard_cap": repair_island_hard_cap,
        "report": report, "parts_json": parts_json,
        "assignment": assignment, "seam_result": built, "charts": charts,
        "chart_to_chapter": chart_to_chapter, "chapter_charts": chapter_charts,
        "nondisk_charts": nondisk_charts,
        "forbidden_edges": sorted(forbidden),
        "forbidden_stripped": sorted(set(built.forbidden_stripped) | strip),
        "forbidden_conflicts": built.forbidden_conflicts,
        "forbidden_disk_conflicts": built.forbidden_disk_conflicts,
        "seam_type_counts": _count_types(seam_types), "pruned_auxiliary": len(pruned),
        "front_preserve_edges": sorted(built.front_preserve_edges),
        # ``shippable`` == UV-technical (hard gate). ``guided_complete`` adds policy reflection.
        "shippable": report["uv_shippable"],
        "uv_shippable": report["uv_shippable"],
        "guided_complete": report["guided_complete"],
        "completion_status": report["completion_status"],
        "guided_policy_reflected": report["guided_policy_reflected"],
        "artist_intent_passed": report["artist_intent_passed"],
        "unmet_artist_intents": report["unmet_artist_intents"],
        "interactive_plan": interactive,
        "interactive_constraints_passed": report.get("interactive_constraints_passed", True),
    }


# Canonical artist intents (work plan §7) → chapter-name keywords used to match a spec chapter.
_ARTIST_INTENTS = {
    "staff": ("staff", "prong"),
    "face": ("face", "head", "beard"),
    "hood": ("hood", "cowl"),
    "upper_front_robe": ("upper_front_robe", "front_robe", "upper_robe", "torso", "chest"),
    "back_cloak": ("back_cloak", "back_robe", "cloak", "back_panel"),
    "sleeve": ("sleeve", "arm"),
    "hands": ("hand",),
    "belt": ("belt", "sash", "tied"),
    "lower_robe": ("lower_robe", "skirt", "lower"),
    "feet": ("foot", "feet"),
}


def _face_front_seam_count(mesh, assignment, final_seams, axis, *, threshold=0.35,
                           max_dihedral=45.0) -> int:
    """Count FINAL seams that cross a front-facing low-dihedral edge of a face chapter — these
    are exactly the "front-smooth seams the artist forbade" (work plan §1 success metric)."""
    fa = front_axis_vector(axis)
    if fa is None:
        return 0
    face_faces: set[int] = set()
    for ch in assignment.chapters:
        if ch.type == "face_front_preserve" or (ch.behavior == "front_preserve"
                                                and any(k in ch.name for k in _ARTIST_INTENTS["face"])):
            face_faces.update(ch.face_ids)
    if not face_faces:
        return 0
    n = 0
    for e in final_seams:
        ed = mesh.edges[e]
        if len(ed.face_ids) != 2 or ed.dihedral_angle >= max_dihedral:
            continue
        a, b = ed.face_ids
        if a not in face_faces or b not in face_faces:
            continue
        nv = np.asarray(mesh.faces[a].normal, float) + np.asarray(mesh.faces[b].normal, float)
        nn = np.linalg.norm(nv)
        if nn > 1e-9 and float(np.dot(nv / nn, fa)) > threshold:
            n += 1
    return n


def build_artist_intent_checklist(mesh, spec, assignment, built, seam_types, chapter_charts):
    """Per-artist-intent pass/fail checklist (work plan §7). Maps the 10 canonical artist
    judgements to the spec chapters and measures whether each was REFLECTED in the final seams,
    so the report says exactly which worker intents are met / partial / failed / missing — never
    a "success-looking failure". Returns ``(checklist, unmet, artist_intent_passed, face_policy)``.
    ``unmet`` = intents that are ``missing`` or ``failed`` (``partial`` = present-but-unverified,
    not counted as a hard miss)."""
    final_seams = set(seam_types)
    by_intent: dict[str, list] = {k: [] for k in _ARTIST_INTENTS}
    for ch in assignment.chapters:
        if ch.source != "spec":
            continue
        for intent, kws in _ARTIST_INTENTS.items():
            if any(k in ch.name for k in kws):
                by_intent[intent].append(ch)
                break

    template_set = set(built.template_chapters)
    # Face policy (work plan §1).
    face_chs = by_intent["face"]
    fss = _face_front_seam_count(mesh, assignment, final_seams, spec.front_preserve_axis,
                                 threshold=spec.front_preserve_threshold,
                                 max_dihedral=spec.front_preserve_max_dihedral)
    beard = next((c for c in face_chs if "beard" in c.name), None)
    beard_charts = len(chapter_charts.get(beard.index, [])) if beard else None
    face_axis_set = bool(spec.front_preserve_axis)
    if not face_chs:
        face_status = "missing"
    elif not face_axis_set:
        face_status = "partial"          # face chapter present but no front axis → label only
    elif fss == 0:
        face_status = "passed"
    else:
        face_status = "failed"
    face_policy = {"requested": bool(face_chs), "front_axis": spec.front_preserve_axis,
                   "front_island_chapters": [c.name for c in face_chs],
                   "front_smooth_seam_count": fss, "face_beard_chart_count": beard_charts,
                   "status": face_status}

    checklist: dict[str, dict] = {}
    for intent, chs in by_intent.items():
        if not chs:
            checklist[intent] = {"status": "missing", "reason": "no chapter covers this intent"}
            continue
        names = [c.name for c in chs]
        if intent == "face":
            checklist[intent] = {"status": face_status, "chapters": names,
                                 "front_smooth_seam_count": fss}
        elif intent in ("staff", "sleeve"):
            cyl = [c for c in chs if c.behavior in ("cylinder", "cylinder_group")]
            if not cyl:
                checklist[intent] = {"status": "partial", "chapters": names,
                                     "reason": "not a cylinder chapter"}
            elif all(c.index in template_set for c in cyl):
                checklist[intent] = {"status": "passed", "chapters": names,
                                     "details": {"tube_strip_reflected": True}}
            else:
                checklist[intent] = {"status": "failed", "chapters": names,
                                     "reason": "tube-strip policy not reflected (template did not fire)"}
        elif intent == "lower_robe":
            n = sum(len(chapter_charts.get(c.index, [])) for c in chs)
            ok = 3 <= n <= 6
            checklist[intent] = {"status": "passed" if ok else "partial", "chapters": names,
                                 "front_panel_count": n,
                                 "reason": "" if ok else f"panel count {n} outside artist target 3-6"}
        else:                                # hood, robe, belt, feet, back_cloak, hands
            checklist[intent] = {"status": "partial", "chapters": names,
                                 "reason": "present; structural policy applied, not visually verified"}

    # A "failed" intent (chapter present, policy not reflected) is ALWAYS unmet. A "missing"
    # intent counts only when the worker DECLARED it (spec.expected_intents) — a minimal spec is
    # not penalised for canonical intents it never raised.
    expected = set(spec.expected_intents)
    unmet = sorted(k for k, v in checklist.items()
                   if v["status"] == "failed" or (v["status"] == "missing" and k in expected))
    artist_intent_passed = not unmet
    return checklist, unmet, artist_intent_passed, face_policy


def chapter_coverage(assignment: ChapterAssignment) -> dict:
    """How much of the artist's guided intent actually drove the result (plan: a spec with
    empty ``source_part_ids`` ships via fallback and must NOT look like a success).

    ``guided_intent_applied`` is False when the spec resolved no parts (everything fell back),
    so a reviewer sees at a glance that the worker-style judgement was not applied. The face
    ratios + per-policy chapter counts quantify exactly how much was guided vs fallback."""
    total = sum(len(c.face_ids) for c in assignment.chapters) or 1
    spec_chs = [c for c in assignment.chapters if c.source == "spec"]
    resolved = [c for c in spec_chs if c.part_ids]
    unresolved = [c.name for c in spec_chs if not c.part_ids]
    spec_faces = sum(len(c.face_ids) for c in spec_chs)
    fb_faces = sum(len(c.face_ids) for c in assignment.chapters if c.source == "fallback")

    def n(beh):
        return sum(1 for c in resolved if c.behavior == beh)

    fb_ratio = round(fb_faces / total, 4)
    return {
        "total_faces": total,
        "spec_chapter_count": len(spec_chs),
        "resolved_spec_chapter_count": len(resolved),
        "unresolved_spec_chapters": unresolved,
        "spec_chapter_face_coverage": round(spec_faces / total, 4),
        "fallback_face_ratio": fb_ratio,
        # INTENT counts (what the spec REQUESTED) — NOT proof the policy was reflected in the
        # seams (see build_guided_report's reflection block). Renamed from the misleading
        # ``template_chapter_count`` (review item 1).
        "cylinder_policy_chapter_count": n("cylinder") + n("cylinder_group"),
        "front_preserve_chapter_count": n("front_preserve"),
        "keep_intact_chapter_count": n("keep_intact"),
        "organic_split_chapter_count": n("organic_split"),
        # intent is "applied" only if SOME spec chapter resolved parts and fallback is not ~all.
        # This is ASSIGNMENT coverage only — see ``guided_policy_reflected`` for whether the
        # per-chapter SEAM policy actually fired.
        "guided_intent_applied": bool(resolved) and fb_ratio < 0.999,
    }


def build_guided_report(mesh: MeshGraph, spec: GuidedUVSpec, assignment: ChapterAssignment,
                        built: GuidedSeamResult, metrics: dict, gate, seam_types: dict,
                        *, pruned: list, chart_count: int, chapter_charts=None,
                        nondisk_charts=None, interactive=None) -> dict:
    """``guided_uv_report.json`` content (plan §step8): final metrics, gate, forbidden
    conflicts, chapter→chart correspondence, seam-type counts, the guided-intent COVERAGE
    block, and the per-artist-intent CHECKLIST (so an empty/unmapped/under-reflected spec never
    reads as a guided success). Pure. ``chapter_charts`` (final, post-repair) is used for the
    per-chapter chart correspondence so the report is never stale.

    ``interactive`` (optional, the block from
    :func:`~artist_uv_agent.interactive_plan.evaluate_interactive_constraints`) embeds the
    INTERACTIVE chapter-seam-planning result and tightens ``guided_complete`` to also require
    every approved interactive constraint to pass (plan §작업7). When omitted, behaviour is
    unchanged."""
    from chart_uv_agent.pipeline import _count_types

    cc = chapter_charts if chapter_charts is not None else built.chapter_charts
    chapters_report = []
    for ch in assignment.chapters:
        chapters_report.append({
            **ch.to_dict(),
            "chart_ids": sorted(cc.get(ch.index, [])),
            "chart_count": len(cc.get(ch.index, [])),
        })
    coverage = chapter_coverage(assignment)

    # POLICY REFLECTION (review items 1/2): did the per-chapter SEAM policy actually fire, vs
    # merely being requested? A cylinder chapter whose template reverted on non-tube geometry
    # is REQUESTED but NOT reflected — reported separately so ``template=N`` can never be
    # mistaken for "N tube strips were produced".
    cyl_resolved = [c for c in assignment.chapters
                    if c.behavior in ("cylinder", "cylinder_group") and c.part_ids]
    template_set = set(built.template_chapters)
    unreflected = [c.name for c in cyl_resolved if c.index not in template_set]
    counts = _count_types(seam_types)

    # FRONT-PRESERVE reflection (work plan §3): a front_preserve chapter is REFLECTED only when
    # a front axis was given AND front-facing protected edges were actually generated; otherwise
    # it is a label only and is flagged.
    fp_chapters = [c.name for c in assignment.chapters
                   if c.behavior == "front_preserve" and c.part_ids]
    fp_axis = built.front_preserve_axis
    fp_edge_count = len(built.front_preserve_edges)
    fp_requested = bool(fp_chapters)
    fp_active = bool(fp_axis) and fp_edge_count > 0
    fp_satisfied = (not fp_requested) or fp_active
    fp_protection = ("active_view_axis" if fp_active
                     else "requested_no_edges" if (fp_requested and fp_axis)
                     else "label_only_no_auto_front_edges")

    policy_reflection = {
        "cylinder_policy_chapter_count": len(cyl_resolved),       # REQUESTED tube strips
        "template_policy_applied_count": len(template_set),       # ACTUALLY fired
        "chapter_template_seam_count": counts.get("chapter_template", 0),
        "unreflected_policy_chapters": unreflected,
        "front_preserve_protection": fp_protection,
        "front_preserve_chapters": fp_chapters,
        "front_preserve_edge_count": fp_edge_count,
        "front_preserve_disk_conflict_count": len(built.front_preserve_disk_conflicts),
        "front_preserve_relaxed_count": len(built.front_preserve_relaxed),
        "front_preserve_axis": fp_axis,
    }
    # Policy is "reflected" only when intent was applied, every cylinder chapter fired, AND
    # front-preserve (if requested) is active.
    guided_policy_reflected = (bool(coverage["guided_intent_applied"])
                               and not unreflected and fp_satisfied)

    warnings = list(assignment.warnings)
    if coverage["unresolved_spec_chapters"]:
        warnings.append(
            f"{len(coverage['unresolved_spec_chapters'])} spec chapter(s) resolved NO parts "
            f"(empty source_part_ids): {coverage['unresolved_spec_chapters']} — fill them from "
            f"guided_parts.json / the part overlay")
    if not coverage["guided_intent_applied"]:
        warnings.append(
            f"artist-guided intent NOT applied: fallback_face_ratio="
            f"{coverage['fallback_face_ratio']:.3f} (no/empty source_part_ids). The layout is "
            f"valid but is the fallback decomposition, not the worker's part judgement.")
    if fp_requested and not fp_axis:
        warnings.append(
            f"front_preserve chapter(s) {fp_chapters} present but no front_preserve_axis set — "
            f"front protection is LABEL ONLY (set spec.front_preserve_axis e.g. '+Z').")
    elif fp_requested and fp_axis and fp_edge_count == 0:
        warnings.append(
            f"front preserve requested (axis={fp_axis}) but NO front-facing protected edges "
            f"were generated — check the axis/threshold/max_dihedral.")
    if built.front_preserve_relaxed:
        warnings.append(
            f"{len(built.front_preserve_relaxed)} front-preserve edge(s) RELAXED (cut by a "
            f"hard-gate distortion/overlap repair — the hard gate outranks soft front preserve; "
            f"{fp_edge_count} front edge(s) remain preserved).")
    if unreflected:
        warnings.append(
            f"cylinder policy NOT reflected for chapter(s) {unreflected}: a tube strip was "
            f"requested but the template did not fire (geometry not a clean two-loop tube). "
            f"Use a cylinder_group chapter and/or mode='full' for shaft/prong separation.")

    # ARTIST INTENT CHECKLIST (work plan §7): per-judgement status; missing/failed → unmet.
    checklist, unmet_intents, artist_intent_passed, face_policy = build_artist_intent_checklist(
        mesh, spec, assignment, built, seam_types, cc)
    if face_policy["status"] == "failed":
        warnings.append(
            f"face front island NOT preserved: {face_policy['front_smooth_seam_count']} "
            f"front-smooth seam(s) cross the face front (axis {face_policy['front_axis']}).")
    if unmet_intents:
        warnings.append(f"unmet artist intents: {unmet_intents}")

    # COMPLETION STATUS (work plan §1 + §7): separate UV-technical success (hard gate) from
    # guided-judgement success. ``guided_complete`` now also requires the artist checklist to
    # pass (no missing/failed intent) — a valid UV with unmet intents is NOT a guided success.
    gate_passed = gate.passed if hasattr(gate, "passed") else bool(gate.get("passed"))
    uv_shippable = bool(gate_passed)
    # INTERACTIVE constraints (plan §작업7): when an interactive plan was run, every approved
    # chapter's machine-checkable constraint must ALSO pass for guided_complete. Absent → True
    # (no interactive judgement to honour), so non-interactive runs are unaffected.
    interactive_passed = True
    if interactive is not None:
        interactive_passed = bool(interactive.get("interactive_constraints_passed", True))
        unmet_ic = _unmet_interactive_constraints(interactive)
        if unmet_ic:
            warnings.append(f"unmet interactive constraints: {unmet_ic}")
    guided_complete = (uv_shippable and guided_policy_reflected and artist_intent_passed
                       and interactive_passed)
    if guided_complete:
        completion_status = "guided_complete"
    elif uv_shippable and interactive is not None and not interactive_passed:
        completion_status = "accepted_with_unmet_interactive_constraints"
    elif uv_shippable and coverage["guided_intent_applied"]:
        completion_status = "accepted_with_policy_warning"
    elif uv_shippable:
        completion_status = "valid_fallback_uv"
    else:
        completion_status = "failed_gate"

    report = {
        "engine": "guided", "object": spec.object, "spec_version": spec.version,
        "mandatory_fold_angle": spec.mandatory_fold_angle,
        "chapter_count": len(assignment.chapters),
        "spec_chapter_count": sum(1 for c in assignment.chapters if c.source == "spec"),
        "fallback_chapter_count": sum(1 for c in assignment.chapters if c.source == "fallback"),
        "coverage": coverage,
        "guided_intent_applied": coverage["guided_intent_applied"],
        "guided_policy_reflected": guided_policy_reflected,
        "uv_shippable": uv_shippable,
        "guided_complete": guided_complete,
        "completion_status": completion_status,
        "artist_intent_passed": artist_intent_passed,
        "unmet_artist_intents": unmet_intents,
        "artist_intent_checklist": checklist,
        "face_policy": face_policy,
        "policy_reflection": policy_reflection,
        "warnings": warnings,
        "chapters": chapters_report,
        "final_chart_count": chart_count,
        "seam_type_counts": _count_types(seam_types),
        "forbidden_edges": sorted(spec.forbidden_edges),
        "forbidden_stripped": sorted(built.forbidden_stripped),
        "forbidden_conflicts": sorted(built.forbidden_conflicts),
        "forbidden_disk_conflicts": sorted(built.forbidden_disk_conflicts),
        "nondisk_charts": sorted(nondisk_charts if nondisk_charts is not None
                                 else built.nondisk_charts),
        "pruned_auxiliary": len(pruned),
        "cap_exceeded": built.cap_exceeded,
        "assignment_warnings": assignment.warnings,
        "gate": gate.to_dict() if hasattr(gate, "to_dict") else gate,
        "repair_island_hard_cap": metrics.get("repair_island_hard_cap"),
        "metrics": {k: metrics.get(k) for k in (
            "overlap_ratio", "raster_overlap_ratio", "stretch_score",
            "worst_island_distortion", "packing_efficiency", "texel_density_variance",
            "island_count", "mandatory_90_missing", "mandatory_90_uv_unsplit",
            "uv_bounds_ok")},
    }
    if interactive is not None:
        report["interactive_plan"] = interactive
        report["interactive_constraints_passed"] = interactive_passed
    return report


def _unmet_interactive_constraints(interactive: dict) -> list[str]:
    """``"chapter.rule"`` for every CHECKABLE interactive constraint that failed (plan §작업7
    — surface exactly which approved judgement the result broke, never hide it)."""
    out: list[str] = []
    for cname, cres in interactive.get("constraint_results", {}).items():
        if not cres.get("chapter_resolved", True):
            out.append(f"{cname}.<unresolved_chapter>")
            continue
        for rule, r in cres.get("constraints", {}).items():
            if r.get("checkable") and not r.get("passed"):
                out.append(f"{cname}.{rule}")
    return out
