from uv_agent.agent.llm import MockProvider, get_provider
from uv_agent.agent.pipeline import UVAgentPipeline
from uv_agent.agent.schema import TOOL_NAMES, validate_agent_output
from uv_agent.io import fixtures


def test_cube_accepted_first_iteration():
    result = UVAgentPipeline(MockProvider()).run(fixtures.build_cube(), "unwrap cube")
    assert result.evaluation.status == "accepted"
    assert len(result.history) == 1
    assert result.evaluation.island_count == 6


def test_cylinder_repaired_via_projection_switch():
    result = UVAgentPipeline(MockProvider(), angle_threshold=45).run(
        fixtures.build_cylinder(16, 4), "unwrap this tube"
    )
    assert result.evaluation.status == "accepted"
    # It must have taken at least one repair step.
    assert len(result.history) >= 2
    first_actions = [s["tool"] for s in result.history[0].agent_output["plan"]]
    assert "set_island_projection" in first_actions
    # Final UV must be fold-free and low stretch.
    assert result.evaluation.overlap_ratio == 0.0
    assert result.evaluation.stretch_score < 0.05


def test_solution_roundtrips_to_uvmap():
    result = UVAgentPipeline(MockProvider()).run(fixtures.build_cube())
    m = fixtures.build_cube()
    uvmap = result.solution.to_uvmap(m)
    assert len(uvmap) == len(m.loops)
    # Every loop got a coordinate inside the unit square.
    assert (uvmap.uv >= -1e-6).all() and (uvmap.uv <= 1 + 1e-6).all()


def test_mock_provider_output_is_schema_valid():
    provider = MockProvider()
    agent_input = {
        "evaluation": {"small_island_ratio": 0.0},
        "islands": [
            {"island_id": "island_00", "projection": "planar", "face_count": 12,
             "overlap_ratio": 0.5, "stretch_score": 2.0, "protected": False}
        ],
        "success_criteria": {"stretch_score_max": 0.25},
    }
    out = provider.plan(agent_input)
    assert validate_agent_output(out) == []
    assert out["plan"][0]["tool"] in TOOL_NAMES


def test_get_provider_factory():
    assert get_provider("mock").name == "mock"
    assert get_provider("openai_oauth_local").name == "openai_oauth_local"
    assert get_provider("openai_api_key").name == "openai_api_key"


def test_result_to_dict_serializable():
    import json

    result = UVAgentPipeline(MockProvider(), angle_threshold=45).run(fixtures.build_cylinder(16, 4))
    payload = json.dumps(result.to_dict())  # must not raise
    assert "evaluation" in json.loads(payload)
