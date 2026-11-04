#!/usr/bin/env python3
"""Convert a Langfuse session into an ATIF v1.7 trajectory file.

    uv run python scripts/langfuse_to_atif.py s_a602a7c7b060 --out runs/atif
    uv run python scripts/langfuse_to_atif.py s_a602a7c7b060 --combine   # one conversation file
    uv run python scripts/langfuse_to_atif.py \
        https://cloud.langfuse.com/project/<project>/sessions/s_a602a7c7b060
    uv run python scripts/langfuse_to_atif.py --trace aba62abeed739799f1385def7999fda1

Why a script and not an exporter: ATIF is a *file* a reviewer uploads into a
trajectory viewer (OpenHands' visualizer, for one). The event log and Langfuse
stay the source of truth; this is a projection, run on demand, with no new
protocol in `rigging/protocols.py` and no vendor import in the loop.

Mapping, following NeMo Relay's ATIF semantics
(https://docs.nvidia.com/nemo/relay/latest/configure-plugins/observability/atif.md):

  * root `invoke_agent` input        -> one `user` step (the prompt)
  * each `chat` generation          -> one `agent` step: message, model, token
                                       metrics, and `tool_calls` promoted from
                                       the response
  * each batch of `execute_tool`    -> one `system` step whose `observation.
    spans that follows a generation    results` carry the tool output, linked by
                                       `source_call_id`

A session is several runs, one per turn, and therefore several traces. The
default is one ATIF file per trace; `--combine` concatenates the session's traces
into one trajectory (step and tool-call ids renumbered, each step tagged with its
`trace_id`), which is what a conversation-level viewer wants.

Langfuse's v2 observations endpoint is the only read path: the session endpoint
that lists a session's traces is deprecated (removal 2026-11-16), so this
script will need a v2 trace listing when it goes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from rigging.config import settings

ATIF_SCHEMA = "ATIF-v1.7"
OBSERVATION_FIELDS = "core,io,metadata"


def credentials() -> tuple[str, dict[str, str]]:
    """Host and Basic-auth header, from the same .env the exporters read."""
    host = (settings.langfuse_host or "https://cloud.langfuse.com").rstrip("/")
    pk, sk = settings.langfuse_public_key, settings.langfuse_secret_key
    if not pk or not sk:
        raise SystemExit("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set "
                         "(the harness reads them from .env)")
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    return host, {"Authorization": f"Basic {auth}"}


def parse_target(target: str) -> tuple[str, str]:
    """Session id, session URL, or (with --trace) trace id -> (kind, id)."""
    if target.startswith(("http://", "https://")):
        path = urllib.parse.urlparse(target).path.rstrip("/")
        kind = "trace" if "/traces/" in path else "session"
        return kind, path.rsplit("/", 1)[-1]
    return ("session" if target.startswith("s_") else "trace"), target


def get(client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Any:
    """GET with a small retry on 429; Langfuse Cloud rate-limits bursts."""
    for attempt in range(4):
        r = client.get(path, params=params)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 401:
            raise SystemExit("Langfuse rejected the keys (401); check .env")
        if r.status_code == 404:
            raise SystemExit(f"not found: {path}")
        r.raise_for_status()
        return r.json()
    raise SystemExit("Langfuse rate-limited the request (429); try again later")


def session_traces(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    data = get(client, f"/api/public/sessions/{session_id}")
    traces = data.get("traces") or []
    if not traces:
        raise SystemExit(f"session {session_id!r} has no traces")
    return traces


def trace_observations(client: httpx.Client, trace_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params = {"traceId": trace_id, "limit": 100, "fields": OBSERVATION_FIELDS}
        if cursor:
            params["cursor"] = cursor
        data = get(client, "/api/public/v2/observations", params)
        rows.extend(data.get("data") or [])
        cursor = (data.get("meta") or {}).get("cursor")
        if not cursor:
            return rows


def _attr(obs: dict[str, Any], name: str) -> Any:
    return (obs.get("metadata") or {}).get(f"attributes.{name}")


def _int(value: Any) -> int | None:
    try:
        return None if value is None or value == "" else int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _decode(value: Any) -> Any:
    """Langfuse stores input/output as JSON strings; decode one layer if it is."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _text(value: Any) -> str:
    value = _decode(value)
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _system_prompt(observations: list[dict[str, Any]]) -> str | None:
    for obs in observations:
        payload = _decode(obs.get("input"))
        if isinstance(payload, list):
            for part in payload:
                if isinstance(part, dict) and part.get("role") == "system":
                    return _text(part.get("content"))
    return None


