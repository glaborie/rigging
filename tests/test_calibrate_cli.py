from __future__ import annotations

import pathlib

import pytest

from rigging import cli
from rigging.protocols import DecisionAnswer
from rigging.registry import DEFAULT_REGISTRY


class ScriptedModel:
    name = "scripted"

    def decide(self, state: str, questions: dict[str, list[str]]) -> dict[str, DecisionAnswer]:
        out: dict[str, DecisionAnswer] = {}
        for prompt, options in questions.items():
            distribution = {option: 1.0 / len(options) for option in options}
            out[prompt] = DecisionAnswer(
                question=prompt, choice=options[0], probability=distribution[options[0]],
                distribution=distribution,
            )
        return out


def _dataset(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "mini.jsonl"
    path.write_text(
        '{"id":"a","state":"s","question":"escalate?","options":["yes","no"],"label":"yes"}\n',
        encoding="utf-8",
    )
    return path


def test_calibrate_cli_scores_a_registered_model(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    DEFAULT_REGISTRY.add("decision_model", "scripted", ScriptedModel, replace=True)
    args = cli._parser().parse_args(["calibrate", "scripted", "--dataset", str(_dataset(tmp_path))])
    exit_code = cli.cmd_calibrate(args)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "scripted" in out
    assert "accuracy" in out


def test_calibrate_cli_exits_2_for_an_unknown_model(capsys: pytest.CaptureFixture[str]) -> None:
    args = cli._parser().parse_args(["calibrate", "not-registered"])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_calibrate(args)
    assert exc.value.code == 2
    assert "not-registered" in capsys.readouterr().err


def test_calibrate_cli_exits_2_when_credentials_are_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    from types import SimpleNamespace

    # importing the calibrate module runs the decide package init (registers models)
    from rigging.decide import calibrate as _calib  # noqa: F401

    # Settings() reads the project .env, so stub it to simulate no key.
    monkeypatch.setattr(
        "rigging.decide.freellmapi.Settings",
        lambda: SimpleNamespace(freellm_base_url="http://localhost:3001/v1", freellm_api_key=""),
    )
    args = cli._parser().parse_args(
        ["calibrate", "@cf/cloudflare/clef-flash", "--dataset", str(_dataset(tmp_path))]
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_calibrate(args)
    assert exc.value.code == 2
