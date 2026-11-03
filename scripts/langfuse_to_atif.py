#!/usr/bin/env python3
"""Convert a Langfuse session into an ATIF v1.7 trajectory file.

    uv run python scripts/langfuse_to_atif.py s_a602a7c7b060 --out runs/atif
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

Langfuse's v2 observations endpoint is the only read path: the session endpoint
that lists a session's traces is deprecated (removal 2026-11-16), so this
script will need a v2 trace listing when it goes.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.parse
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


def _tool_calls(obs: dict[str, Any], step_id: int) -> list[dict[str, Any]]:
    """Promote a generation's `[{"name","arguments"}]` output into ATIF calls."""
    payload = _decode(obs.get("output"))
    if not isinstance(payload, list):
        return []
    calls = []
    for index, part in enumerate(payload, start=1):
        if not isinstance(part, dict) or not part.get("name"):
            continue
        calls.append({
            "tool_call_id": f"call_{step_id}_{index}",
            "function_name": str(part["name"]),
            "arguments": part.get("arguments") or {},
        })
    return calls


def build_trajectory(session_id: str, trace: dict[str, Any],
                     observations: list[dict[str, Any]]) -> dict[str, Any]:
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
            payload = obs.get("output")
            message = "" if calls else _text(payload)
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
            },
        },
    }


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("rigging")
    except PackageNotFoundError:  # pragma: no cover - editable installs always have it
        return "unknown"


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", name)


def main() -> int:
    ap = argparse.ArgumentParser(description="Langfuse session -> ATIF v1.7 trajectory")
    ap.add_argument("target", help="session id, session URL, or trace id")
    ap.add_argument("--out", default="runs/atif", help="output directory (default: runs/atif)")
    ap.add_argument("--stdout", action="store_true", help="print the trajectory instead of writing")
    a = ap.parse_args()

    kind, ident = parse_target(a.target)
    host, headers = credentials()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    with httpx.Client(base_url=host, headers=headers, timeout=60) as client:
        traces = session_traces(client, ident) if kind == "session" else [{"id": ident}]

        written: list[Path] = []
        for trace in traces:
            trace_id = trace["id"]
            observations = trace_observations(client, trace_id)
            doc = build_trajectory(ident if kind == "session" else _session_of(observations, ident),
                                   trace, observations)
            if a.stdout:
                print(json.dumps(doc, indent=2, default=str))
                continue
            suffix = "" if len(traces) == 1 else f"-{trace_id[:8]}"
            path = out / f"atif-{_safe(doc['session_id'])}{suffix}.json"
            path.write_text(json.dumps(doc, indent=2, default=str) + "\n")
            written.append(path)
            print(f"{path}  {len(doc['steps'])} steps  "
                  f"{doc['agent']['name']} / {doc['agent']['model_name']}", file=sys.stderr)

    if written:
        print("upload: open http://localhost:12000 and drop the file in, "
              "or add ?fileUrl=<same-origin-url>", file=sys.stderr)
    return 0


def _session_of(observations: list[dict[str, Any]], fallback: str) -> str:
    for obs in observations:
        value = obs.get("sessionId")
        if value:
            return str(value)
    return fallback


if __name__ == "__main__":
    raise SystemExit(main())
