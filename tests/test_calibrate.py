from __future__ import annotations

import json
import pathlib

import pytest

from rigging.decide.calibrate import (
    CalibrationReport,
    DecisionCase,
    load_dataset,
    score,
)
from rigging.protocols import DecisionAnswer


class ScriptedModel:
    """Fake DecisionModel that answers every question with its first option."""

    name = "scripted"

    def decide(self, state: str, questions: dict[str, list[str]]) -> dict[str, DecisionAnswer]:
        out: dict[str, DecisionAnswer] = {}
        for prompt, options in questions.items():
            distribution = {option: 1.0 / len(options) for option in options}
            first = options[0]
            out[prompt] = DecisionAnswer(
                question=prompt, choice=first, probability=distribution[first],
                distribution=distribution,
            )
        return out


class PerfectModel:
    name = "perfect"

    def decide(self, state: str, questions: dict[str, list[str]]) -> dict[str, DecisionAnswer]:
        out: dict[str, DecisionAnswer] = {}
        for prompt, options in questions.items():
            distribution = {option: (1.0 if option == "yes" else 0.0) for option in options}
            out[prompt] = DecisionAnswer(
                question=prompt, choice="yes", probability=1.0, distribution=distribution
            )
        return out


def test_dataset_loads_from_jsonl_and_exposes_labels() -> None:
    path = pathlib.Path("tests/data/decisions.jsonl")
    cases = load_dataset(path)
    assert len(cases) >= 40
    assert all(isinstance(case, DecisionCase) for case in cases)
    assert all(case.label in case.options for case in cases)


def test_score_computes_accuracy_and_brier_for_a_perfect_model() -> None:
    dataset = [
        DecisionCase(id="a", state="s", question="escalate?", options=["yes", "no"], label="yes"),
        DecisionCase(id="b", state="s", question="escalate?", options=["yes", "no"], label="yes"),
    ]
    report = score(PerfectModel(), dataset)
    assert isinstance(report, CalibrationReport)
    assert report.accuracy == 1.0
    assert report.brier == 0.0
    assert report.n_cases == 2


def test_score_brier_penalises_confident_wrong_answers() -> None:
    class AlwaysYesConfident:
        name = "always-yes"

        def decide(self, state: str, questions: dict[str, list[str]]) -> dict[str, DecisionAnswer]:
            out: dict[str, DecisionAnswer] = {}
            for prompt, options in questions.items():
                distribution = {option: (1.0 if option == "yes" else 0.0) for option in options}
                out[prompt] = DecisionAnswer(
                    question=prompt, choice="yes", probability=1.0, distribution=distribution
                )
            return out

    dataset = [
        DecisionCase(id="a", state="s", question="escalate?", options=["yes", "no"], label="no"),
    ]
    report = score(AlwaysYesConfident(), dataset)
    assert report.accuracy == 0.0
    assert report.brier == pytest.approx(2.0)  # (1-0)^2 + (0-1)^2 over the two classes


def test_score_rejects_a_case_whose_label_is_not_an_option() -> None:
    dataset = [
        DecisionCase(id="a", state="s", question="escalate?", options=["yes", "no"], label="maybe"),
    ]
    with pytest.raises(ValueError, match="label"):
        score(ScriptedModel(), dataset)


def test_report_renders_a_reviewer_readable_table() -> None:
    dataset = [
        DecisionCase(id="a", state="s", question="escalate?", options=["yes", "no"], label="yes"),
    ]
    text = score(ScriptedModel(), dataset).render()
    assert "accuracy" in text
    assert "brier" in text
    assert "n" in text


def test_dataset_records_are_valid_json_with_required_fields() -> None:
    path = pathlib.Path("tests/data/decisions.jsonl")
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        assert {"id", "state", "question", "options", "label"} <= set(record)
