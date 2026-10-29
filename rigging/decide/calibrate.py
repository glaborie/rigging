"""Calibration scoring for decision models (ADR-0009).

A decision model is advisory until it earns authority on a labelled set. This
module measures it: accuracy (argmax choice vs. label) and multiclass Brier
score (calibration), over `tests/data/decisions.jsonl`-shaped cases.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Protocol

from rigging.protocols import DecisionAnswer


class _DecisionModel(Protocol):
    name: str

    def decide(self, state: str, questions: dict[str, list[str]]) -> dict[str, DecisionAnswer]: ...


@dataclass(frozen=True)
class DecisionCase:
    id: str
    state: str
    question: str
    options: tuple[str, ...]
    label: str


@dataclass(frozen=True)
class CalibrationReport:
    model: str
    n_cases: int
    accuracy: float
    brier: float

    def render(self) -> str:
        """One-line-per-metric summary a reviewer can read in five seconds."""
        return (
            f"decision model {self.model}: n={self.n_cases} "
            f"accuracy={self.accuracy:.3f} brier={self.brier:.3f}"
        )


def load_dataset(path: str | pathlib.Path) -> list[DecisionCase]:
    """Load `tests/data/decisions.jsonl`: id, state, question, options, label."""
    cases: list[DecisionCase] = []
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        cases.append(
            DecisionCase(
                id=str(record["id"]),
                state=str(record["state"]),
                question=str(record["question"]),
                options=tuple(str(option) for option in record["options"]),
                label=str(record["label"]),
            )
        )
    return cases


def score(model: _DecisionModel, dataset: list[DecisionCase]) -> CalibrationReport:
    """Ask the model every case's question and measure accuracy + Brier score."""
    if not dataset:
        raise ValueError("dataset is empty")
    correct = 0
    brier_sum = 0.0
    for case in dataset:
        if case.label not in case.options:
            raise ValueError(f"case {case.id!r}: label {case.label!r} is not one of its options")
        answers = model.decide(case.state, {case.question: list(case.options)})
        answer = answers.get(case.question)
        if answer is None:
            raise ValueError(f"case {case.id!r}: model returned no answer for {case.question!r}")
        if answer.choice == case.label:
            correct += 1
        brier_sum += sum(
            (answer.distribution.get(option, 0.0) - (1.0 if option == case.label else 0.0)) ** 2
            for option in case.options
        )
    return CalibrationReport(
        model=model.name,
        n_cases=len(dataset),
        accuracy=correct / len(dataset),
        brier=brier_sum / len(dataset),
    )
