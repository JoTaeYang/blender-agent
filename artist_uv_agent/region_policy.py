"""Important Region Policy (IMPORTANT_REGION_UV_POLICY_PLAN + REGION_AWARE_FACE_UV_RECOVERY_PLAN).

An *auxiliary* policy layered on ``chart_uv_agent.pipeline.run_chart_uv`` — NOT a new UV engine
and NOT a base-solver rewrite. It protects the regions a texture artist cares about from
needless low-angle *smooth* seams, while ALWAYS yielding to the ≥90° mandatory-seam rule.

Two modes:

- ``face_recovery`` (DEFAULT, the v2 plan): reduce face-front smooth seams at the FRONT of the
  pipeline — region-aware ``edge_cut_cost`` (the repair/reroute cost, §6.2/§6.4) plus a
  post-segmentation *protected merge* (§6.3) that dissolves face_front_core interior smooth
  boundaries when the union stays a developable disk under a bounded normal cone (so the face
  stays coherent WITHOUT exploding distortion). The face is split into THREE zones
  (face_front_core / face_side_transition / head_back_neck_preferred) — never one giant
  protected island (the v1 failure mode).
- ``post_split_reject`` (EXPERIMENTAL, off by default): the v1 approach — reject a distortion
  split that cuts a protected smooth edge. Kept for the report + A/B comparison only; the v1
  Blender run showed it does NOT reduce face seams (overlap-correctness re-cuts the face).

Precedence (both modes): **mandatory 90° fold > region protection > distortion split**. A ≥90°
fold is never protected/merged away; mandatory wins and the clash is reported.

Pure + Blender-free: it computes face/edge sets, the region-aware cost multipliers, the
protected-merge decision (topology + normal-cone only — the real unwrap re-validates), and the
``regions`` report block. The axis is never guessed (§11.7).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from artist_uv_agent.seam_policy import _axis_vector
from uv_agent.geometry.mesh_graph import MeshGraph

FOLD_ANGLE = 90.0

# Zone kind → priority (higher wins when face sets overlap) + default smooth-seam cost.
_ZONE_PRIORITY = {"face_front_core": 3, "face_front": 3, "face_side_transition": 2,
                  "head_back_neck_preferred": 1, "generic": 0}
_DEFAULT_ZONE_COST = {"face_front_core": 50.0, "face_front": 50.0,
                      "face_side_transition": 5.0, "head_back_neck_preferred": 0.25,
                      "generic": 1.0}


@dataclass
class ImportantRegion:
    """One artist-important region/zone. ``protected_edges`` are the sub-
    ``forbidden_smooth_angle`` *interior* edges to keep OFF the seam set; a ≥90° fold is never
    placed here (mandatory wins), so this set is smooth-only by construction. ``smooth_seam_cost``
    is the multiplier applied to a sub-fold edge's cut cost (>1 discourages a cut, <1 invites
    one). ``allow_auxiliary_seams`` lets side/back zones grow helper islands (§5.4)."""

    name: str
    kind: str = "generic"
    face_ids: set[int] = field(default_factory=set)
    protected_edges: set[int] = field(default_factory=set)
    preferred_edges: set[int] = field(default_factory=set)
    forbidden_smooth_angle: float = FOLD_ANGLE
    distortion_priority: str = "secondary"   # "primary" | "secondary"
    max_smooth_seams: int | None = None
    smooth_seam_cost: float = 1.0
    allow_auxiliary_seams: bool = True
    detection: str = "explicit"     # "explicit" | "heuristic"
    confidence: str = "high"        # "high" | "low"
    front_axis: str = ""
    up_axis: str = ""

    def to_report(self) -> dict:
        return {"name": self.name, "kind": self.kind,
                "face_count": len(self.face_ids),
                "protected_edge_count": len(self.protected_edges),
                "preferred_edge_count": len(self.preferred_edges),
                "smooth_seam_cost": self.smooth_seam_cost,
                "allow_auxiliary_seams": self.allow_auxiliary_seams,
                "detection": self.detection, "confidence": self.confidence,
                "front_axis": self.front_axis, "up_axis": self.up_axis}


@dataclass
class RegionPolicyConfig:
    """Detection + cost thresholds. Axes default EMPTY — never assume an axis for an unknown
    asset (§11.7). The human-statue test passes ``front_axis="-Y"``, ``up_axis="+Z"``."""

    front_axis: str = ""
    up_axis: str = ""
    # face_front (v1 single zone) / face_front_core (v2) detection
    face_front_normal_threshold: float = 0.25   # face.normal·front must exceed this (v1)
    core_facing_min: float = 0.5                # strongly-front faces are the CORE (v2)
    side_facing_min: float = 0.0                # front-ish upper faces are the SIDE band (v2)
    face_upper_body_z_min: float = 0.55         # keep core/side faces in the top of the up span
    neck_z_min: float = 0.35                    # neck band lower bound (preferred-seam zone)
    face_center_radius_frac: float = 0.35       # drop protrusions (staff/trident) off-centre
    smooth_seam_angle_max: float = 45.0
    protected_cost_multiplier: float = 10.0     # v1 generic protected cost
    # Protected-merge (§6.3): the union normal-cone bound — coherent face WITHOUT distortion
    # blow-up. Larger than the base merge cone (50°) but far below a whole-face shell.
    core_merge_cone_limit: float = 68.0


def _face_centroid(mesh: MeshGraph, face) -> np.ndarray:
    co = np.array([mesh.vertices[v].co for v in face.vertex_ids], dtype=float)
    return co.mean(axis=0)


def region_protected_edges(mesh: MeshGraph, face_ids, *, angle: float = FOLD_ANGLE) -> set[int]:
    """Interior edges of ``face_ids`` whose dihedral is below ``angle`` — the smooth edges to
    keep off the seam set. A ≥``angle`` fold is deliberately excluded so mandatory always wins."""
    fset = set(face_ids)
    out: set[int] = set()
    for e in mesh.edges:
        if len(e.face_ids) == 2 and e.face_ids[0] in fset and e.face_ids[1] in fset \
                and e.dihedral_angle < angle:
            out.add(e.id)
    return out


def region_interior_edges(mesh: MeshGraph, face_ids) -> set[int]:
    """Every edge with BOTH faces in ``face_ids`` (smooth or fold) — the edges that, if cut,
    show as a seam *inside* the region. Used to split a region's seams into mandatory/smooth."""
    fset = set(face_ids)
    return {e.id for e in mesh.edges
            if len(e.face_ids) == 2 and e.face_ids[0] in fset and e.face_ids[1] in fset}


