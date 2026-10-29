from __future__ import annotations


def test_span01_lite_is_exported_and_registered_by_model_id() -> None:
    from rigging.decide import OpenRouterSpan01Lite
    from rigging.registry import DEFAULT_REGISTRY

    assert "respan/span-01-lite:free" in DEFAULT_REGISTRY.names("decision_model")
    assert DEFAULT_REGISTRY.get("decision_model", "respan/span-01-lite:free") is OpenRouterSpan01Lite
