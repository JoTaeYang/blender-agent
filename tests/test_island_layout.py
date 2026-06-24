"""Blender-free tests for the island-level layout passes
(MVP3_EXISTING_UV_REPACK_OPTIMIZATION_PLAN §2 Goal B). The pure transforms
(density normalize, orientation, density summary) operate on a MeshGraph + UVMap, so
they unit-test without ``bpy`` using the same fixtures as ``test_packing``."""

from __future__ import annotations

import numpy as np

from chart_uv_agent.island_layout import (
    island_density_summary, normalize_island_density, orient_islands,
)
from uv_agent.geometry.projection import project_island
from uv_agent.geometry.solution import UVMap
from uv_agent.io import fixtures
from uv_agent.planner.island_planner import PlanConstraints, plan_islands


def _projected_cylinder():
    mesh = fixtures.build_cylinder(10, 4)
    plan = plan_islands(mesh, constraints=PlanConstraints(padding_px=8, texture_size_px=1024),
                        angle_threshold=20)
    uvm = UVMap.for_mesh(mesh)
    for isl in plan.islands:
        project_island(mesh, isl.face_ids, uvm, isl.projection)
    islands = [isl.face_ids for isl in plan.islands if isl.face_ids]
    return mesh, uvm, islands


def _density_cv(rows) -> float:
    d = np.array([r["density"] for r in rows if r["area_3d"] > 1e-12 and r["density"] > 1e-12])
    if len(d) < 2 or d.mean() <= 1e-12:
        return 0.0
    return float(d.std() / d.mean())


def test_density_summary_has_positive_densities():
    mesh, uvm, islands = _projected_cylinder()
    rows = island_density_summary(mesh, uvm, islands)
    assert rows
    assert all(r["area_3d"] > 0 for r in rows)
    assert any(r["density"] > 0 for r in rows)


def test_density_normalize_reduces_density_variance():
    mesh, uvm, islands = _projected_cylinder()
    # Inflate one island so densities genuinely differ before normalizing.
    loops = [li for fid in islands[0] for li in mesh.faces[fid].loop_indices]
    uvm.uv[loops] *= 1.8
    before = _density_cv(island_density_summary(mesh, uvm, islands))
    normalize_island_density(mesh, uvm, islands)
    after = _density_cv(island_density_summary(mesh, uvm, islands))
    assert after <= before + 1e-9
    assert after < 1e-3  # near-uniform texel density afterward


def test_orient_islands_does_not_lose_loops_or_crash():
    mesh, uvm, islands = _projected_cylinder()
    before = uvm.uv.copy()
    n = orient_islands(mesh, uvm, islands)
    assert n >= 0
    assert uvm.uv.shape == before.shape
    assert np.all(np.isfinite(uvm.uv))