def _face_geometry(mesh: MeshGraph, config: RegionPolicyConfig):
    """Shared geometry for the heuristics: per-face (facing·front, up-coord, horizontal
    distance from the vertical centre line) + a finite mask. Returns ``None`` if the axis is
    unknown (never guessed, §11.7) or no face is finite."""
    front = _axis_vector(config.front_axis)
    up = _axis_vector(config.up_axis)
    if front is None or up is None or not mesh.faces:
        return None
    centroids = np.array([_face_centroid(mesh, f) for f in mesh.faces])
    normals = np.array([np.asarray(f.normal, dtype=float) for f in mesh.faces])
    finite = np.isfinite(centroids).all(axis=1) & np.isfinite(normals).all(axis=1)
    if not finite.any():
        return None
    fin_c = centroids[finite]
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        up_coord = centroids @ up
        fin_up = fin_c @ up
        lo, hi = float(fin_up.min()), float(fin_up.max())
        span = (hi - lo) or 1.0
        centre = fin_c.mean(axis=0)
        rel = centroids - centre
        horiz = rel - np.outer(rel @ up, up)
        horiz_dist = np.linalg.norm(horiz, axis=1)
        radius = float(horiz_dist[finite].max()) or 1.0
        facing = normals @ front
    return {"facing": facing, "up_coord": up_coord, "lo": lo, "span": span,
            "horiz_dist": horiz_dist, "radius": radius, "finite": finite}


