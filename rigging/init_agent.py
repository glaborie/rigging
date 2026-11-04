"""`harness init`: an interview that writes a manifest the schema already accepts.

Unix-configure style on purpose: every question has a numbered set of real
choices (read from the registry, not a hardcoded list), a suggested default, and
Enter takes it. The output is validated by loading it through `load_manifest`
before anything touches disk, so the wizard cannot emit a manifest `harness run`
would reject.

No new dependency: `input` and `yaml` are enough (rule 7). The prompt callable is
injected so the tests drive the interview without a terminal.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

import yaml

from rigging.config import settings
from rigging.errors import ManifestError, RiggingError
from rigging.manifest import SCHEMA_PATH, load_manifest
from rigging.registry import DEFAULT_REGISTRY

MODEL_CHOICES: list[tuple[str, str]] = [
    ("beam:cloud", "free through Ollama cloud (needs `ollama signin`)"),
    ("qwen3:8b", "fully local, no credentials"),
    ("", "other — type a model id"),
]

PROMPT_CHOICES: list[tuple[str, str]] = [
    ("You are a helpful assistant. Answer in one or two sentences.", "terse assistant"),
    ("You are a careful support agent. Use the tools you have before you answer.", "tool-using assistant"),
    ("", "write your own"),
]

RETRIEVER_CHOICES: list[tuple[str, str]] = [
    ("", "none — no retrieval tool"),
    ("fts5", "SQLite FTS5, needs `demo.db` chunks beside the manifest (`harness index`)"),
    ("qdrant", "needs the [qdrant] extra, a running Qdrant and an embedding model"),
]

GUARDRAIL_DEFAULTS: list[tuple[str, list[str]]] = [
    ("on_input", ["pii_redaction"]),
    ("on_tool_call", ["consequential_action"]),
    ("on_tool_result", ["prompt_injection"]),
    ("on_output", ["pii_redaction", "commitment_check"]),
]

LIMIT_DEFAULTS: list[tuple[str, str, str]] = [
    ("max_steps", "20", "model turns before the loop asks for a final answer"),
    ("max_tokens", "60000", "total tokens for the run"),
    ("max_cost_usd", "0.50", "USD cap; unknown models skip the check"),
]


class InitError(RiggingError):
    """A refusal the CLI reports and exits 2 on (existing files, a bad name)."""


class Interview:
    """One question at a time, with the terminal wired in only through `ask`."""

    def __init__(self, ask: Callable[[str], str], out: TextIO, *, quiet: bool = False) -> None:
        self._ask = ask
        self._out = out
        self.quiet = quiet

    # ------------------------------------------------------------------ output

    def say(self, text: str = "") -> None:
        if not self.quiet:
            self._out.write(text + "\n")

    def rule(self, title: str) -> None:
        self.say()
        self.say(f"  {title}")

    def _show(self, choices: Sequence[tuple[str, str]], default: str) -> None:
        for i, (value, note) in enumerate(choices, 1):
            mark = " (default)" if value == default else ""
            label = value or "(none)"
            self.say(f"    {i}) {label:<12} {note}{mark}")

    # ------------------------------------------------------------------ inputs

    def text(self, label: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        raw = self._ask(f"  {label}{hint}: ").strip()
        return raw or default

    def choice(self, label: str, choices: Sequence[tuple[str, str]], default: str) -> str:
        self.rule(label)
        self._show(choices, default)
        index = next((i for i, (v, _) in enumerate(choices, 1) if v == default), 1)
        raw = self._ask(f"  {label} [{index}]: ").strip()
        if not raw:
            return default
        if raw in ("-", "none"):
            return ""
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1][0]
        return raw  # a typed model id / retriever name is an answer too

    def many(self, label: str, choices: Sequence[str], default: Sequence[str]) -> list[str]:
        self.rule(label)
        marks = set(default)
        for i, name in enumerate(choices, 1):
            mark = " (suggested)" if name in marks else ""
            self.say(f"    {i}) {name}{mark}")
        if not choices:
            self.say("    (nothing registered)")
            return list(default)
        hint = ", ".join(default) if default else "none"
        raw = self._ask(f"  {label} (numbers/names, '-' for none) [{hint}]: ").strip()
        if not raw:
            return list(default)
        if raw in ("-", "none"):
            return []
        picked: list[str] = []
        for token in raw.replace(" ", ",").split(","):
            token = token.strip()
            if not token:
                continue
            if token.isdigit() and 1 <= int(token) <= len(choices):
                picked.append(choices[int(token) - 1])
            elif token in choices:
                picked.append(token)
            else:
                raise InitError(f"{label}: {token!r} is not one of the listed choices")
        return list(dict.fromkeys(picked))

    def yes(self, label: str, default: bool = True) -> bool:
        hint = "Y/n" if default else "y/N"
        raw = self._ask(f"  {label} [{hint}]: ").strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes")


# --------------------------------------------------------------------- choices

def _capability(kind: str) -> list[str]:
    """Registered names, loading the packs the CLI loads (builtin + demo)."""
    for mod in ("rigging.tools.core", "rigging.tools.demo", "rigging.guardrails",
                "rigging.retriever", "rigging.decide"):
        try:
            importlib.import_module(mod)
        except ImportError:  # an extra is missing; that pack simply has no choices
            continue
    return DEFAULT_REGISTRY.names(kind)


def _side_effecting(names: Sequence[str]) -> list[str]:
    """Tools the registry already marks consequential.

    The registry stores the decorated *function* (the decorator stamps
    `__rigging_tool__` on it) while assembly builds `FunctionTool` instances, so
    both shapes are read here. This is the reason the wizard beats a shell script:
    `approval.side_effecting_tools` is derived, never guessed.
    """
    out: list[str] = []
    for name in names:
        try:
            impl: Any = DEFAULT_REGISTRY.get("tool", name)
        except Exception:  # an unknown name is the run's problem, not the wizard's
            continue
        meta = getattr(impl, "__rigging_tool__", None) or {}
        if getattr(impl, "side_effecting", False) or meta.get("side_effecting"):
            out.append(name)
    return out


def _exporters() -> list[str]:
    try:
        from rigging.observability import EXPORTERS

        return sorted(EXPORTERS)
    except ImportError:
        return []


def _profiles() -> list[str]:
    """Profiles the .env actually defines, so `credential_profile` is a real choice."""
    extra = settings.model_extra or {}
    found = {
        key[len("profile_"):-len("_base_url")]
        for key in extra
        if key.startswith("profile_") and key.endswith("_base_url")
    }
    return ["ollama", "default", *sorted(found - {"ollama", "default"})]


def _slug(name: str) -> bool:
    return bool(name) and name[0].isalnum() and all(c.isalnum() or c in "-_." for c in name)


# ------------------------------------------------------------------ the manifest

def _dump(raw: dict[str, Any], *, schema_ref: str, manifest_path: Path) -> str:
    """YAML plus the two comments that make the file self-describing.

    `$schema` has to travel as a comment: the contract sets
    `additionalProperties: false` and has no `$schema` key, so a real key would be
    rejected by the very validator it asks the editor to use.
    """
    header = (
        f"# Generated by `harness init` — edit freely, then:\n"
        f"#   uv run harness run {manifest_path} \"hello\"\n"
        f"# yaml-language-server: $schema={schema_ref}\n"
    )
    body: str = yaml.safe_dump(raw, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return header + body


def build_manifest(iv: Interview) -> dict[str, Any]:
    """Ask the questions and return a manifest dict (unvalidated)."""
    tools = _capability("tool")
    guardrails = _capability("guardrail")

    iv.say()
    iv.say("  rigging init — every question has a suggested answer; Enter takes it.")

    iv.rule("name")
    while True:
        name = iv.text("agent name", "my-agent")
        if _slug(name):
            break
        iv.say("    letters, digits, '.', '_' and '-' only")
    description = iv.text("description", "")

    model = iv.choice("model", MODEL_CHOICES, MODEL_CHOICES[0][0])
    profiles = _profiles()
    profile = iv.choice(
        "credential_profile",
        [(p, "gateway profile from .env" if p != "ollama" else "local Ollama") for p in profiles],
        "ollama",
    )

    prompt = iv.choice("system prompt", PROMPT_CHOICES, PROMPT_CHOICES[0][0])
    if not prompt:
        prompt = iv.text("system prompt", "")

    picked = iv.many("tools", tools, [])
    retriever = iv.choice("retriever", RETRIEVER_CHOICES, "")
    if retriever and retriever not in _capability("retriever"):
        raise InitError(
            f"retriever {retriever!r} is not registered "
            f"(have: {', '.join(_capability('retriever'))})"
        )

    hooks: dict[str, list[str]] = {}
    for hook, suggested in GUARDRAIL_DEFAULTS:
        keep = [n for n in suggested if n in guardrails]
        chosen = iv.many(f"guardrails.{hook}", guardrails, keep)
        if chosen:
            hooks[hook] = chosen

    policy = iv.choice(
        "approval policy",
        [("auto_deny", "deny every side-effecting call (safe default)"),
         ("cli", "ask y/N on the terminal")],
        "auto_deny",
    )
    gated = _side_effecting(picked)
    if gated:
        iv.say(f"    side-effecting: {', '.join(gated)} — always gated in the manifest")

    limits: dict[str, Any] = {}
    for key, default, note in LIMIT_DEFAULTS:
        iv.rule(f"limits.{key} — {note}")
        raw_value = iv.text(key, default)
        try:
            limits[key] = float(raw_value) if key == "max_cost_usd" else int(raw_value)
        except ValueError:
            raise InitError(f"limits.{key}: {raw_value!r} is not a number") from None

    exporters = iv.many("observability.exporters", _exporters(), [])

    raw: dict[str, Any] = {"name": name}
    if description:
        raw["description"] = description
    raw["model"] = model
    raw["credential_profile"] = profile
    raw["system_prompt_file"] = "prompt.md"
    raw["tools"] = picked
    if retriever:
        raw["retriever"] = {"name": retriever, "options": {}}
    if hooks:
        raw["guardrails"] = hooks
    raw["approval"] = {"policy": policy, "side_effecting_tools": gated}
    raw["limits"] = limits
    raw["observability"] = {"exporters": exporters, "capture_content": True}
    raw["__prompt__"] = prompt
    return raw


def _validate(raw: dict[str, Any]) -> None:
    """Round-trip through the real loader; the manifest is valid or nothing is written."""
    import tempfile

    clean = {k: v for k, v in raw.items() if not k.startswith("__")}
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "agent.yaml"
        probe.write_text(yaml.safe_dump(clean, sort_keys=False), encoding="utf-8")
        try:
            load_manifest(probe)
        except (ManifestError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
            raise InitError(f"the answers did not make a valid manifest: {exc}") from exc


def summary(raw: dict[str, Any]) -> list[str]:
    lines = [
        f"  name      {raw['name']}",
        f"  model     {raw['model']} (profile {raw['credential_profile']})",
        f"  tools     {', '.join(raw['tools']) or '(none)'}",
    ]
    gated = raw["approval"]["side_effecting_tools"]
    if gated:
        lines.append(f"  approval  {raw['approval']['policy']} · gate on {', '.join(gated)}")
    else:
        lines.append(f"  approval  {raw['approval']['policy']}")
    if raw.get("guardrails"):
        lines.append("  guardrails " + " · ".join(f"{k}={','.join(v)}" for k, v in raw["guardrails"].items()))
    if raw.get("retriever"):
        lines.append(f"  retriever {raw['retriever']['name']}")
    lines.append(f"  limits    steps={raw['limits']['max_steps']} "
                 f"tokens={raw['limits']['max_tokens']} usd={raw['limits']['max_cost_usd']}")
    return lines


def run_init(
    *,
    directory: str | Path = "agents",
    assume_yes: bool = False,
    force: bool = False,
    ask: Callable[[str], str] = input,
    out: TextIO | None = None,
) -> int:
    """Interview, validate, write `agents/<name>/agent.yaml` + `prompt.md`."""
    out = out or sys.stdout
    iv = Interview(ask if not assume_yes else (lambda _prompt: ""), out, quiet=assume_yes)

    raw = build_manifest(iv)
    _validate(raw)  # before the "write?" question: never ask to write an invalid file

    out.write("\n")
    for line in summary(raw):
        out.write(line + "\n")

    root = Path(directory) / raw["name"]
    manifest_path = root / "agent.yaml"
    prompt_path = root / "prompt.md"
    existing = [p for p in (manifest_path, prompt_path) if p.exists()]
    if existing and not force:
        out.write("\n")
        raise InitError(
            f"{', '.join(str(p) for p in existing)} already exists "
            f"(pass --force to overwrite)"
        )

    if not assume_yes:
        out.write("\n")
        if not iv.yes(f"write {manifest_path} + {prompt_path}?", True):
            raise InitError("nothing written")

    root.mkdir(parents=True, exist_ok=True)
    text = _dump(
        {k: v for k, v in raw.items() if not k.startswith("__")},
        schema_ref=_relpath(SCHEMA_PATH, root),
        manifest_path=manifest_path,
    )
    manifest_path.write_text(text, encoding="utf-8")
    prompt_path.write_text(str(raw["__prompt__"]).rstrip() + "\n", encoding="utf-8")

    out.write(f"\nwrote {manifest_path} and {prompt_path}\n\n")
    out.write("next:\n")
    out.write(f"  uv run harness run {manifest_path} \"hello\" --replay "
              f"examples/hello/transcript-codestral.jsonl\n")
    out.write(f"  uv run harness run {manifest_path} \"hello\""
              f"    # the same prompt, through the gateway\n")
    return 0


def _relpath(target: Path, start: Path) -> str:
    import os

    try:
        return os.path.relpath(target, start)
    except ValueError:  # different drive on Windows; absolute is the honest fallback
        return str(target)
