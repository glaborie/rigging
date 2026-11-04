"""`harness` command line.

v0.1: run, schema, tools. Later milestones add: chat (v0.2), index (v0.3), eval (v0.4),
serve (v0.5), runs. `init` is the manifest wizard: an interview, never a template dump.
Anything not yet built says so and exits 2 — it never pretends.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

from rigging import __version__
from rigging.config import settings
from rigging.events import EventLog
from rigging.manifest import AgentSpec, load_manifest
from rigging.registry import DEFAULT_REGISTRY

NOT_YET: dict[str, str] = {}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="rigging agent harness")
    p.add_argument("--version", action="version", version=f"rigging {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run an agent once")
    run.add_argument("manifest", help="path to agent.yaml")
    run.add_argument("prompt", help="the user message")
    run.add_argument("--model", default=None, help="override the manifest's model")
    run.add_argument("--db", default=None, help="SQLite path (default: $RIGGING_DB_PATH)")
    run.add_argument("--defaults", default=None, help="harness.yaml to merge under the manifest")
    run.add_argument("--session", default=None, help="continue an existing session id")
    run.add_argument("--show-reasoning", action="store_true", help="stream reasoning deltas")
    run.add_argument("--yes", action="store_true", help="approve side-effecting tools (demo only)")
    run.add_argument("--replay", metavar="JSONL", default=None,
                     help="replay a recorded transcript instead of calling the API (offline)")
    run.add_argument("--json", action="store_true", help="print the RunResult as JSON")

    for name, version in NOT_YET.items():
        sp = sub.add_parser(name, help=f"(lands in {version})")
        sp.add_argument("args", nargs="*")

    runs = sub.add_parser("runs", help="list runs and inspect their event trail")
    runs.add_argument("--db", default=None, help="SQLite path (default: $RIGGING_DB_PATH)")
    runs.add_argument("--run", default=None, help="show this run's full event trail")
    runs.add_argument("--limit", type=int, default=10)

    ev = sub.add_parser("eval", help="run an eval suite over event-log predicates")
    ev.add_argument("suite", help="path to suite.yaml")
    ev.add_argument("--db", default=None)
    ev.add_argument("--model", default=None, help="override the manifest model for the suite")
    ev.add_argument("--profile", default=None,
                    help="override the manifest credential_profile (e.g. default for $RIGGING_BASE_URL)")
    ev.add_argument("--only", action="append", default=None, help="run only these case ids")
    ev.add_argument("--export", action="append", default=None,
                    help="override the manifest exporters (repeatable, e.g. --export langfuse)")
    ev.add_argument("--json", action="store_true")

    idx = sub.add_parser("index", help="(re)build the manifest's retrieval index")
    idx.add_argument("manifest")

    cal = sub.add_parser("calibrate", help="score a decision model against the labelled dataset")
    cal.add_argument("model", help="registered decision_model name (e.g. @cf/cloudflare/clef-flash)")
    cal.add_argument("--dataset", default="tests/data/decisions.jsonl", help="labelled JSONL dataset")
    cal.add_argument("--min-interval", type=float, default=0.0,
                     help="minimum seconds between decision requests (pace free tiers)")
    cal.add_argument("--retries", type=int, default=0,
                     help="how many times to retry an HTTP 429 before giving up")

    chat = sub.add_parser("chat", help="interactive REPL: one session, many runs")
    chat.add_argument("manifest")
    chat.add_argument("--db", default=None)
    chat.add_argument("--session", default=None, help="resume a session id")
    chat.add_argument("--model", default=None, help="override the manifest's model")
    chat.add_argument("--profile", default=None,
                      help="override the manifest credential_profile (e.g. default)")
    chat.add_argument("--show-reasoning", action="store_true")

    serve = sub.add_parser("serve", help="web view of the event log; --manifest adds chat")
    serve.add_argument("--db", default=None, help="SQLite path (default: $RIGGING_DB_PATH)")
    serve.add_argument("--manifest", default=None,
                       help="enable chat: path to agent.yaml (needs RIGGING_SERVICE_TOKEN)")
    serve.add_argument("--model", default=None, help="override the manifest's model (chat)")
    serve.add_argument("--profile", default=None,
                       help="override the manifest credential_profile (chat)")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--ssl-certfile", default=None,
                       help="TLS certificate (PEM); with --ssl-keyfile serves https")
    serve.add_argument("--ssl-keyfile", default=None,
                       help="TLS private key (PEM)")
    serve.add_argument("--trusted-proxies", default=None,
                       help="comma-separated peers allowed to set X-Forwarded-For "
                            "(default: 127.0.0.1,::1; never '*')")

    sub.add_parser("schema", help="print the manifest JSON Schema")
    tools = sub.add_parser("tools", help="list registered capabilities")
    tools.add_argument("--kind", default=None, help="tool | guardrail | retriever | decision_model")

    init = sub.add_parser("init", help="interview -> agents/<name>/agent.yaml + prompt.md")
    init.add_argument("--dir", default="agents", help="parent directory for <name>/ (default: agents)")
    init.add_argument("--yes", dest="assume_yes", action="store_true",
                      help="take every suggested default instead of asking")
    init.add_argument("--force", action="store_true",
                      help="overwrite an existing agent.yaml / prompt.md")
    return p


def cmd_run(args: argparse.Namespace) -> int:
    from rigging.approvals import ScriptedApproval
    from rigging.loop import run_agent

    spec: AgentSpec = load_manifest(args.manifest, defaults_path=args.defaults)
    if args.model:
        spec = spec.model_copy(update={"model": args.model})

    def emit(delta: Any) -> None:
        if delta.text:
            sys.stdout.write(delta.text)
            sys.stdout.flush()
        if args.show_reasoning and delta.reasoning:
            sys.stderr.write(f"\033[2m{delta.reasoning}\033[0m")
            sys.stderr.flush()

    log = EventLog(args.db or settings.db_path)
    stream = None if args.json else emit  # --json means machine-readable stdout
    client = None
    if args.replay:
        from rigging.replay import ReplayClient

        client = ReplayClient(args.replay)
        # replay is offline and its transcript is already in the file; streaming a
        # recording through the SSE reassembler would be theatre (and ReplayClient
        # has no .stream())
        stream = None
    result = run_agent(spec, args.prompt, log=log, session_id=args.session,
                       stream=stream,
                       client=client,
                       approval=ScriptedApproval(True) if args.yes or args.replay else None)

    if not args.json:
        print()
        usage = result.usage
        print(f"\n— run {result.run_id} · {result.status} · steps {result.steps} · "
              f"tokens {usage.input_tokens}/{usage.output_tokens} · "
              f"cost ${result.cost_usd if result.cost_usd is not None else 'unknown'}",
              file=sys.stderr)
        if result.error:
            print(f"  {result.error}", file=sys.stderr)
    else:
        print(json.dumps(result.model_dump(), indent=2, default=str))
    return 0 if result.status == "ok" else 1


def cmd_eval(args: argparse.Namespace) -> int:
    from rigging.eval import render, run_suite

    results, suite = run_suite(args.suite, db=args.db, model=args.model, only=args.only,
                               exporters=args.export, profile=args.profile)
    if args.json:
        print(json.dumps({"suite": suite.get("name"), "results": [r.__dict__ for r in results]},
                         indent=2, default=str))
    else:
        print(render(results))
    return 0 if all(r.passed for r in results) else 1


def cmd_index(args: argparse.Namespace) -> int:
    import json as _json
    import sqlite3 as _sq

    from rigging.manifest import load_manifest
    from rigging.types import Chunk

    spec = load_manifest(args.manifest)
    if spec.retriever is None:
        print("manifest has no retriever; nothing to index", file=sys.stderr)
        return 1
    if spec.retriever.name == "fts5":
        db_path = (pathlib.Path(spec.source_path).parent / "demo.db") if spec.source_path else pathlib.Path("demo.db")
        db = _sq.connect(db_path)
        n = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        print(f"fts5: chunks already live in {db_path} ({n} rows) — seed.py wrote them; nothing to do.")
        return 0
    if spec.retriever.name != "qdrant":
        print(f"unknown retriever {spec.retriever.name!r}", file=sys.stderr)
        return 1

    # qdrant: embed the chunks table and upsert (needs ollama + qdrant up)
    db_path = (pathlib.Path(spec.source_path).parent / "demo.db") if spec.source_path else pathlib.Path("demo.db")
    db = _sq.connect(db_path)
    rows = db.execute("SELECT id, text, source, metadata FROM chunks").fetchall()
    chunks = [Chunk(id=r[0], text=r[1], source=r[2], metadata=_json.loads(r[3]) if r[3] else {}) for r in rows]
    from rigging.retriever.qdrant import QdrantRetriever

    r = QdrantRetriever(**{k: v for k, v in spec.retriever.options.items()})
    n = r.index(chunks)
    print(f"qdrant: indexed {n} chunks into {r.collection} at {r.url}")
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    from rigging.chat import chat

    spec = load_manifest(args.manifest)
    if args.model:
        spec = spec.model_copy(update={"model": args.model})
    if args.profile:
        spec = spec.model_copy(update={"credential_profile": args.profile})
    log = EventLog(args.db or settings.db_path)

    def emit(delta: Any) -> None:
        if delta.text:
            sys.stdout.write(delta.text)
            sys.stdout.flush()
        if args.show_reasoning and delta.reasoning:
            sys.stderr.write(f"\033[2m{delta.reasoning}\033[0m")

    return chat(spec, log=log, session_id=args.session, stream=emit)


def cmd_runs(args: argparse.Namespace) -> int:
    import json

    log = EventLog(args.db or settings.db_path)
    if args.run:
        found = False
        for ev in log.events(args.run):
            found = True
            print(f"{ev['seq']:>3}  {ev['type']:<20} {json.dumps(ev['payload'], default=str)[:160]}")
        if not found:
            print(f"no events for run {args.run}", file=sys.stderr)
            return 1
        return 0
    rows = log.runs(limit=args.limit)
    if not rows:
        print("no runs yet", file=sys.stderr)
        return 1
    print(f"{'run_id':<18} {'status':<12} {'model':<22} started")
    for r in rows:
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["started_at"]))
        # A child run (ADR-0019) is indented under the run that spawned it.
        prefix = "  └ " if r.get("parent_run_id") else ""
        print(f"{prefix}{r['run_id']:<16} {r['status']:<12} {r['model'] or '-':<22} {started}")
    print("\ndrill in with: harness runs --run <run_id>")
    return 0


def cmd_schema(_args: argparse.Namespace) -> int:
    from rigging.manifest import SCHEMA_PATH

    print(SCHEMA_PATH.read_text(encoding="utf-8"))
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    import sys as _sys

    from rigging.decide import calibrate as calib  # package init registers decision models

    if args.model not in DEFAULT_REGISTRY.names("decision_model"):
        print(
            f"unknown decision model: {args.model} "
            f"(registered: {', '.join(DEFAULT_REGISTRY.names('decision_model'))})",
            file=_sys.stderr,
        )
        raise SystemExit(2)
    model = DEFAULT_REGISTRY.get("decision_model", args.model)
    try:
        instance = model()
    except (ValueError, TypeError) as exc:
        print(f"cannot start {args.model}: {exc}", file=_sys.stderr)
        raise SystemExit(2) from exc

    cases = calib.load_dataset(args.dataset)
    report = calib.score(
        instance,
        cases,
        min_interval=args.min_interval,
        max_retries=args.retries,
    )
    close = getattr(instance, "close", None)
    if close is not None:
        close()
    print(report.render())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from rigging.service import check_bind, check_trusted_proxies, create_app
    except ModuleNotFoundError:
        print("`harness serve` needs the [service] extra: uv sync --extra service",
              file=sys.stderr)
        return 2
    import uvicorn

    db = args.db or settings.db_path
    if not pathlib.Path(db).exists():
        print(f"no event log at {db} — run an agent first", file=sys.stderr)
        return 2
    runner = None
    if args.manifest:
        from rigging.service.chat import ChatRunner

        spec = load_manifest(args.manifest)
        if args.model:
            spec = spec.model_copy(update={"model": args.model})
        if args.profile:
            spec = spec.model_copy(update={"credential_profile": args.profile})
        if not settings.service_token.strip():
            print("chat needs RIGGING_SERVICE_TOKEN: any page the browser visits can "
                  "POST to a loopback port (ADR-0023). Set a token, or drop --manifest "
                  "for the read-only view.", file=sys.stderr)
            return 2
        runner = ChatRunner(spec, db)
    if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        print("--ssl-certfile and --ssl-keyfile go together", file=sys.stderr)
        return 2
    tls = bool(args.ssl_certfile and args.ssl_keyfile)
    tokens = [t.strip() for t in settings.service_token.split(",") if t.strip()]
    trusted = (args.trusted_proxies if args.trusted_proxies is not None
               else settings.service_trusted_proxies)
    try:
        check_bind(args.host, tokens, tls=tls,
                   allow_plaintext=settings.service_allow_plaintext)
        check_trusted_proxies(trusted)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    scheme = "https" if tls else "http"
    mode = "chat + runs" if runner is not None else "read-only"
    print(f"rigging runs: {scheme}://{args.host}:{args.port}  ({mode}, db={db})")
    if runner is not None:
        print(f"agent: {runner.spec.name} · {runner.spec.model} "
              f"(profile {runner.spec.credential_profile or 'default'})")
    print(f"auth: {'bearer token' if tokens else 'none — loopback only (ADR-0021)'}")
    print("rate limit: "
          + (f"{settings.service_rate_limit}/min per client"
             if settings.service_rate_limit > 0 else "off"))
    print(f"proxy trust: {trusted} (X-Forwarded-For sets the rate-limit key)"
          if trusted.strip() else
          "proxy trust: off — every client shares the socket peer's bucket")
    uvicorn.run(
        create_app(db, tokens=tokens,
                   rate_limit_per_minute=settings.service_rate_limit,
                   runner=runner),
        host=args.host, port=args.port,
        ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile,
        proxy_headers=True, forwarded_allow_ips=trusted,
        log_level="warning",
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from rigging.init_agent import InitError, run_init

    try:
        return run_init(directory=args.dir, assume_yes=args.assume_yes, force=args.force)
    except InitError as exc:
        print(f"harness init: {exc}", file=sys.stderr)
        return 2


def cmd_tools(args: argparse.Namespace) -> int:
    import rigging.decide
    import rigging.guardrails
    import rigging.retriever
    import rigging.tools.core  # noqa: F401

    kinds = [args.kind] if args.kind else ["tool", "guardrail", "retriever", "decision_model", "approval_policy"]
    for kind in kinds:
        names = DEFAULT_REGISTRY.names(kind)
        print(f"{kind}: {', '.join(names) if names else '(none registered)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command in NOT_YET:
        print(f"`harness {args.command}` lands in {NOT_YET[args.command]}. "
              f"See docs/milestones.md.", file=sys.stderr)
        return 2
    handler = {"run": cmd_run, "schema": cmd_schema, "tools": cmd_tools, "runs": cmd_runs, "chat": cmd_chat, "index": cmd_index, "eval": cmd_eval, "calibrate": cmd_calibrate, "serve": cmd_serve, "init": cmd_init}[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