def detect_face_front(mesh: MeshGraph, config: RegionPolicyConfig, *,
                      name: str = "face_front_auto") -> ImportantRegion | None:
    """v1 single-zone ``face_front`` heuristic (IMPORTANT_REGION_UV_POLICY_PLAN §5.2). Kept for
    backward compatibility; ``classify_face_regions`` is the v2 3-zone entry point."""
    g = _face_geometry(mesh, config)
    if g is None:
        return None
    z_bar = g["lo"] + config.face_upper_body_z_min * g["span"]
    radius_bar = config.face_center_radius_frac * g["radius"]
    faces = {f.id for f in mesh.faces
             if g["finite"][f.id]
             and g["facing"][f.id] > config.face_front_normal_threshold
             and g["up_coord"][f.id] >= z_bar
             and g["horiz_dist"][f.id] <= radius_bar}
    if not faces:
        return None
    protected = region_protected_edges(mesh, faces, angle=FOLD_ANGLE)
    confidence = "high" if len(faces) >= 30 else "low"
    return ImportantRegion(name=name, kind="face_front", face_ids=faces,
                           protected_edges=protected, smooth_seam_cost=config.protected_cost_multiplier,
                           allow_auxiliary_seams=False, detection="heuristic",
                           confidence=confidence, front_axis=config.front_axis,
                           up_axis=config.up_axis)


def classify_face_regions(mesh: MeshGraph, config: RegionPolicyConfig, *,
                          zone_costs: dict | None = None) -> list[ImportantRegion]:
    """v2 three-zone heuristic (REGION_AWARE_FACE_UV_RECOVERY_PLAN §5.1). Splits the head into
    DISJOINT face sets so the face is never one giant protected island (§5.4):

    - ``face_front_core``: strongly front-facing (``facing > core_facing_min``), upper, central.
    - ``face_side_transition``: front-ish upper faces not in the core (the band toward the ears).
    - ``head_back_neck_preferred``: back-facing faces + the neck band — the preferred seam zone.

    Returns the zones that have faces (empty list if the axis is unknown — never guessed). Each
    zone carries its ``smooth_seam_cost`` (core high, side medium, back low) so the cut cost and
    protected merge steer seams away from the face front and toward the back/neck."""
    g = _face_geometry(mesh, config)
    if g is None:
        return []
    costs = {**_DEFAULT_ZONE_COST, **(zone_costs or {})}
    z_bar = g["lo"] + config.face_upper_body_z_min * g["span"]
    neck_bar = g["lo"] + config.neck_z_min * g["span"]
    radius_bar = config.face_center_radius_frac * g["radius"]
    core, side, back = set(), set(), set()
    for f in mesh.faces:
        i = f.id
        if not g["finite"][i]:
            continue
        fac, uc, hd = g["facing"][i], g["up_coord"][i], g["horiz_dist"][i]
        if uc >= z_bar and fac > config.core_facing_min and hd <= radius_bar:
            core.add(i)
        elif fac < config.side_facing_min or (neck_bar <= uc < z_bar):
            back.add(i)        # back of head + neck band → preferred seam zone
        elif uc >= z_bar and fac > config.side_facing_min:
            side.add(i)        # front-ish upper, not core → transition band
    out: list[ImportantRegion] = []
    for fs, kind, name in ((core, "face_front_core", "face_front_core"),
                           (side, "face_side_transition", "face_side_transition"),
                           (back, "head_back_neck_preferred", "head_back_neck_preferred")):
        if not fs:
            continue
        protected = region_protected_edges(mesh, fs) if kind != "head_back_neck_preferred" else set()
        preferred = region_interior_edges(mesh, fs) if kind == "head_back_neck_preferred" else set()
        conf = "high" if (kind != "face_front_core" or len(fs) >= 30) else "low"
        out.append(ImportantRegion(
            name=name, kind=kind, face_ids=fs, protected_edges=protected,
            preferred_edges=preferred, smooth_seam_cost=costs.get(kind, 1.0),
            allow_auxiliary_seams=(kind != "face_front_core"), detection="heuristic",
            confidence=conf, front_axis=config.front_axis, up_axis=config.up_axis))
    return out


