"""Settings: `.env` is the home for secrets *and* for named credential profiles.

The point of these tests: a manifest that says `credential_profile: ollama` must
work from `.env` alone, with nothing exported in the shell.
"""

from __future__ import annotations

from rigging.config import Settings


def test_profile_is_read_from_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("RIGGING_PROFILE_OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("RIGGING_PROFILE_OLLAMA_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "RIGGING_BASE_URL=http://default:20128/v1\n"
        "RIGGING_PROFILE_OLLAMA_BASE_URL=http://localhost:11434/v1\n"
        "RIGGING_PROFILE_OLLAMA_API_KEY=ollama\n"
    )

    profile = Settings(_env_file=str(env)).profile("ollama")
    assert profile.base_url == "http://localhost:11434/v1"
    assert profile.api_key == "ollama"
    assert profile.label == "ollama"


def test_the_environment_beats_the_env_file_for_a_profile(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "RIGGING_PROFILE_OLLAMA_BASE_URL=http://from-file:11434/v1\n"
        "RIGGING_PROFILE_OLLAMA_API_KEY=from-file\n"
    )
    monkeypatch.setenv("RIGGING_PROFILE_OLLAMA_BASE_URL", "http://from-shell:11434/v1")

    profile = Settings(_env_file=str(env)).profile("ollama")
    assert profile.base_url == "http://from-shell:11434/v1"
    assert profile.api_key == "from-file"


def test_unknown_profile_falls_back_to_the_default_gateway(tmp_path, monkeypatch):
    monkeypatch.delenv("RIGGING_PROFILE_NOPE_BASE_URL", raising=False)
    monkeypatch.delenv("RIGGING_PROFILE_NOPE_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("RIGGING_BASE_URL=http://default:20128/v1\nRIGGING_API_KEY=k\n")

    profile = Settings(_env_file=str(env)).profile("nope")
    assert profile.base_url == "http://default:20128/v1"
    assert profile.api_key == "k"
