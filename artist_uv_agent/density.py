"""A7 — density / importance policy (AUTO_ARTIST_UV_PLAN §5.A7).

Default is a single global uniform texel density. Optional, SMALL importance weights may
bias a part's UV area up or down — but only when classification confidence is high, and
never far from 1.0 (plan §5.A7: "do not guess aggressive importance"). The layout packer
(A6) consumes ``weights`` to scale each part's islands before packing; this module also
reports the resulting per-part density and its variance.
"""

from __future__ import annotations

import numpy as np

from artist_uv_agent.classification import PartClass
from artist_uv_agent.descriptors import PartDescriptor

# Near-1.0 importance weights (plan §5.A7). Applied only above CONF_GATE confidence.
CONF_GATE = 0.7
WEIGHT_FRONT_VISIBLE = 1.1     # front-facing visible parts
WEIGHT_DETAIL = 1.2            # face/head/hands or small details
WEIGHT_HIDDEN = 0.8           # hidden / back underside


def density_weights(descriptors: list[PartDescriptor], classes: list[PartClass], *,
                    importance: bool = False) -> dict[int, float]:
    """Per-part texel-density weight (plan §5.A7). With ``importance=False`` (the v1
    default) every part weighs exactly 1.0 — a single uniform density. With
    ``importance=True`` a confident ``detail``/``cap`` part is nudged up to
    ``WEIGHT_DETAIL``; weights stay near 1.0 and only apply above ``CONF_GATE``."""
    class_by = {c.part_id: c for c in classes}
    weights: dict[int, float] = {}
    for d in descriptors:
        w = 1.0
        c = class_by[d.part_id]
        if importance and c.confidence >= CONF_GATE:
            if c.type in ("detail", "cap"):
                w = WEIGHT_DETAIL
        weights[d.part_id] = float(w)
    return weights


def density_report(part_density: dict[int, float], weights: dict[int, float]) -> dict:
    """Report block for the gate / p5_gate.json (plan §5.A7): per-part texel density, its
    variance, and which intentional weights were applied (anything != 1.0)."""
    vals = np.array(list(part_density.values()), dtype=float) if part_density else np.array([0.0])
    intentional = {int(k): round(v, 3) for k, v in weights.items() if abs(v - 1.0) > 1e-6}
    return {
        "per_part_density": {int(k): round(v, 6) for k, v in part_density.items()},
        "density_variance": float(np.var(vals)),
        "density_mean": float(np.mean(vals)),
        "intentional_weights": intentional,
        "uniform": not intentional,
    }