@dataclass
class RegionPolicy:
    """The assembled, mesh-resolved policy passed into ``run_chart_uv(region_policy=...)``.
    A thin lookup layer over its :class:`ImportantRegion`s — never mutates the mesh."""

    regions: list[ImportantRegion]
    config: RegionPolicyConfig = field(default_factory=RegionPolicyConfig)
    mode: str = "face_recovery"     # "face_recovery" | "post_split_reject"

    def __post_init__(self) -> None:
        # protected smooth edge id -> owning region name (highest-priority zone wins).
        self._edge_region: dict[int, str] = {}
        # sub-fold edge id -> cut-cost multiplier (core high, back low; default 1.0).
        self._cost_mult: dict[int, float] = {}
        for r in sorted(self.regions, key=lambda r: _ZONE_PRIORITY.get(r.kind, 0), reverse=True):
            for e in r.protected_edges:
                self._edge_region.setdefault(e, r.name)
                self._cost_mult.setdefault(e, r.smooth_seam_cost)
            for e in r.preferred_edges:
                self._cost_mult.setdefault(e, r.smooth_seam_cost)

    # -- generic lookups (used by both modes + the report) ----------------
    @property
    def protected_smooth_edges(self) -> set[int]:
        return set(self._edge_region)

    @property
    def preferred_edges(self) -> set[int]:
        out: set[int] = set()
        for r in self.regions:
            out |= r.preferred_edges
        return out

    @property
    def core_protected_edges(self) -> set[int]:
        """face_front_core (or v1 face_front) interior smooth edges — the protected-merge set."""
        out: set[int] = set()
        for r in self.regions:
            if r.kind in ("face_front_core", "face_front"):
                out |= r.protected_edges
        return out

    def edge_cost_multiplier(self, edge_id: int) -> float:
        """Cut-cost multiplier for ``edge_id`` (1.0 = neutral). Consumed by
        ``chart_uv_agent.segmentation.edge_cut_cost`` — only ever applied to sub-fold edges, so
        a ≥90° mandatory edge is unaffected (mandatory always wins)."""
        return self._cost_mult.get(edge_id, 1.0)

    # -- experimental post-split reject (mode="post_split_reject") ---------
    def protected_cut(self, edges) -> set[int]:
        return {e for e in edges if e in self._edge_region}

    def region_for_edge(self, edge_id: int) -> str | None:
        return self._edge_region.get(edge_id)

    def region_names_for(self, edges) -> list[str]:
        return sorted({self._edge_region[e] for e in edges if e in self._edge_region})

    def seam_policy_constraints(self) -> tuple[set[int], set[int]]:
        """Feed ``artist_uv_agent.seam_policy.decide_edge_seams`` (§5.3): protected smooth edges
        become ``forbidden``, preferred edges become ``preferred``. ``decide_edge`` checks the
        ≥90° fold rule BEFORE the forbidden rule, so a mandatory fold stays ``mandatory``."""
        return self.protected_smooth_edges, self.preferred_edges


def region_edge_cost_multiplier(edge_id: int, regions, mesh: MeshGraph) -> float:
    """Module-level helper (REGION_AWARE_FACE_UV_RECOVERY_PLAN §6.1): the cut-cost multiplier an
    edge gets from a list of regions. Highest-priority owning zone wins; a ≥90° fold is never
    multiplied (mandatory wins). Returns 1.0 when no zone owns the edge."""
    if mesh.edges[edge_id].dihedral_angle >= FOLD_ANGLE:
        return 1.0
    best_pri, mult = -1, 1.0
    for r in regions:
        pri = _ZONE_PRIORITY.get(r.kind, 0)
        if pri > best_pri and (edge_id in r.protected_edges or edge_id in r.preferred_edges):
            best_pri, mult = pri, r.smooth_seam_cost
    return mult


