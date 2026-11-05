"""bench.py must parse both output shapes of `harness eval`: the `--json`
envelope and the human table. A parse miss is recorded as a *crashed run*, so a
parser bug looks exactly like a broken provider — five zeroes and no clue.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

BENCH = Path(__file__).resolve().parents[1] / "scripts" / "bench.py"


def _bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_under_test", BENCH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_json_envelope_from_harness_eval() -> None:
    """`harness eval --json` prints {"suite", "results"}, not a bare list."""
    rows = _bench().parse(json.dumps({"suite": "s", "results": [
        {"id": "refund", "passed": True, "latency_s": 1.5, "cost_usd": 0.0, "steps": 3},
        {"id": "unknown-sku", "passed": False, "latency_s": 0.5, "cost_usd": None, "steps": 2},
    ]}))
    assert [r["case"] for r in rows] == ["refund", "unknown-sku"]
    assert [r["passed"] for r in rows] == [True, False]
    assert rows[0]["steps"] == 3 and rows[0]["latency_s"] == 1.5


def test_bare_list_is_still_accepted() -> None:
    rows = _bench().parse(json.dumps([{"case": "a", "passed": True}]))
    assert rows == [{"case": "a", "passed": True, "latency_s": None,
                     "cost_usd": None, "steps": None}]


def test_table_output_falls_back_to_scraping() -> None:
    table = "case                       result  steps\nrefund-in-window-approved  PASS    4\n"
    rows = _bench().parse(table)
    assert [(r["case"], r["passed"]) for r in rows] == [("refund-in-window-approved", True)]
    assert rows[0]["steps"] is None  # the text table carries no metrics


def test_unparsable_output_is_empty_so_the_run_counts_as_a_crash() -> None:
    assert _bench().parse("Traceback (most recent call last):\nFileNotFoundError: nope") == []


def test_eval_runs_export_langfuse_by_default() -> None:
    """A benchmark without traces cannot answer 'which model did that?'."""
    cmd = _bench().build_cmd("uv run harness eval {suite} --model {model}",
                             "s.yaml", "opencode-go/deepseek-v4.1-flash", ["langfuse"])
    assert cmd[-2:] == ["--export", "langfuse"]
    assert "opencode-go/deepseek-v4.1-flash" in cmd


def test_an_explicit_export_in_the_template_is_not_overridden() -> None:
    bench = _bench()
    template = "uv run harness eval {suite} --model {model} --export mlflow"
    cmd = bench.build_cmd(template, "s.yaml", "m", ["langfuse"])
    assert cmd.count("--export") == 1 and "mlflow" in cmd
    assert bench.build_cmd(template, "s.yaml", "m", [])[-1] == "mlflow"


def test_each_rep_is_tagged_with_bench_model_and_pass() -> None:
    bench = _bench()
    assert bench.run_tags("opencode-go/deepseek-v4.1-flash", 0) == "bench,opencode-go/deepseek-v4.1-flash,rep-1"
    assert bench.run_tags("m", 4).endswith("rep-5")


def test_the_run_name_is_a_utc_stamp() -> None:
    assert _bench().run_stamp(0) == "1970-01-01T00-00-00Z"


def test_an_existing_run_directory_is_never_reused(tmp_path) -> None:
    """Rerunning a benchmark must not overwrite the first run's evidence."""
    bench = _bench()
    first = bench.fresh_dir(tmp_path, "smoke")
    first.mkdir()
    second = bench.fresh_dir(tmp_path, "smoke")
    assert second.name == "smoke-2" and second != first


def test_two_invocations_write_two_directories(tmp_path, capsys) -> None:
    """The end-to-end version: same command twice, both result sets intact."""
    bench = _bench()
    args = [str(tmp_path / "eval.yaml"), "--models", "m", "--repeats", "2",
            "--cmd", "echo refund PASS", "--no-export", "--quiet",
            "--out", str(tmp_path / "out"), "--run-name", "smoke"]
    assert bench.main(args) == 0
    capsys.readouterr()
    assert bench.main(args) == 0
    err = capsys.readouterr().err

    one = tmp_path / "out" / "smoke"
    two = tmp_path / "out" / "smoke-2"
    for run_dir in (one, two):
        assert (run_dir / "raw" / "m-0.txt").read_text().strip() == "refund PASS"
        assert (run_dir / "raw" / "m-1.txt").exists()
        assert (run_dir / "table.md").exists() and (run_dir / "summary.json").exists()
    assert "1/2" in err, "the rep counter is part of the progress output"
    assert "results:" in err and str(two) in err


def test_a_broken_mlflow_target_stops_the_benchmark_before_any_run(tmp_path, capsys, monkeypatch) -> None:
    """85 runs that record nothing is worse than a refusal to start."""
    bench = _bench()
    monkeypatch.setattr(bench, "mlflow_problem",
                        lambda: "mlflow experiment '1' (rigging-bench) is deleted")
    out = tmp_path / "out"
    rc = bench.main([str(tmp_path / "eval.yaml"), "--models", "m", "--repeats", "2",
                     "--cmd", "echo refund PASS", "--export", "mlflow", "--out", str(out)])
    err = capsys.readouterr().err
    assert rc == 2, "a configuration error is an exit code, not a benchmark of zeroes"
    assert "cannot record runs" in err and "is deleted" in err
    assert not out.exists(), "nothing is written when the run cannot be exported"

