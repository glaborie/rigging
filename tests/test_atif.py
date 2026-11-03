"""Langfuse observations -> ATIF: the projection has to survive a real viewer.

Fixtures mirror what `/api/public/v2/observations` returns: input/output are JSON
*strings*, tool spans are siblings of the generations, and the API does not
guarantee order. A parse miss here produces a file that uploads and then renders
as empty steps.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "langfuse_to_atif.py"


def _mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("langfuse_to_atif", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _attr(**values: object) -> dict[str, object]:
    return {f"attributes.{k}": str(v) for k, v in values.items()}


def _observations() -> list[dict[str, object]]:
    return [
        {   # root, deliberately last in the list: order comes from startTime
            "id": "root", "type": "AGENT", "parentObservationId": None,
            "startTime": "2026-01-01T00:00:00Z", "sessionId": "s_test",
            "input": json.dumps("Refund ORD-1"),
            "output": json.dumps("Escalated for approval."),
            "metadata": _attr(**{"gen_ai.agent.name": "b2b-support",
                                 "gen_ai.request.model": "test-model",
                                 "rigging.run_id": "r_1", "rigging.status": "ok",
                                 "rigging.steps": 3}),
        },
        {
            "id": "g1", "type": "GENERATION", "parentObservationId": "root",
            "startTime": "2026-01-01T00:00:01Z",
            "input": json.dumps([{"role": "system", "content": "be terse"},
                                 {"role": "user", "content": "Refund ORD-1"}]),
            "output": json.dumps([{"name": "lookup_order",
                                   "arguments": {"order_id": "ORD-1"}}]),
            "metadata": _attr(**{"gen_ai.usage.input_tokens": 100,
                                 "gen_ai.usage.output_tokens": 20,
                                 "gen_ai.response.model": "test-model"}),
        },
        {
            "id": "t1", "type": "TOOL", "parentObservationId": "root",
            "startTime": "2026-01-01T00:00:02Z",
            "input": json.dumps({"order_id": "ORD-1"}),
            "output": json.dumps({"status": "delivered"}),
            "metadata": _attr(**{"gen_ai.tool.name": "lookup_order",
                                 "rigging.tool.is_error": False}),
        },
        {
            "id": "g2", "type": "GENERATION", "parentObservationId": "root",
            "startTime": "2026-01-01T00:00:03Z",
            "output": json.dumps("Escalated for approval."),
            "metadata": _attr(**{"gen_ai.usage.input_tokens": 50,
                                 "gen_ai.usage.output_tokens": 10}),
        },
    ]


def test_steps_follow_atif_semantics() -> None:
    doc = _mod().build_trajectory("s_test", {"id": "tr_1"}, _observations())
    assert doc["schema_version"] == "ATIF-v1.7"
    assert doc["session_id"] == "s_test" and doc["trajectory_id"] == "tr_1"
    assert doc["agent"] == {"name": "b2b-support", "version": doc["agent"]["version"],
                            "model_name": "test-model"}
    assert [s["source"] for s in doc["steps"]] == ["user", "agent", "system", "agent"]
    assert [s["step_id"] for s in doc["steps"]] == [1, 2, 3, 4]
    assert doc["steps"][0]["message"] == "Refund ORD-1"


def test_tool_calls_and_observations_are_correlated_by_call_id() -> None:
    steps = _mod().build_trajectory("s_test", {"id": "tr_1"}, _observations())["steps"]
    call = steps[1]["tool_calls"][0]
    assert call["function_name"] == "lookup_order"
    assert call["arguments"] == {"order_id": "ORD-1"}
    result = steps[2]["observation"]["results"][0]
    assert result["source_call_id"] == call["tool_call_id"]
    assert "delivered" in result["content"]
    assert result["extra"]["tool_name"] == "lookup_order"
    # the call must survive being matched to its result (no aliasing bug)
    assert len(steps[1]["tool_calls"]) == 1


def test_json_string_payloads_are_decoded_not_double_quoted() -> None:
    steps = _mod().build_trajectory("s_test", {"id": "tr_1"}, _observations())["steps"]
    assert steps[3]["message"] == "Escalated for approval."


def test_metrics_are_summed_and_the_system_prompt_is_kept_as_extra() -> None:
    doc = _mod().build_trajectory("s_test", {"id": "tr_1"}, _observations())
    assert doc["final_metrics"]["total_prompt_tokens"] == 150
    assert doc["final_metrics"]["total_completion_tokens"] == 30
    assert doc["final_metrics"]["total_steps"] == 4
    assert doc["extra"]["rigging"]["system_prompt"] == "be terse"


def test_targets_parse_from_ids_and_session_urls() -> None:
    bench = _mod()
    assert bench.parse_target("s_a602a7c7b060") == ("session", "s_a602a7c7b060")
    assert bench.parse_target(
        "https://cloud.langfuse.com/project/p/sessions/s_a602a7c7b060"
    ) == ("session", "s_a602a7c7b060")
    assert bench.parse_target(
        "https://cloud.langfuse.com/project/p/traces/aba62abe"
    ) == ("trace", "aba62abe")