def region_protected_merge(mesh: MeshGraph, seams: set[int], policy: RegionPolicy, *,
                           fold_angle: float = FOLD_ANGLE, normals=None) -> dict:
    """Post-segmentation protected merge (REGION_AWARE_FACE_UV_RECOVERY_PLAN §6.3) — the FRONT-
    stage lever that actually reduces face-front smooth seams.

    Dissolves a chart-pair boundary IFF (a) it TOUCHES the face_front_core (≥1 shared edge is a
    core interior smooth edge), (b) it carries NO mandatory ≥90° fold (so every removed edge is
    a sub-fold smooth seam and mandatory always wins), and (c) the merged union stays a
    topological **disk** under ``config.core_merge_cone_limit`` (so the face front becomes a
    coherent island WITHOUT the v1 distortion blow-up). Segmentation charts straddle the
    core/side line, so a strict all-core boundary almost never exists on a real head — touching
    the core (plus the disk + cone guards) is the right, safe condition. Pure: topology +
    normal-cone only; the Blender unwrap re-validates overlap/stretch.

    Returns ``{"removed": [edge ids], "merges": int, "history": [...]}``. Mutates ``seams``."""
    from chart_uv_agent.segmentation import (
        _face_normals, flood_charts, is_disk, normal_cone_halfangle,
    )

    if normals is None:
        normals = _face_normals(mesh)
    core = policy.core_protected_edges
    cone_limit = policy.config.core_merge_cone_limit
    removed: list[int] = []
    history: list[dict] = []
    merges = 0

    changed = True
    while changed:
        changed = False
        charts = flood_charts(mesh, seams)
        face_chart = {fid: cid for cid, fs in enumerate(charts) for fid in fs}
        # Group ALL shared seam edges by the chart pair they separate.
        border: dict[tuple[int, int], list[int]] = {}
        for eid in seams:
            e = mesh.edges[eid]
            if len(e.face_ids) != 2:
                continue
            ca, cb = face_chart.get(e.face_ids[0]), face_chart.get(e.face_ids[1])
            if ca is None or cb is None or ca == cb:
                continue
            border.setdefault((min(ca, cb), max(ca, cb)), []).append(eid)

        for (ca, cb), edges in sorted(border.items()):
            # Skip a boundary carrying ANY mandatory fold (mandatory always wins) and any
            # boundary that does NOT touch the protected core (a purely side/back boundary is
            # allowed to stay a seam, §5.4). After the mandatory check every edge is smooth.
            if any(mesh.edges[e].dihedral_angle >= fold_angle for e in edges):
                continue
            if not any(e in core for e in edges):
                continue
            union = charts[ca] + charts[cb]
            cone = normal_cone_halfangle(mesh, union, normals)
            if is_disk(mesh, union) and cone <= cone_limit:
                seams.difference_update(edges)
                removed.extend(edges)
                merges += 1
                history.append({"merged_charts": [ca, cb],
                                "removed_smooth_edges": len(edges),
                                "core_edges_removed": sum(1 for e in edges if e in core),
                                "union_cone": round(float(cone), 2)})
                changed = True
                break
    return {"removed": removed, "merges": merges, "history": history}


def region_boundary_audit(mesh: MeshGraph, seams, policy: RegionPolicy) -> dict:
    """Per-zone seam accounting (REGION_AWARE_FACE_UV_RECOVERY_PLAN §6.1) — mandatory vs smooth
    interior seams for each zone, the headline ``face_front_core`` smooth-seam count (the
    milestone's success metric), and the total face smooth seams. Pure."""
    seamset = set(seams)
    per_zone: dict[str, dict] = {}
    core_smooth = 0
    face_smooth = 0
    for r in policy.regions:
        interior = region_interior_edges(mesh, r.face_ids) & seamset
        mand = {e for e in interior if mesh.edges[e].dihedral_angle >= FOLD_ANGLE}
        smooth = interior - mand
        per_zone[r.name] = {"kind": r.kind, "mandatory_seams": len(mand),
                            "smooth_seams": len(smooth)}
        if r.kind in ("face_front_core", "face_front"):
            core_smooth += len(smooth)
        if r.kind in ("face_front_core", "face_front", "face_side_transition"):
            face_smooth += len(smooth)
    return {"per_zone": per_zone, "face_front_core_smooth_seams": core_smooth,
            "face_smooth_seams": face_smooth}


