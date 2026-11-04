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


def test_harbor_layout_matches_what_harbor_view_reads(tmp_path) -> None:
    """`harbor view` lists trials from result.json and reads agent/trajectory.json."""
    import hashlib

    mod = _mod()
    doc = mod.build_trajectory("s_test", {"id": "tr_1"}, _observations())
    job = mod.write_harbor_layout([("s_test", doc)], tmp_path / "harbor")

    trajectory = job / "s_test" / "agent" / "trajectory.json"
    assert json.loads(trajectory.read_text())["schema_version"] == "ATIF-v1.7"
    result = json.loads((job / "s_test" / "result.json").read_text())
    assert result["trial_name"] == "s_test"
    assert result["agent_info"]["name"] == "rigging"
    assert result["task_checksum"] == hashlib.sha256(trajectory.read_bytes()).hexdigest()

    job_result = json.loads((job / "result.json").read_text())
    assert job_result["n_total_trials"] == 1
    assert job_result["stats"]["n_completed_trials"] == 1

    readme = (job / "README.md").read_text()
    assert "harbor view" in readme and "s_test" in readme


def test_a_second_session_joins_the_same_job_without_losing_the_first(tmp_path) -> None:
    mod = _mod()
    job = mod.write_harbor_layout([("s_one", mod.build_trajectory("s_one", {"id": "a"}, _observations()))],
                                  tmp_path / "harbor")
    job = mod.write_harbor_layout([("s_two", mod.build_trajectory("s_two", {"id": "b"}, _observations()))],
                                  job.parent)
    job_result = json.loads((job / "result.json").read_text())
    assert job_result["n_total_trials"] == 2
    assert sorted(r["trial_name"] for r in job_result["trial_results"]) == ["s_one", "s_two"]
    assert (job / "s_one" / "agent" / "trajectory.json").exists()


def test_eval_verdict_becomes_a_harbor_verifier_result(tmp_path) -> None:
    """Harbor's Overview/Pareto read verifier_result.rewards — from the eval, not invented."""
    mod = _mod()
    verdict = {"case": "refund-in-window", "passed": True, "failures": []}
    doc = mod.build_trajectory("s_test", {"id": "tr_1"}, _observations(), verdict=verdict)
    assert doc["extra"]["rigging"]["eval"] == verdict

    job = mod.write_harbor_layout([("s_test", doc)], tmp_path / "harbor")
    result = json.loads((job / "s_test" / "result.json").read_text())
    assert result["verifier_result"]["rewards"]["reward"] == 1.0


def test_a_failed_case_exports_zero(tmp_path) -> None:
    mod = _mod()
    verdict = {"case": "c1", "passed": False, "failures": ["tool 'refund' was called"]}
    doc = mod.build_trajectory("s_test", {"id": "tr_1"}, _observations(), verdict=verdict)
    job = mod.write_harbor_layout([("s_test", doc)], tmp_path / "harbor")
    result = json.loads((job / "s_test" / "result.json").read_text())
    assert result["verifier_result"]["rewards"]["reward"] == 0.0


def test_no_verdict_means_no_verifier_result(tmp_path) -> None:
    mod = _mod()
    doc = mod.build_trajectory("s_test", {"id": "tr_1"}, _observations())
    job = mod.write_harbor_layout([("s_test", doc)], tmp_path / "harbor")
    result = json.loads((job / "s_test" / "result.json").read_text())
    assert "verifier_result" not in result


def test_verdicts_are_read_from_the_event_log_by_run_id(tmp_path) -> None:
    from rigging.events import EventLog

    mod = _mod()
    log = EventLog(tmp_path / "eval.db")
    log.append("r_1", "eval_verdict", {"case": "c1", "passed": False, "failures": ["x"]})
    verdicts = mod.load_verdicts(tmp_path / "eval.db")
    assert verdicts["r_1"]["passed"] is False
    assert mod.load_verdicts(tmp_path / "missing.db") == {}


def test_combining_a_session_keeps_one_trajectory_step_order_and_ids() -> None:
    mod = _mod()
    first = mod.build_trajectory("s_test", {"id": "tr_a"}, _observations())
    second = mod.build_trajectory(
        "s_test", {"id": "tr_b"}, _observations(),
        verdict={"case": "c2", "passed": True, "failures": []})
    doc = mod.combine_trajectories([first, second], "s_test")

    assert doc["trajectory_id"] == "s_test"
    assert doc["schema_version"] == "ATIF-v1.7"
    assert [s["step_id"] for s in doc["steps"]] == list(range(1, 9))
    assert [s["extra"]["trace_id"] for s in doc["steps"]] == ["tr_a"] * 4 + ["tr_b"] * 4

    # Call ids are renumbered per trace, and every result still points at its call.
    calls = {c["tool_call_id"] for s in doc["steps"] for c in s.get("tool_calls") or []}
    assert len(calls) == 2
    results = [r for s in doc["steps"]
               for r in (s.get("observation") or {}).get("results") or []]
    assert len(results) == 2
    assert {r["source_call_id"] for r in results} == calls

    assert doc["final_metrics"]["total_steps"] == 8
    assert doc["final_metrics"]["total_prompt_tokens"] == 300
    assert doc["final_metrics"]["total_completion_tokens"] == 60

    # One entry per run, in order, with that run's eval verdict.
    runs = doc["extra"]["rigging"]["runs"]
    assert [r["run_id"] for r in runs] == ["r_1", "r_1"]
    assert "eval" not in runs[0] and runs[1]["eval"]["case"] == "c2"
    assert doc["extra"]["rigging"]["models"] == ["test-model"]
    assert doc["extra"]["langfuse"]["traces"][0]["trace_id"] == "tr_a"


def test_a_combined_document_survives_the_harbor_layout(tmp_path) -> None:
    mod = _mod()
    docs = [mod.build_trajectory("s_test", {"id": "tr_a"}, _observations()),
            mod.build_trajectory("s_test", {"id": "tr_b"}, _observations())]
    job = mod.write_harbor_layout([("s_test", mod.combine_trajectories(docs, "s_test"))],
                                  tmp_path / "harbor")
    trajectory = json.loads((job / "s_test" / "agent" / "trajectory.json").read_text())
    assert len(trajectory["steps"]) == 8


def test_text_and_tool_calls_in_one_generation() -> None:
    """rigging records a text+calls turn as {"content", "tool_calls"}."""
    mod = _mod()
    obs = _observations()
    obs[1]["output"] = json.dumps({
        "content": "Let me check that order.",
        "tool_calls": [{"name": "lookup_order", "arguments": {"order_id": "ORD-1"}}],
    })
    steps = mod.build_trajectory("s_test", {"id": "tr_1"}, obs)["steps"]
    assert steps[1]["message"] == "Let me check that order."
    assert steps[1]["tool_calls"][0]["function_name"] == "lookup_order"
    assert steps[2]["observation"]["results"][0]["source_call_id"] == steps[1]["tool_calls"][0]["tool_call_id"]
