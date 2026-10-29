from __future__ import annotations


def test_clef_flash_is_exported_and_registered_by_model_id() -> None:
    from rigging.decide import FreeLLMClefFlash
    from rigging.registry import DEFAULT_REGISTRY

    assert "@cf/cloudflare/clef-flash" in DEFAULT_REGISTRY.names("decision_model")
    assert DEFAULT_REGISTRY.get("decision_model", "@cf/cloudflare/clef-flash") is FreeLLMClefFlash
