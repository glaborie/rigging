"""Field encryption for PII that has to be stored (ADR-0027).

The event log and every trace are PII-free by construction (ADR-0025): the model,
the log and the exporters only ever see `[EMAIL_1]`. The one place a raw value
survives is what a *tool* chooses to write down — the contact on an order, the
address a notification was queued to — plus the session sentinel map that lets a
second turn resolve the placeholder (ADR-0026).

This module is the whole cryptographic surface: seal one string, open one string,
and a small encrypted store for the sentinel maps. AES-256-GCM, a random 96-bit
nonce per value, and an `aad` that binds a ciphertext to where it belongs.

What that buys, stated plainly because the threat model has to say it: with the
key in the same environment as the process, this protects a *file* that leaks —
a backup, a disk image, an attached or accidentally committed `demo.db`, a
mounted volume — and nothing against a compromised host or a leaked key. AAD
binding stops a ciphertext being moved between columns undetected; two values in
the same column can still be swapped, because the row id is not always known
before the write.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import os
import secrets
from typing import Any

from rigging.errors import CryptoError

#: Marks a stored value as sealed, and gives the format room to change.
PREFIX = "enc:v1:"
#: Environment variable holding the key (base64 or hex, 32 bytes).
KEY_ENV = "RIGGING_PII_KEY"
_KEY_BYTES = 32
_NONCE_BYTES = 12


def new_key() -> str:
    """A fresh 32-byte key, base64-encoded, for `.env`. Never derive one from a password."""
    return base64.b64encode(secrets.token_bytes(_KEY_BYTES)).decode()


def load_key(raw: str) -> bytes:
    """Parse a key from base64 (standard or URL-safe) or 64 hex characters."""
    text = raw.strip()
    if not text:
        raise CryptoError(f"{KEY_ENV} is empty; a sealed value needs a 32-byte key "
                          "(generate one with `python -c 'from rigging.crypto import new_key;"
                          " print(new_key())'`)")
    candidates: list[bytes] = []
    for candidate in (text, text.replace("-", "+").replace("_", "/")):
        try:
            candidates.append(base64.b64decode(candidate + "=" * (-len(candidate) % 4),
                                               validate=True))
        except (binascii.Error, ValueError):
            continue
    with contextlib.suppress(ValueError):
        candidates.append(bytes.fromhex(text))
    for key in candidates:
        if len(key) == _KEY_BYTES:
            return key
    raise CryptoError(f"{KEY_ENV} must decode to 32 bytes (base64 or 64 hex characters); "
                      "generate one with `python -c 'from rigging.crypto import new_key; "
                      "print(new_key())'`")


def _aead(key: bytes) -> Any:
    """AES-GCM, imported here so the base install does not need `cryptography`."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the environment
        raise CryptoError("sealing PII needs the [vault] extra: uv sync --extra vault") from exc
    return AESGCM(key)


def is_sealed(value: str) -> bool:
    return value.startswith(PREFIX)


def seal(value: str, key: bytes, *, aad: str = "") -> str:
    """Seal one string. `aad` is not stored, so opening needs the same value."""
    if is_sealed(value):
        return value  # already sealed: sealing twice is a bug, not a request
    nonce = os.urandom(_NONCE_BYTES)
    token = _aead(key).encrypt(nonce, value.encode("utf-8"), aad.encode("utf-8"))
    return PREFIX + base64.urlsafe_b64encode(nonce + token).decode("ascii")


def open_sealed(value: str, key: bytes, *, aad: str = "") -> str:
    """Open a sealed string. A value without the prefix is returned unchanged.

    Passthrough exists for rows written before the key was configured (a seeded
    `demo.db`, a migrated database) and is the reason the claim in the threat
    model is "PII written by the tools is sealed", not "every byte is".
    """
    if not is_sealed(value):
        return value
    raw = value[len(PREFIX):]
    try:
        blob = base64.urlsafe_b64decode(raw.encode("ascii"))
        if len(blob) <= _NONCE_BYTES:
            raise ValueError("truncated")
        plaintext: bytes = _aead(key).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:],
                                              aad.encode("utf-8"))
    except CryptoError:
        raise
    except Exception as exc:
        raise CryptoError("cannot open a sealed value: wrong key, or the ciphertext was "
                          "altered (the value is not recoverable)") from exc
    return plaintext.decode("utf-8")
