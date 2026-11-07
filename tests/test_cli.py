"""`harness run` takes the same model/profile overrides as the other commands.

The demo manifest names one model and one credential profile; a reviewer should
be able to try another pair without editing YAML (`make` passes MODEL= and
PROFILE= through, and `harness run` must accept them directly).
"""

from __future__ import annotations

import pathlib
from contextlib import nullcontext

from rigging import cli
from rigging.manifest import load_manifest
from rigging.types import RunResult

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_run_applies_model_and_profile_overrides(monkeypatch):
    spec = load_manifest(str(ROOT / "examples/hello/agent.yaml"))
    seen = {}

    monkeypatch.setattr(cli, "load_manifest", lambda *a, **k: spec)
    monkeypatch.setattr(cli, "EventLog", lambda *a, **k: nullcontext())

    def fake_run_agent(spec, prompt, **kwargs):
        seen["spec"] = spec
        return RunResult(run_id="r_test", status="ok")

    monkeypatch.setattr("rigging.loop.run_agent", fake_run_agent)

    argv = ["run", "agent.yaml", "hi", "--model", "deepseek/deepseek-v4.1-flash",
            "--profile", "openrouter", "--export", "langfuse"]
    assert cli.main(argv) == 0

    assert seen["spec"].model == "deepseek/deepseek-v4.1-flash"
    assert seen["spec"].credential_profile == "openrouter"
    assert seen["spec"].observability.exporters == ["langfuse"]
    assert seen["spec"].observability.capture_content is True
    assert spec.credential_profile != "openrouter", "the loaded spec is not mutated"
    assert spec.observability.exporters == [], "exporters are not written back to the manifest"


def test_chat_and_serve_accept_the_export_flag():
    parser = cli._parser()
    assert parser.parse_args(["chat", "a.yaml", "--export", "langfuse"]).export == ["langfuse"]
    assert parser.parse_args(
        ["serve", "--manifest", "a.yaml", "--export", "langfuse"]).export == ["langfuse"]


def test_serve_verbose_is_off_by_default_and_accepts_short_form():
    parser = cli._parser()
    assert parser.parse_args(["serve"]).verbose is False
    assert parser.parse_args(["serve", "-v"]).verbose is True
    assert parser.parse_args(["serve", "--verbose"]).verbose is True


def test_serve_openai_api_is_opt_in():
    parser = cli._parser()
    assert parser.parse_args(["serve"]).openai_api is False
    assert parser.parse_args(["serve", "--manifest", "a.yaml",
                              "--openai-api"]).openai_api is True


def test_serve_openai_api_needs_the_manifest_it_serves(tmp_path, capsys):
    """The surface is one agent, so there is nothing to serve without a manifest."""
    db = tmp_path / "e.db"
    db.write_text("")
    assert cli.main(["serve", "--db", str(db), "--openai-api"]) == 2
    assert "--openai-api serves an agent" in capsys.readouterr().err


def test_serve_verbose_raises_the_uvicorn_log_level(tmp_path, monkeypatch, capsys):
    """The access log is uvicorn's, on stdout; log_level is the whole switch."""
    import uvicorn

    import rigging.service as service

    db = tmp_path / "e.db"
    db.write_text("")
    seen: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(service, "check_bind", lambda *a, **k: None)
    monkeypatch.setattr(service, "check_trusted_proxies", lambda *a, **k: None)
    monkeypatch.setattr(service, "create_app", lambda *a, **k: object())

    assert cli.main(["serve", "--db", str(db)]) == 0
    assert seen["log_level"] == "warning", "the page stays quiet by default"
    assert "access log" not in capsys.readouterr().out

    seen.clear()
    assert cli.main(["serve", "--db", str(db), "-v"]) == 0
    assert seen["log_level"] == "info"
    assert "access log: on" in capsys.readouterr().out


def test_export_is_repeatable_and_untouched_when_absent(monkeypatch):
    spec = load_manifest(str(ROOT / "examples/b2b-support/agent.yaml"))
    seen = {}

    monkeypatch.setattr(cli, "load_manifest", lambda *a, **k: spec)
    monkeypatch.setattr(cli, "EventLog", lambda *a, **k: nullcontext())

    def fake_run_agent(spec, prompt, **kwargs):
        seen.setdefault("specs", []).append(spec)
        return RunResult(run_id="r_test", status="ok")

    monkeypatch.setattr("rigging.loop.run_agent", fake_run_agent)

    assert cli.main(["run", "agent.yaml", "hi", "--export", "console",
                     "--export", "mlflow"]) == 0
    assert seen["specs"][0].observability.exporters == ["console", "mlflow"]

    assert cli.main(["run", "agent.yaml", "hi"]) == 0
    assert seen["specs"][1].observability.exporters == [], \
        "the demo manifest stays key-free unless asked"


def test_run_without_overrides_keeps_the_manifest_pair(monkeypatch):
    spec = load_manifest(str(ROOT / "examples/hello/agent.yaml"))
    seen = {}

    monkeypatch.setattr(cli, "load_manifest", lambda *a, **k: spec)
    monkeypatch.setattr(cli, "EventLog", lambda *a, **k: nullcontext())

    def fake_run_agent(spec, prompt, **kwargs):
        seen["spec"] = spec
        return RunResult(run_id="r_test", status="ok")

    monkeypatch.setattr("rigging.loop.run_agent", fake_run_agent)

    assert cli.main(["run", "agent.yaml", "hi"]) == 0
    assert seen["spec"].model == spec.model
    assert seen["spec"].credential_profile == spec.credential_profile

# --------------------------------------------------- pending approvals (ADR-0029)

def test_cli_approvals_lists_the_queue(tmp_path, capsys):
    """`harness approvals` is the same projection the service serves."""
    import json as _json
    import pathlib as _pathlib

    from rigging import cli
    from rigging.config import settings
    from rigging.events import EventLog
    from rigging.loop import run_agent
    from rigging.manifest import AgentSpec, ApprovalCfg, Limits
    from rigging.registry import Registry
    from rigging.testing import FakeClient

    def refund(amount: int) -> str:
        """Refund money."""
        return f"refunded {amount}"

    refund.__rigging_tool__ = {"name": "refund", "description": "Refund money.",
                               "side_effecting": True}
    registry = Registry()
    registry.add("tool", "refund", refund)

    spec = AgentSpec(name="t", model="fake", tools=["refund"],
                     approval=ApprovalCfg(policy="deferred"), limits=Limits(max_steps=3))
    client = FakeClient([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": "refund", "arguments": {"amount": 10}}]},
        {"role": "assistant", "content": "done"},
    ])
    db = tmp_path / "r.db"
    with EventLog(db) as log:
        result = run_agent(spec, "refund 10", registry=registry, client=client, log=log)
    pending = result.pending
    assert pending is not None

    assert cli.main(["approvals", "--db", str(db), "--json"]) == 0
    rows = _json.loads(capsys.readouterr().out)
    assert [row["approval_id"] for row in rows] == [pending.approval_id]
    assert rows[0]["tool"] == "refund"

    assert cli.main(["approvals", "--db", str(db)]) == 0
    shown = capsys.readouterr().out
    assert pending.approval_id in shown
    assert "harness approve" in shown
    assert _pathlib.Path(settings.db_path).name  # the flag, not the default, was used


def test_cli_approve_parses_its_arguments():
    from rigging.cli import _parser

    args = _parser().parse_args(["approve", "agent.yaml", "ap_1", "--deny", "--as", "bob"])
    assert (args.command, args.manifest, args.approval_id) == ("approve", "agent.yaml", "ap_1")
    assert args.deny is True
    assert args.approver == "bob"
