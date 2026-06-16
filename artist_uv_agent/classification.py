"""A3 — rule-based part classification (AUTO_ARTIST_UV_PLAN §5.A3).

Assign each part a coarse GEOMETRY class (not a semantic name like "left arm"):

    panel     flat or cloth-like area
    strip     long thin cloth/panel section
    cylinder  limb, handle, shaft, tine
    cap       end of a cylinder or round protrusion
    blob      head/torso/organic mass
    detail    small attached feature
    shell     detached / weakly-attached component
    unknown   fallback

Rules over the A2 descriptors, v1 (plan §5.A3: "rule-based in v1, with optional ML
later"). Each part gets a class + a confidence; an ``unknown`` class is EXPLICIT (the A4
seam layer falls back to chart segmentation for it), never a silent mislabel.
"""

from __future__ import annotations

from dataclasses import dataclass

from artist_uv_agent.descriptors import PartDescriptor

PART_TYPES = ("panel", "strip", "cylinder", "cap", "blob", "detail", "shell", "unknown")


@dataclass
class PartClass:
    part_id: int
    type: str
    confidence: float
    reason: str = ""

    def to_dict(self) -> dict:
        return {"part_id": self.part_id, "type": self.type,
                "confidence": round(self.confidence, 3), "reason": self.reason}


# Tunable rule thresholds (plan §5.A3; recalibrate on the fixture suite per §AR7).
DETAIL_AREA_FRAC = 0.02     # below this share of mesh area → a small attached feature
STRIP_ELONGATION = 2.5      # length/width above this → strip
CYL_ELONGATION = 1.8        # elongated …
CYL_SCORE = 0.55            # … and normals wrap the long axis → cylinder
PANEL_FLATNESS = 0.18       # 3rd-axis extent / 2nd-axis extent below this → planar
CAP_FLATNESS = 0.30
BLOB_CONE = 80.0            # normal cone above this (closed-ish) → organic mass


def classify_part(d: PartDescriptor, *, seg_neighbors: set[int] | None = None) -> PartClass:
    """Classify ONE part from its descriptor (plan §5.A3). ``seg_neighbors`` is unused in
    v1's local rules but kept for the cap/detail-near-parent relationships (A6)."""
    # 1. Detached shell — segmentation gave it no part neighbours (its own component).
    if seg_neighbors is not None and len(seg_neighbors) == 0 and d.area_frac < 0.5:
        return PartClass(d.part_id, "shell", 0.6, "no attached neighbour")

    # 2. Small attached feature → detail (grouped near its parent in A6).
    if d.area_frac < DETAIL_AREA_FRAC:
        return PartClass(d.part_id, "detail", _conf(0.7, d), f"area_frac {d.area_frac:.3f} small")

    # 3. Strip — long and thin (cloth band / panel strip).
    if d.elongation >= STRIP_ELONGATION and d.flatness <= CAP_FLATNESS:
        return PartClass(d.part_id, "strip", _conf(min(1.0, d.stripness + 0.4), d),
                         f"elongation {d.elongation:.1f}, flat")

    # 4. Cylinder — a tube wall: normals wrap the long axis. Either clearly elongated, or
    #    a clean OPEN tube (two boundary loops, normals tightly wrapping) regardless of
    #    aspect ratio. The high cylindricalness bar on the open-tube path keeps an
    #    elongated torso (a partially-wrapping blob) out of this class.
    if (d.elongation >= CYL_ELONGATION and d.cylindricalness >= CYL_SCORE) \
            or (d.cylindricalness >= 0.85 and d.boundary_loops >= 2):
        return PartClass(d.part_id, "cylinder", _conf(d.cylindricalness, d),
                         f"elongation {d.elongation:.1f}, cyl {d.cylindricalness:.2f}, loops {d.boundary_loops}")

    # 5. Panel — planar (flat sheet); strip already handled the long case.
    if d.flatness <= PANEL_FLATNESS and d.normal_cone_deg <= 55.0:
        return PartClass(d.part_id, "panel", _conf(1.0 - d.flatness, d),
                         f"flatness {d.flatness:.2f}, cone {d.normal_cone_deg:.0f}")

    # 6. Cap — a small-ish rounded end (round, not elongated, fairly flat-ish disk).
    if d.is_disk and d.boundary_loops <= 1 and d.elongation < CYL_ELONGATION \
            and d.flatness <= CAP_FLATNESS and d.area_frac < 0.12:
        return PartClass(d.part_id, "cap", _conf(0.6, d), "small round disk end")

    # 7. Blob — organic mass (closed-ish normal cone, not elongated).
    if d.normal_cone_deg >= BLOB_CONE and d.elongation < STRIP_ELONGATION:
        return PartClass(d.part_id, "blob", _conf(min(1.0, d.normal_cone_deg / 140.0), d),
                         f"cone {d.normal_cone_deg:.0f}")

    return PartClass(d.part_id, "unknown", 0.2 * d.confidence + 0.1,
                     "no rule matched → chart fallback")


def _conf(rule_strength: float, d: PartDescriptor) -> float:
    """Blend the rule's geometric strength with the part's segmentation confidence —
    a class on a poorly-walled part stays low-confidence (drives nothing aggressive)."""
    return float(max(0.0, min(1.0, 0.6 * rule_strength + 0.4 * d.confidence)))


def classify_parts(descriptors: list[PartDescriptor],
                   neighbors: dict[int, set[int]] | None = None) -> list[PartClass]:
    """Classify every part; refine cap detection using cylinder adjacency (a small round
    disk touching a cylinder is a cap, plan §5.A4)."""
    neighbors = neighbors or {}
    classes = [classify_part(d, seg_neighbors=neighbors.get(d.part_id))
               for d in descriptors]
    by_id = {c.part_id: c for c in classes}
    cyl_ids = {c.part_id for c in classes if c.type == "cylinder"}
    for d, c in zip(descriptors, classes):
        if c.type in ("detail", "blob") and d.is_disk and d.boundary_loops <= 1 \
                and d.flatness <= CAP_FLATNESS and (neighbors.get(d.part_id, set()) & cyl_ids):
            by_id[d.part_id] = PartClass(d.part_id, "cap", max(c.confidence, 0.5),
                                         "round disk adjacent to a cylinder")
    return [by_id[d.part_id] for d in descriptors]
