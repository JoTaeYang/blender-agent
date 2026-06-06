"""JSON schema for constrained LLM output (plan §10.2).

The agent must emit a structured ``{intent, plan, success_criteria}`` object.
``plan`` is a list of tool calls whose ``tool`` is one of :data:`TOOL_NAMES`.
This schema is passed to the provider as a ``response_format`` / structured
output spec, and is also used to validate output before dispatch.
"""

from __future__ import annotations

# Canonical action set (plan §7.6) plus MVP extensions used by the solver.
TOOL_NAMES = [
    # Topology (planner) actions
    "split_island",
    "merge_islands",
    "protect_region",
    # Coordinate actions
    "set_island_projection",  # MVP extension: planar <-> cylindrical
    "relax_island",
    # Packing transform actions
    "rotate_island",
    "scale_island",
    "translate_island",
    "repack_all",
    # Misc
    "pin_uv_vertices",
    "manual_review_required",
]

AGENT_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {
            "type": "string",
            "description": "What the agent decided to do, e.g. 'repair_uv' or 'accept'.",
        },
        "plan": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tool": {"type": "string", "enum": TOOL_NAMES},
                    "args": {"type": "object", "additionalProperties": True},
                    "reason": {"type": "string"},
                },
                "required": ["tool", "args"],
            },
        },
        "success_criteria": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "stretch_score_max": {"type": "number"},
                "overlap_ratio_max": {"type": "number"},
            },
        },
    },
    "required": ["intent", "plan"],
}


def validate_agent_output(data: dict) -> list[str]:
    """Lightweight validation (no jsonschema dependency). Returns error strings."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["agent output must be an object"]
    if "intent" not in data or not isinstance(data["intent"], str):
        errors.append("missing or non-string 'intent'")
    plan = data.get("plan")
    if not isinstance(plan, list):
        errors.append("missing or non-list 'plan'")
        return errors
    for i, step in enumerate(plan):
        if not isinstance(step, dict):
            errors.append(f"plan[{i}] is not an object")
            continue
        tool = step.get("tool")
        if tool not in TOOL_NAMES:
            errors.append(f"plan[{i}].tool '{tool}' is not a known tool")
        if "args" in step and not isinstance(step["args"], dict):
            errors.append(f"plan[{i}].args must be an object")
    return errors