def _output_parts(payload: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Split a generation output into (text, tool calls).

    rigging records text and calls in one turn as
    `{"content": "...", "tool_calls": [...]}`; a turn with only calls is the bare
    list, and a plain answer is a string.
    """
    payload = _decode(payload)
    if isinstance(payload, dict):
        return payload.get("content"), [p for p in payload.get("tool_calls") or []
                                        if isinstance(p, dict)]
    if isinstance(payload, list):
        if all(isinstance(p, dict) and p.get("name") for p in payload):
            return None, payload
        text = "".join(p.get("text", "") for p in payload
                       if isinstance(p, dict) and p.get("type") == "text")
        return text or None, [p for p in payload if isinstance(p, dict) and p.get("name")]
    return payload, []


def _tool_calls(obs: dict[str, Any], step_id: int) -> list[dict[str, Any]]:
    """Promote a generation's tool calls into ATIF calls, ids included."""
    _, parts = _output_parts(obs.get("output"))
    calls = []
    for index, part in enumerate(parts, start=1):
        if not part.get("name"):
            continue
        calls.append({
            "tool_call_id": f"call_{step_id}_{index}",
            "function_name": str(part["name"]),
            "arguments": part.get("arguments") or {},
        })
    return calls


def build_trajectory(session_id: str, trace: dict[str, Any],
                     observations: list[dict[str, Any]],
                     verdict: dict[str, Any] | None = None) -> dict[str, Any]:
    """Langfuse observations -> one ATIF v1.7 trajectory document."""
    ordered = sorted(observations, key=lambda o: str(o.get("startTime") or ""))
    roots = [o for o in ordered if not o.get("parentObservationId")]
    root = roots[0] if roots else (ordered[0] if ordered else {})
    root_id = root.get("id")
    trace_id = trace.get("id") or root.get("traceId") or ""
    agent_name = _attr(root, "gen_ai.agent.name") or "rigging"
    model_name = _attr(root, "gen_ai.request.model") or _attr(
        next((o for o in ordered if o.get("type") == "GENERATION"), {}),
        "gen_ai.request.model")

    steps: list[dict[str, Any]] = []
    if root.get("input") is not None:
        steps.append({
            "step_id": 1,
            "source": "user",
            "message": _text(root.get("input")),
            "timestamp": root.get("startTime"),
            "extra": {"ancestry": {"function_id": root_id, "parent_id": None}},
        })

    pending: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    def flush_observations() -> None:
        nonlocal results
        if not results:
            return
        steps.append({
            "step_id": len(steps) + 1,
            "source": "system",
            "message": "",
            "timestamp": (results[0].get("extra") or {}).get("timestamp"),
            "observation": {"results": results},
        })
        results = []

    for obs in ordered:
        kind = obs.get("type")
        if obs is root:
            continue
        if kind == "GENERATION":
            flush_observations()
            step_id = len(steps) + 1
            calls = _tool_calls(obs, step_id)
            content, _ = _output_parts(obs.get("output"))
            message = _text(content) if content else ""
            metrics = {
                "prompt_tokens": _int(_attr(obs, "gen_ai.usage.input_tokens")),
                "completion_tokens": _int(_attr(obs, "gen_ai.usage.output_tokens")),
                "cost_usd": _float(obs.get("totalPrice")),
            }
            step: dict[str, Any] = {
                "step_id": step_id,
                "source": "agent",
                "message": message,
                "timestamp": obs.get("startTime"),
                "model_name": _attr(obs, "gen_ai.response.model") or obs.get("modelId"),
                "extra": {"ancestry": {"function_id": obs.get("id"),
                                       "parent_id": obs.get("parentObservationId")}},
            }
            if calls:
                step["tool_calls"] = calls
            if any(v is not None for v in metrics.values()):
                step["metrics"] = {k: v for k, v in metrics.items() if v is not None}
            steps.append(step)
            pending = list(calls)
        elif kind == "TOOL":
            name = _attr(obs, "gen_ai.tool.name")
            match = next((c for c in pending if c["function_name"] == name), None)
            if match is not None:
                pending.remove(match)
            results.append({
                "source_call_id": match["tool_call_id"] if match else None,
                "content": _text(obs.get("output")),
                "extra": {
                    "function_id": obs.get("id"),
                    "parent_id": obs.get("parentObservationId"),
                    "tool_name": name,
                    "is_error": _attr(obs, "rigging.tool.is_error"),
                    "denied": _attr(obs, "rigging.tool.denied"),
                    "timestamp": obs.get("startTime"),
                },
            })
    flush_observations()

    prompts = [s["metrics"]["prompt_tokens"] for s in steps
               if isinstance(s.get("metrics"), dict) and "prompt_tokens" in s["metrics"]]
    completions = [s["metrics"]["completion_tokens"] for s in steps
                   if isinstance(s.get("metrics"), dict) and "completion_tokens" in s["metrics"]]
    costs = [s["metrics"]["cost_usd"] for s in steps
             if isinstance(s.get("metrics"), dict) and "cost_usd" in s["metrics"]]
    final_metrics: dict[str, Any] = {"total_steps": len(steps)}
    if prompts:
        final_metrics["total_prompt_tokens"] = sum(prompts)
    if completions:
        final_metrics["total_completion_tokens"] = sum(completions)
    if costs:
        final_metrics["total_cost_usd"] = sum(costs)

    return {
        "schema_version": ATIF_SCHEMA,
        "session_id": session_id,
        "trajectory_id": trace_id,
        "agent": {
            "name": agent_name,
            "version": _package_version(),
            "model_name": model_name,
        },
        "steps": steps,
        "notes": (f"Converted from Langfuse session {session_id}, trace {trace_id}. "
                  "Steps follow NeMo Relay's ATIF semantics: user prompt, agent "
                  "generations with promoted tool calls, system observation steps."),
        "final_metrics": final_metrics,
        "extra": {
            "langfuse": {
                "session_id": session_id,
                "trace_id": trace_id,
                "trace_name": trace.get("name"),
                "tags": trace.get("tags") or [],
                "project_id": trace.get("projectId"),
            },
            "rigging": {
                "run_id": _attr(root, "rigging.run_id"),
                "spec_hash": _attr(root, "rigging.spec_hash"),
                "status": _attr(root, "rigging.status"),
                "steps": _int(_attr(root, "rigging.steps")),
                "handoff": _attr(root, "rigging.handoff"),
                "system_prompt": _system_prompt(ordered),
                **({"eval": verdict} if verdict else {}),
            },
        },
    }


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("rigging")
    except PackageNotFoundError:  # pragma: no cover - editable installs always have it
        return "unknown"


def _doc_start(doc: dict[str, Any]) -> str:
    times = [str(s.get("timestamp")) for s in doc.get("steps") or [] if s.get("timestamp")]
    return min(times) if times else ""


def combine_trajectories(docs: list[dict[str, Any]],
                         session_id: str) -> dict[str, Any]:
    """One ATIF trajectory for a conversation: its traces, in start order.

    A rigging session is several runs (one per turn), and therefore several
    Langfuse traces. ATIF has no trace container, so the steps are concatenated
    and every id is renumbered. The run boundaries stay visible: each step's
    `extra.trace_id` names its trace, and `extra.rigging.runs` keeps one entry
    per run (including its eval verdict).
    """
    ordered = sorted(docs, key=_doc_start)
    steps: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    models: list[str] = []
    system_prompt: str | None = None
    totals: dict[str, float] = {}

    for index, doc in enumerate(ordered, start=1):
        mapping: dict[str, str] = {}
        for step in doc.get("steps") or []:
            new = {**step}
            if step.get("tool_calls"):
                new["tool_calls"] = []
                for call in step["tool_calls"]:
                    old_id = str(call.get("tool_call_id"))
                    renamed = f"t{index}:{old_id}"
                    mapping[old_id] = renamed
                    new["tool_calls"].append({**call, "tool_call_id": renamed})
            if step.get("observation"):
                new["observation"] = {
                    **step["observation"],
                    "results": [
                        {**result,
                         "source_call_id": mapping.get(str(result.get("source_call_id")),
                                                       result.get("source_call_id"))}
                        for result in step["observation"].get("results") or []
                    ],
                }
            new["step_id"] = len(steps) + 1
            new["extra"] = {**(step.get("extra") or {}),
                            "trace_id": doc.get("trajectory_id")}
            steps.append(new)

        extra = doc.get("extra") or {}
        rigging = extra.get("rigging") or {}
        run = {key: rigging[key] for key in ("run_id", "status", "steps", "handoff")
               if rigging.get(key) is not None}
        if "eval" in rigging:
            run["eval"] = rigging["eval"]
        runs.append(run)
        langfuse = extra.get("langfuse") or {}
        traces.append({key: langfuse[key]
                       for key in ("trace_id", "trace_name", "tags")
                       if langfuse.get(key) is not None})
        if system_prompt is None and rigging.get("system_prompt"):
            system_prompt = str(rigging["system_prompt"])
        model = str((doc.get("agent") or {}).get("model_name") or "")
        if model and model not in models:
            models.append(model)
        for key in ("total_prompt_tokens", "total_completion_tokens", "total_cost_usd"):
            value = (doc.get("final_metrics") or {}).get(key)
            if value is not None:
                totals[key] = totals.get(key, 0.0) + float(value)

    first_agent = (ordered[0].get("agent") if ordered else {}) or {}
    return {
        "schema_version": ATIF_SCHEMA,
        "session_id": session_id,
        "trajectory_id": session_id,
        "agent": {
            "name": first_agent.get("name") or "rigging",
            "version": first_agent.get("version") or _package_version(),
            "model_name": models[0] if models else first_agent.get("model_name"),
        },
        "steps": steps,
        "notes": (
            f"One conversation: {len(ordered)} runs/traces from Langfuse session "
            f"{session_id}, concatenated in start order. Each step's extra.trace_id "
            "names the run it came from; extra.rigging.runs lists the runs."),
        "final_metrics": {"total_steps": len(steps), **totals},
        "extra": {
            "langfuse": {"session_id": session_id, "traces": traces},
            "rigging": {
                "runs": runs,
                "system_prompt": system_prompt,
                "models": models,
            },
        },
    }


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", name)


def load_verdicts(db: Path) -> dict[str, dict[str, Any]]:
    """run_id -> the eval's `eval_verdict` event, read-only.

    The verdict cannot ride on the root span: the eval only knows pass/fail once
    the run has returned, which is after the span closed. So it is an event, and
    the exporter joins it by the run id the trace already carries.
    """
    import sqlite3

    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT run_id, payload FROM run_events WHERE type='eval_verdict' ORDER BY seq"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return {}
    verdicts: dict[str, dict[str, Any]] = {}
    for run_id, payload in rows:
        try:
            verdicts[str(run_id)] = json.loads(payload)
        except json.JSONDecodeError:
            continue
    return verdicts


def write_harbor_layout(trials: list[tuple[str, dict[str, Any]]], out: Path,
                        *, job: str = "rigging") -> Path:
    """Lay the trajectories out as a Harbor job, which is what `harbor view` reads.

    Harbor's viewer does not take a bare ATIF file: it lists jobs from
    `<jobs>/<job>/result.json` and trials from `<job>/<trial>/result.json`, then
    fetches `<job>/<trial>/agent/trajectory.json`. The shape below is verified
    against harbor 0.25.0 (its pydantic models fill every field not listed).
    """
    job_dir = out / job
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "config.json").write_text("{}\n")

    results: dict[str, dict[str, Any]] = {}
    for existing in job_dir.glob("*/result.json"):
        try:
            row = json.loads(existing.read_text())
            results[row["trial_name"]] = row
        except (json.JSONDecodeError, KeyError):
            continue  # a half-written trial must not break the job listing

    now = datetime.now(UTC).isoformat()
    for trial_name, doc in trials:
        trial_dir = job_dir / trial_name
        (trial_dir / "agent").mkdir(parents=True, exist_ok=True)
        trajectory = trial_dir / "agent" / "trajectory.json"
        trajectory.write_text(json.dumps(doc, indent=2, default=str) + "\n")
        task_path = str(trial_dir.resolve())
        (trial_dir / "config.json").write_text(
            json.dumps({"task": {"path": task_path}}, indent=2) + "\n")
        model = doc.get("agent", {}).get("model_name")
        provider, _, short = (model or "").partition("/")
        model_info = ({"name": short or model, "provider": provider or None}
                      if model else None)
        results[trial_name] = {
            "id": str(uuid.uuid4()),
            "task_name": doc.get("agent", {}).get("name") or "rigging",
            "trial_name": trial_name,
            "trial_uri": trial_dir.resolve().as_uri(),
            "task_id": {"path": task_path},
            "source": "rigging",
            "task_checksum": hashlib.sha256(trajectory.read_bytes()).hexdigest(),
            "config": {"task": {"path": task_path}},
            "agent_info": {"name": "rigging",
                           "version": doc.get("agent", {}).get("version"),
                           "model_info": model_info},
            "started_at": now,
            "finished_at": now,
        }
        verdict = ((doc.get("extra") or {}).get("rigging") or {}).get("eval")
        if verdict is not None:
            # Harbor's Overview/Pareto read verifier_result.rewards; this is the
            # eval's own pass/fail, not an invented score.
            results[trial_name]["verifier_result"] = {
                "rewards": {"reward": 1.0 if verdict.get("passed") else 0.0},
            }
        (trial_dir / "result.json").write_text(
            json.dumps(results[trial_name], indent=2) + "\n")

    (job_dir / "result.json").write_text(json.dumps({
        "id": str(uuid.uuid4()),
        "started_at": now,
        "finished_at": now,
        "n_total_trials": len(results),
        "stats": {"n_completed_trials": len(results)},
        "trial_results": list(results.values()),
    }, indent=2) + "\n")
    rows = "\n".join(f"| `{name}` | {row.get('task_name')} |"
                     for name, row in sorted(results.items()))
    (job_dir / "README.md").write_text(
        f"# Harbor job `{job}`\n\n"
        "Generated by `scripts/langfuse_to_atif.py`; safe to delete.\n\n"
        "```bash\n"
        f"harbor view {job_dir.parent.resolve()} --port 8080\n"
        f"# http://127.0.0.1:8080/jobs/{job}/trials/<trial>\n"
        "```\n\n"
        "Open the trial URL directly, or the job's **Rollouts** tab: these trials\n"
        "have no verifier rewards, so **Overview** and **Pareto** render empty.\n\n"
        "`harbor view` lists trials from `<trial>/result.json` and reads\n"
        "`<trial>/agent/trajectory.json` (ATIF v1.7).\n\n"
        "| Trial | Task |\n|---|---|\n" + rows + "\n")
    return job_dir


