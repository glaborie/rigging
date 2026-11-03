"""Eval config: `--export` traces a suite without editing the shared manifest."""

from __future__ import annotations

import pathlib

from rigging.eval import _with_exporters
from rigging.manifest import load_manifest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_export_override_replaces_the_manifest_list():
    spec = load_manifest(str(ROOT / "examples/b2b-support/agent.yaml"))
    assert spec.observability.exporters == [], "the demo manifest stays key-free"

    traced = _with_exporters(spec, ["langfuse"])
    assert traced.observability.exporters == ["langfuse"]
    assert spec.observability.exporters == [], "the input spec is not mutated"

    assert _with_exporters(spec, None).observability.exporters == []
