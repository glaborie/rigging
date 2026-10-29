from __future__ import annotations

from rigging.decide.calibrate import DecisionCase, score
from rigging.errors import TransportError
from rigging.protocols import DecisionAnswer


class PacedModel:
    """Records gaps between decide() calls; fails twice with 429 first."""

    name = "scripted"

    def __init__(self, fail_first: int = 0) -> None:
        self.calls: list[float] = []
        self.remaining_fails = fail_first

    def decide(
        self, state: str, questions: dict[str, list[str]]
    ) -> dict[str, DecisionAnswer]:
        import time

        self.calls.append(time.monotonic())
        if self.remaining_fails > 0:
            self.remaining_fails -= 1
            raise TransportError(429, "Rate limit exceeded", attempts=1)
        (question,) = questions
        options = questions[question]
        return {
            question: DecisionAnswer(
                question=question,
                choice=options[0],
                probability=0.6,
                distribution={option: 1.0 / len(options) for option in options},
            )
        }


def _cases(n: int) -> list[DecisionCase]:
    return [
        DecisionCase(id=f"c{i}", state="s", question="q", options=("a", "b"), label="a")
        for i in range(n)
    ]


def test_score_paces_requests_by_min_interval(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("rigging.decide.calibrate.time.sleep", sleeps.append)
    monkeypatch.setattr(
        "rigging.decide.calibrate.time.monotonic",
        lambda: len(sleeps) * 0.0,  # monotonic frozen; gaps come from sleeps
    )

    model = PacedModel()
    score(model, _cases(3), min_interval=3.0)
    # after the first call, each subsequent call must have waited >= interval
    assert len(sleeps) >= 2
    assert all(s >= 3.0 for s in sleeps)


def test_score_retries_429_and_gives_up_after_max_retries(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("rigging.decide.calibrate.time.sleep", sleeps.append)
    monkeypatch.setattr("rigging.decide.calibrate.time.monotonic", lambda: 0.0)

    model = PacedModel(fail_first=2)
    report = score(model, _cases(1), min_interval=0.0, max_retries=3)
    assert report.n_cases == 1
    assert len(model.calls) == 3  # two 429s then success

    stubborn = PacedModel(fail_first=99)
    monkeypatch.setattr(
        "rigging.decide.calibrate.time.sleep",
        lambda _s: sleeps.append(_s),
    )
    try:
        score(stubborn, _cases(1), min_interval=0.0, max_retries=2)
    except TransportError as exc:
        assert exc.status == 429
    else:
        raise AssertionError("expected TransportError after exhausting retries")
    assert len(stubborn.calls) == 3  # initial + 2 retries
