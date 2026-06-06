"""LLM providers (plan §8.4 LLM Gateway, §3 provider strategy).

The whole system is abstracted behind :class:`LLMProvider` so the default local
``openai-oauth`` proxy can be swapped for the official OpenAI API or a mock
without touching the pipeline.

Providers:
    * ``mock``        - deterministic rule-based provider; needs no network. Used
                        by tests and as an offline fallback.
    * ``openai_oauth_local`` - default. OpenAI-compatible client pointed at the
                        local proxy (http://127.0.0.1:10531/v1).
    * ``openai_api_key`` - fallback using the official API + OPENAI_API_KEY.
"""

from __future__ import annotations

import json
import os
from typing import Protocol, runtime_checkable

from uv_agent.agent.schema import AGENT_OUTPUT_SCHEMA, validate_agent_output

# Default endpoint for EvanZhouDev/openai-oauth local proxy (plan §3).
DEFAULT_OAUTH_PROXY_BASE_URL = "http://127.0.0.1:10531/v1"

SYSTEM_PROMPT = """\
You are the UV Layout Agent. You DO NOT output raw UV coordinates. You decide a
short plan of structured tool actions; a deterministic geometry solver computes
the actual coordinates, packing and validation.

You receive: the user's request, a mesh summary, the current UV evaluation
(overlap_ratio, stretch_score, packing_efficiency, ...), and per-island metrics.

Reply ONLY with JSON matching the provided schema: {intent, plan, success_criteria}.
Each plan step is {"tool": <name>, "args": {...}, "reason": <short>}.

Guidance:
- High stretch_score or overlap_ratio on a tubular/curved island (many faces,
  planar projection) -> set_island_projection to "cylindrical".
- Residual stretch on a flat island -> relax_island.
- Too many tiny islands (small_island_ratio high) -> merge_islands.
- A region the user said to keep -> protect_region.
- If the result already meets success criteria, return an empty plan with
  intent "accept". If nothing can improve it, use manual_review_required.
"""


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def plan(self, agent_input: dict) -> dict:
        """Return a validated agent output dict {intent, plan, success_criteria}."""
        ...


class MockProvider:
    """Deterministic, network-free provider implementing the repair heuristics.

    It reproduces the decisions a good LLM would make, so the full
    plan->execute->evaluate->repair loop is testable without any API."""

    name = "mock"

    def __init__(self, stretch_threshold: float = 0.25):
        self.stretch_threshold = stretch_threshold

    def plan(self, agent_input: dict) -> dict:
        ev = agent_input.get("evaluation", {})
        islands = agent_input.get("islands", [])
        thr = agent_input.get("success_criteria", {}).get("stretch_score_max", self.stretch_threshold)
        steps: list[dict] = []

        for isl in islands:
            iid = isl["island_id"]
            stretch = isl.get("stretch_score", 0.0)
            overlap = isl.get("overlap_ratio", 0.0)
            projection = isl.get("projection", "planar")
            faces = isl.get("face_count", 0)
            if isl.get("protected"):
                continue
            # Curved/tubular island flattened planar -> switch to cylindrical.
            if projection == "planar" and faces >= 6 and (overlap > 0.05 or stretch > thr):
                steps.append(
                    {
                        "tool": "set_island_projection",
                        "args": {"island_id": iid, "projection": "cylindrical"},
                        "reason": "high distortion on a curved island; cylindrical fits better",
                    }
                )
            # Flat island with residual stretch -> relax.
            elif stretch > thr:
                steps.append(
                    {
                        "tool": "relax_island",
                        "args": {"island_id": iid},
                        "reason": "reduce residual stretch via Laplacian relaxation",
                    }
                )

        if ev.get("small_island_ratio", 0.0) > 0.3:
            steps.append(
                {
                    "tool": "merge_islands",
                    "args": {},
                    "reason": "too many tiny islands; merge the smallest",
                }
            )

        if not steps:
            return {
                "intent": "manual_review_required",
                "plan": [{"tool": "manual_review_required", "args": {}, "reason": "no automatic repair available"}],
                "success_criteria": {"stretch_score_max": thr, "overlap_ratio_max": 0.0},
            }

        return {
            "intent": "repair_uv",
            "plan": steps,
            "success_criteria": {"stretch_score_max": thr, "overlap_ratio_max": 0.0},
        }


class OpenAIProvider:
    """OpenAI-compatible provider. Defaults to the local openai-oauth proxy.

    ``openai`` is imported lazily so the package works without it installed.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        name: str = "openai_oauth_local",
        timeout: float = 60.0,
    ):
        self.base_url = base_url or os.environ.get("UV_AGENT_BASE_URL", DEFAULT_OAUTH_PROXY_BASE_URL)
        # Local proxy ignores the key; a dummy keeps the SDK happy (plan §3).
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "openai-oauth-local")
        self.model = model
        self.name = name
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI  # lazy import
            except ImportError as exc:  # pragma: no cover - only when openai missing
                raise RuntimeError(
                    "openai package not installed. `pip install 'uv-agent[llm]'` "
                    "or use MockProvider."
                ) from exc
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout)
        return self._client

    def plan(self, agent_input: dict) -> dict:
        client = self._get_client()
        resp = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(agent_input, ensure_ascii=False),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        errors = validate_agent_output(data)
        if errors:
            # Degrade gracefully rather than crash the job.
            return {
                "intent": "manual_review_required",
                "plan": [{"tool": "manual_review_required", "args": {"errors": errors}}],
            }
        return data

    # Exposed so callers can pass the schema to other structured-output APIs.
    schema = AGENT_OUTPUT_SCHEMA


def get_provider(name: str = "mock", **kwargs) -> LLMProvider:
    """Factory matching the provider names in plan §8.4."""
    if name in ("mock", "mock_provider"):
        return MockProvider(**kwargs)
    if name in ("openai_oauth_local", "oauth", "local"):
        return OpenAIProvider(name="openai_oauth_local", **kwargs)
    if name in ("openai_api_key", "openai"):
        kwargs.setdefault("base_url", "https://api.openai.com/v1")
        return OpenAIProvider(name="openai_api_key", **kwargs)
    raise ValueError(f"unknown provider: {name}")