def main() -> int:
    ap = argparse.ArgumentParser(description="Langfuse session -> ATIF v1.7 trajectory")
    ap.add_argument("target", help="session id, session URL, or trace id")
    ap.add_argument("--out", default="runs/atif", help="output directory (default: runs/atif)")
    ap.add_argument("--stdout", action="store_true", help="print the trajectory instead of writing")
    ap.add_argument("--harbor", metavar="DIR",
                    help="also write a Harbor job layout under DIR, for `harbor view DIR`")
    ap.add_argument("--combine", action="store_true",
                    help="one trajectory for the whole session instead of one file per trace")
    ap.add_argument("--db", default="runs/eval.db",
                    help="eval event log to read verdicts from (default: runs/eval.db)")
    a = ap.parse_args()

    kind, ident = parse_target(a.target)
    host, headers = credentials()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    verdicts = load_verdicts(Path(a.db))

    with httpx.Client(base_url=host, headers=headers, timeout=60) as client:
        trace_refs = session_traces(client, ident) if kind == "session" else [{"id": ident}]
        docs: list[dict[str, Any]] = []
        for trace in trace_refs:
            trace_id = trace["id"]
            observations = trace_observations(client, trace_id)
            run_id = next((_attr(o, "rigging.run_id") for o in observations
                           if not o.get("parentObservationId")), None)
            verdict = verdicts.get(str(run_id)) if run_id else None
            docs.append(build_trajectory(
                ident if kind == "session" else _session_of(observations, ident),
                trace, observations, verdict=verdict))

    if a.combine and len(docs) > 1:
        docs = [combine_trajectories(docs, docs[0]["session_id"])]

    if a.stdout:
        for doc in docs:
            print(json.dumps(doc, indent=2, default=str))
        return 0

    written: list[Path] = []
    trials: list[tuple[str, dict[str, Any]]] = []
    for doc in docs:
        suffix = "" if len(docs) == 1 else f"-{doc['trajectory_id'][:8]}"
        name = f"atif-{_safe(doc['session_id'])}{suffix}"
        path = out / f"{name}.json"
        path.write_text(json.dumps(doc, indent=2, default=str) + "\n")
        written.append(path)
        trials.append((name.removeprefix("atif-"), doc))
        print(f"{path}  {len(doc['steps'])} steps  "
              f"{doc['agent']['name']} / {doc['agent']['model_name']}", file=sys.stderr)

    if len(docs) > 1 and not a.combine:
        print(f"note: {len(docs)} traces -> {len(docs)} files for this session; "
              "add --combine (or make atif COMBINE=1) for one conversation trajectory",
              file=sys.stderr)

    if written:
        print("upload: open http://localhost:12000 and drop the file in, "
              "or add ?fileUrl=<same-origin-url>", file=sys.stderr)
    if a.harbor and trials:
        job_dir = write_harbor_layout(trials, Path(a.harbor))
        trial_name = trials[0][0]
        if not verdicts:
            print(f"note: no eval verdicts in {a.db}; the Harbor trial carries no "
                  "verifier_result (Overview and Pareto stay empty)", file=sys.stderr)
        print(f"harbor: {job_dir}\n"
              f"  harbor view {job_dir.parent} --port 8080\n"
              f"  http://127.0.0.1:8080/jobs/{job_dir.name}/trials/{trial_name}",
              file=sys.stderr)
    return 0


def _session_of(observations: list[dict[str, Any]], fallback: str) -> str:
    for obs in observations:
        value = obs.get("sessionId")
        if value:
            return str(value)
    return fallback


if __name__ == "__main__":
    raise SystemExit(main())
