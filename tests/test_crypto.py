"""Field encryption for stored PII (ADR-0027), and the encrypted session vault.

Offline and deterministic: `cryptography` is imported by the code under test, so
these skip when the `[vault]` extra is absent, the same way the service tests
skip without `[service]`.
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("cryptography", reason="needs the [vault] extra")

from rigging.crypto import (
    PREFIX,
    load_key,
    new_key,
    open_sealed,
    seal,
)
from rigging.errors import CryptoError
from rigging.guardrails.pii import SentinelStore
from rigging.vault import SentinelVault

EMAIL = "anna.mueller@example.com"


# ------------------------------------------------------------------ primitives


def test_round_trip_and_prefix():
    key = load_key(new_key())
    sealed = seal(EMAIL, key, aad="notifications.contact")
    assert sealed.startswith(PREFIX) and EMAIL not in sealed
    assert open_sealed(sealed, key, aad="notifications.contact") == EMAIL


def test_a_wrong_key_does_not_open():
    sealed = seal(EMAIL, load_key(new_key()))
    with pytest.raises(CryptoError, match="wrong key"):
        open_sealed(sealed, load_key(new_key()))


def test_tampering_is_detected():
    key = load_key(new_key())
    sealed = seal(EMAIL, key, aad="orders.contact:ORD-1")
    broken = sealed[:-6] + ("A" if sealed[-6] != "A" else "B") + sealed[-5:]
    with pytest.raises(CryptoError):
        open_sealed(broken, key, aad="orders.contact:ORD-1")


def test_the_aad_binds_a_ciphertext_to_its_column():
    """A value lifted from one column must not open in another."""
    key = load_key(new_key())
    sealed = seal(EMAIL, key, aad="notifications.contact")
    with pytest.raises(CryptoError):
        open_sealed(sealed, key, aad="orders.contact:ORD-1018")


def test_plaintext_passes_through_so_old_rows_still_read():
    """Passthrough is why the claim is 'values the tools write', not 'every byte'."""
    assert open_sealed("luca.ferrari@ticino-retail.ch", load_key(new_key())) == \
        "luca.ferrari@ticino-retail.ch"


def test_the_key_accepts_base64_and_hex_and_rejects_junk():
    raw = new_key()
    assert load_key(raw) == load_key(load_key(raw).hex())
    for bad in ("", "hunter2", raw[:-4]):
        with pytest.raises(CryptoError, match=r"32 bytes|empty"):
            load_key(bad)


# ------------------------------------------------------------------ session vault


def _store(tmp_path, key: str | None = None) -> SentinelStore:
    if key is None:
        return SentinelStore()
    return SentinelStore(vault=SentinelVault(tmp_path / "vault", load_key(key)))


def test_a_vaulted_map_survives_a_new_process(tmp_path):
    """The restart that lost a contact in `s_f824c5a9410f` no longer loses it."""
    key = new_key()
    first = _store(tmp_path, key).get("s_1")
    first.sentinelise(f"email {EMAIL}")

    second = _store(tmp_path, key).get("s_1")  # a fresh store, as after a restart
    assert second.deanonymise("[EMAIL_1]") == EMAIL


def test_the_vault_file_holds_no_plaintext_and_is_owner_only(tmp_path):
    key = new_key()
    store = _store(tmp_path, key)
    store.get("s_1").sentinelise(f"email {EMAIL}")
    files = list((tmp_path / "vault").glob("*.pii"))
    assert len(files) == 1
    assert EMAIL not in files[0].read_text(encoding="utf-8")
    assert "s_1" not in files[0].name, "the file name is a hash, not the session id"
    assert files[0].stat().st_mode & 0o777 == 0o600


def test_a_wrong_key_degrades_to_memory_only(tmp_path, capsys, monkeypatch):
    import rigging.vault as vault_module

    monkeypatch.setattr(vault_module, "_warned", False)  # warnings are once per process
    key = new_key()
    _store(tmp_path, key).get("s_1").sentinelise(f"email {EMAIL}")

    other = _store(tmp_path, new_key()).get("s_1")  # different process, different key
    assert other.deanonymise("[EMAIL_1]") == "[EMAIL_1]", "unresolvable, not a crash"
    assert "does not open with this key" in capsys.readouterr().err


def test_maps_expire_from_the_vault(tmp_path):
    key = new_key()
    store = _store(tmp_path, key)
    store.get("s_1").sentinelise(f"email {EMAIL}")
    path = next((tmp_path / "vault").glob("*.pii"))
    old = time.time() - store.ttl_s - 60
    import os

    os.utime(path, (old, old))
    assert _store(tmp_path, key).get("s_1").deanonymise("[EMAIL_1]") == "[EMAIL_1]"


def test_forget_removes_the_vault_file(tmp_path):
    key = new_key()
    store = _store(tmp_path, key)
    store.get("s_1").sentinelise(f"email {EMAIL}")
    store.forget("s_1")
    assert not list((tmp_path / "vault").glob("*.pii"))


def test_without_a_key_nothing_is_written(tmp_path):
    store = SentinelStore()
    store.get("s_1").sentinelise(f"email {EMAIL}")
    assert not (tmp_path / "vault").exists()
    assert json.dumps(store.get("s_1").to_dict())  # still works in memory


def test_the_persisted_payload_round_trips_counters(tmp_path):
    key = new_key()
    _store(tmp_path, key).get("s_1").sentinelise(f"{EMAIL} and +41 91 555 01 23")
    restored = _store(tmp_path, key).get("s_1")
    emitted = restored.sentinelise("a third@example.com")
    assert emitted == "a [EMAIL_2]", "counters continue, so a placeholder never repeats"


def test_from_settings_vaults_only_when_a_key_is_configured(tmp_path, monkeypatch):
    from rigging.config import settings

    monkeypatch.setattr(settings, "pii_key", "")
    assert SentinelStore.from_settings().vault is None

    monkeypatch.setattr(settings, "pii_key", new_key())
    monkeypatch.setattr(settings, "pii_vault_dir", str(tmp_path / "vault"))
    store = SentinelStore.from_settings()
    assert store.vault is not None
    store.get("s_9").sentinelise(f"email {EMAIL}")
    assert len(list((tmp_path / "vault").glob("*.pii"))) == 1


def test_a_malformed_key_degrades_to_memory_only(tmp_path, monkeypatch, capsys):
    import rigging.vault as vault_module
    from rigging.config import settings

    monkeypatch.setattr(vault_module, "_warned", False)
    monkeypatch.setattr(settings, "pii_key", "not-a-key")
    store = SentinelStore.from_settings()
    assert store.vault is None
    assert "32 bytes" in capsys.readouterr().err
