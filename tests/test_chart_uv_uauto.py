"""P5 engine selection (GENERIC_UV_REVISION_PLAN §G1).

The generic product path must NOT enter the reference-guided transfer engine
implicitly. ``--uv-engine auto`` resolves to ``chart`` unconditionally — even when
the reference carries UV layers — and ``--uv-engine transfer`` still routes to the
explicit reference-assisted engine (which fails loud, never silently, without UVs).

These are Blender-free: we load the worker module (it imports only stdlib at top)
and stub the three engine dispatchers to record which one ``run_p5_uv`` calls.
"""

import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKER = os.path.join(os.path.dirname(_HERE), "worker", "run_quad_retopo_job.py")


def _load_worker():
    spec = importlib.util.spec_from_file_location("rqr_worker", _WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _UVLayers(list):
    """Minimal stand-in for ``obj.data.uv_layers`` — only ``len()`` is consulted."""


class _FakeRef:
    def __init__(self, n_uv_layers):
        self.data = type("D", (), {"uv_layers": _UVLayers(range(n_uv_layers))})()


def _patch_dispatch(mod, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "_run_p5_transfer",
                        lambda *a, **k: (calls.append("transfer"), {"engine": "transfer"})[1])
    monkeypatch.setattr(mod, "_run_p5_chart",
                        lambda *a, **k: (calls.append("chart"), {"engine": "chart"})[1])
    monkeypatch.setattr(mod, "_run_p5_organic",
                        lambda *a, **k: (calls.append("organic"), {"engine": "organic"})[1])
    monkeypatch.setattr(mod, "_run_p5_artist",
                        lambda *a, **k: (calls.append("artist"), {"engine": "artist"})[1])
    return calls


def test_auto_uses_chart_even_when_reference_has_uvs(monkeypatch):
    mod = _load_worker()
    calls = _patch_dispatch(mod, monkeypatch)
    ref_with_uv = _FakeRef(n_uv_layers=3)
    out = mod.run_p5_uv(None, object(), ref_with_uv, "/tmp/out", engine="auto")
    assert calls == ["chart"]
    assert out["engine"] == "chart"


def test_auto_uses_chart_without_reference(monkeypatch):
    mod = _load_worker()
    calls = _patch_dispatch(mod, monkeypatch)
    out = mod.run_p5_uv(None, object(), _FakeRef(n_uv_layers=0), "/tmp/out", engine="auto")
    assert calls == ["chart"]
    assert out["engine"] == "chart"


def test_explicit_transfer_still_routes_to_transfer(monkeypatch):
    mod = _load_worker()
    calls = _patch_dispatch(mod, monkeypatch)
    out = mod.run_p5_uv(None, object(), _FakeRef(n_uv_layers=2), "/tmp/out", engine="transfer")
    assert calls == ["transfer"]
    assert out["engine"] == "transfer"


def test_explicit_organic_still_routes_to_organic(monkeypatch):
    mod = _load_worker()
    calls = _patch_dispatch(mod, monkeypatch)
    out = mod.run_p5_uv(None, object(), _FakeRef(n_uv_layers=1), "/tmp/out", engine="organic")
    assert calls == ["organic"]
    assert out["engine"] == "organic"


def test_explicit_artist_routes_to_artist(monkeypatch):
    """The artist engine is explicit-only (NOT the default); ``auto`` still resolves to
    chart (AUTO_ARTIST_UV_PLAN §8)."""
    mod = _load_worker()
    calls = _patch_dispatch(mod, monkeypatch)
    out = mod.run_p5_uv(None, object(), _FakeRef(n_uv_layers=0), "/tmp/out", engine="artist")
    assert calls == ["artist"]
    assert out["engine"] == "artist"


def test_auto_does_not_route_to_artist(monkeypatch):
    mod = _load_worker()
    calls = _patch_dispatch(mod, monkeypatch)
    mod.run_p5_uv(None, object(), _FakeRef(n_uv_layers=2), "/tmp/out", engine="auto")
    assert calls == ["chart"]


def test_transfer_fails_loud_without_reference_uvs():
    """Explicit transfer with a UV-less reference must raise NoReferenceUVError, NOT
    silently fall back to chart (GENERIC_UV_REVISION_PLAN §6). Exercises the real
    ``_run_p5_transfer`` guard (no monkeypatch); it raises before touching Blender."""
    mod = _load_worker()
    from transfer_uv_agent.pipeline import NoReferenceUVError
    with pytest.raises(NoReferenceUVError):
        mod.run_p5_uv(None, object(), _FakeRef(n_uv_layers=0), "/tmp/out", engine="transfer")
    with pytest.raises(NoReferenceUVError):
        mod.run_p5_uv(None, object(), None, "/tmp/out", engine="transfer")