def load_region_policy(spec, mesh: MeshGraph) -> RegionPolicy | None:
    """Build a :class:`RegionPolicy` from a region-spec dict, or ``None``.

    Returns ``None`` (identical-to-baseline behaviour) when ``spec`` is falsy / ``enabled=false``
    / no region resolves. ``mode`` (default ``face_recovery``) selects the strategy. For
    ``face_recovery`` an empty ``regions`` list (or any v2 zone with no explicit faces) is
    resolved by :func:`classify_face_regions`; explicit ``face_ids``/``protected_edges`` always
    win (§5.6). A v1 spec (``mode`` absent, a single ``face_front`` region) loads as before but
    runs under ``face_recovery`` (post-split reject is off by default, §2.1)."""
    if not spec or not spec.get("enabled", True):
        return None
    mode = spec.get("mode", "face_recovery")
    config = RegionPolicyConfig(
        front_axis=spec.get("front_axis", "") or "",
        up_axis=spec.get("up_axis", "") or "",
    )
    for k in ("face_front_normal_threshold", "core_facing_min", "side_facing_min",
              "face_upper_body_z_min", "neck_z_min", "face_center_radius_frac",
              "smooth_seam_angle_max", "protected_cost_multiplier", "core_merge_cone_limit"):
        if k in spec:
            setattr(config, k, spec[k])

    raw = spec.get("regions", [])
    explicit: list[ImportantRegion] = []
    zone_costs: dict[str, float] = {}
    need_heuristic = False
    for r in raw:
        name = r.get("name", "region")
        kind = r.get("kind", "generic")
        face_ids = set(int(f) for f in (r.get("face_ids") or []))
        protected = set(int(e) for e in (r.get("protected_edges") or []))
        preferred = set(int(e) for e in (r.get("preferred_edges") or []))
        angle = float(r.get("forbidden_smooth_angle", FOLD_ANGLE))
        if "smooth_seam_cost" in r:
            zone_costs[kind] = float(r["smooth_seam_cost"])
        if not face_ids and not protected:
            need_heuristic = True   # this zone is heuristic — resolved in one classify pass
            continue
        if face_ids and not protected and kind != "head_back_neck_preferred":
            protected = region_protected_edges(mesh, face_ids, angle=angle)
        if face_ids and kind == "head_back_neck_preferred":
            preferred = preferred or region_interior_edges(mesh, face_ids)
        explicit.append(ImportantRegion(
            name=name, kind=kind, face_ids=face_ids, protected_edges=protected,
            preferred_edges=preferred, forbidden_smooth_angle=angle,
            smooth_seam_cost=float(r.get("smooth_seam_cost",
                                         _DEFAULT_ZONE_COST.get(kind, 1.0))),
            allow_auxiliary_seams=bool(r.get("allow_auxiliary_seams", kind != "face_front_core")),
            detection="explicit", confidence="high",
            front_axis=config.front_axis, up_axis=config.up_axis))

    regions = list(explicit)
    if need_heuristic or not raw:
        if mode == "face_recovery":
            regions += classify_face_regions(mesh, config, zone_costs=zone_costs)
        else:
            det = detect_face_front(mesh, config)
            if det is not None:
                regions.append(det)

    if not regions:
        return None
    return RegionPolicy(regions=regions, config=config, mode=mode)


def build_region_report(policy: RegionPolicy, mesh: MeshGraph, final_seams, history) -> list[dict]:
    """The ``regions`` block for ``seam_report.json`` (§5.5 / §6.1). Per zone: mandatory vs
    smooth interior seams, the protected-merge removals (face_recovery), and the post-split
    rejects (experimental) — the exact distinction a reviewer needs to answer "why is there a
    seam on the face?" and "what did the policy actually do?"."""
    seams = set(final_seams)
    merges = [h for h in history if h.get("action") == "region_protected_merge"]
    out: list[dict] = []
    for r in policy.regions:
        interior = region_interior_edges(mesh, r.face_ids)
        in_region = interior & seams
        mandatory = {e for e in in_region if mesh.edges[e].dihedral_angle >= FOLD_ANGLE}
        smooth = in_region - mandatory
        rejects = [h for h in history
                   if h.get("reason") == "protected_region_reject" and h.get("region") == r.name]
        rejected_edges = sorted({e for h in rejects for e in h.get("protected_edges_cut", [])})
        zone_merges = sum(h.get("merges", 0) for h in merges) if r.kind in (
            "face_front_core", "face_front") else 0
        if mandatory and smooth:
            status = "protected_with_mandatory_conflicts"
        elif smooth:
            status = "protected_with_smooth_seams"
        elif mandatory:
            status = "protected_mandatory_only"
        else:
            status = "fully_protected"
        out.append({
            **r.to_report(),
            "mode": policy.mode,
            "distortion_priority": r.distortion_priority,
            "smooth_seams_in_region": len(smooth),
            "mandatory_seams_in_region": len(mandatory),
            "protected_merges": zone_merges,
            "rejected_splits": len(rejects),
            "rejected_protected_edges": rejected_edges,
            "status": status,
        })
    return out
