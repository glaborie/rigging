from __future__ import annotations

import pytest

from rigging.registry import Registry


@pytest.fixture()
def registry() -> Registry:
    return Registry()


@pytest.fixture(autouse=True)
def _isolate_pii(tmp_path, monkeypatch):
    """Keep a developer's `.env` PII key out of the suite (ADR-0027).

    With `RIGGING_PII_KEY` set, the process store would vault every test session
    into `runs/pii-vault` and restore maps between tests, so sentinel numbering
    would depend on test order. Tests that exercise sealing set the key (or build
    their own vault) explicitly.
    """
    import rigging.guardrails.pii as pii
    from rigging.config import settings

    monkeypatch.setattr(settings, "pii_key", "")
    monkeypatch.setattr(settings, "pii_vault_dir", str(tmp_path / "vault"))
    monkeypatch.setattr(pii, "DEFAULT_SENTINEL_STORE", pii.SentinelStore())
