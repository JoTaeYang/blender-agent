"""Topology level presets + LOD batch/comparison (retopology plan §6.2, §10 Phase 4)."""

import pytest

from retopo_agent.levels import (
    LodComparison,
    plan_topology_levels,
    resolve_target_face_count,
)
from retopo_agent.pipeline import generate_lod_set_offline
from retopo_agent.io.fixtures import build_uv_sphere


# -- presets ---------------------------------------------------------------


def test_presets_resolve_100k_to_50_20_10k():
    # plan §6.2 table: a 100k input -> 50k / 20k / 10k.
    assert resolve_target_face_count(100_000, "high_retopo") == 50_000
    assert resolve_target_face_count(100_000, "mid_retopo") == 20_000
    assert resolve_target_face_count(100_000, "low_retopo") == 10_000


def test_custom_level_requires_target():
    assert resolve_target_face_count(100_000, "custom", custom_target=7500) == 7500
    with pytest.raises(ValueError):
        resolve_target_face_count(100_000, "custom")


def test_unknown_level_raises():
    with pytest.raises(ValueError):
        resolve_target_face_count(100_000, "ultra_retopo")


def test_plan_levels_sorted_and_deduped():
    plans = plan_topology_levels(100_000, levels=["low_retopo", "high_retopo", "mid_retopo"])
    # high detail first
    assert [p.target_face_count for p in plans] == [50_000, 20_000, 10_000]
    assert [p.level for p in plans] == ["high_retopo", "mid_retopo", "low_retopo"]


def test_plan_levels_mixes_presets_and_targets_and_dedups():
    plans = plan_topology_levels(100_000, levels=["low_retopo"], targets=[10_000, 5000])
    # low_retopo resolves to 10_000, which collides with the explicit 10_000 target.
    assert [p.target_face_count for p in plans] == [10_000, 5000]


def test_plan_levels_default_is_low_retopo():
    plans = plan_topology_levels(100_000)
    assert len(plans) == 1 and plans[0].level == "low_retopo"


def test_object_suffix():
    (plan,) = plan_topology_levels(100_000, targets=[10_000])
    assert plan.object_suffix() == "LOW_10000"


# -- offline batch + comparison --------------------------------------------


def test_generate_lod_set_offline_three_versions():
    high = build_uv_sphere(segments=80, rings=56)  # ~4400 faces
    comp = generate_lod_set_offline(high, targets=[2000, 1000, 500])
    assert isinstance(comp, LodComparison)
    assert comp.source_face_count == high.face_count
    assert len(comp.entries) == 3

    # Sorted high-detail first, and face counts strictly decrease across LODs.
    actual = [e.actual_face_count for e in comp.entries]
    assert actual == sorted(actual, reverse=True)
    assert actual[0] > actual[-1]

    # Each LOD lands near its target and preserves shape reasonably.
    for e in comp.entries:
        assert e.target_error_ratio <= 0.30
        assert e.surface_distance_mean_ratio < 0.05


def test_comparison_to_dict_shape():
    high = build_uv_sphere(segments=48, rings=32)
    comp = generate_lod_set_offline(high, levels=["mid_retopo", "low_retopo"])
    d = comp.to_dict()
    assert d["lod_count"] == 2
    assert d["source_face_count"] == high.face_count
    assert len(d["lods"]) == 2
    for lod in d["lods"]:
        for key in ("level", "target_face_count", "actual_face_count", "shape_status", "quad_ratio"):
            assert key in lod
