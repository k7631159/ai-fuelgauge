"""Shared pytest fixtures for ai-fuelgauge tests.

Every test fixture that touches the filesystem redirects reads / writes into
a tempdir so the real user's credentials file, cache file, and codex sqlite
snapshot are never seen or modified.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import ai_fuelgauge as afg  # noqa: E402


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path):
    """Redirect every filesystem constant in ai_fuelgauge into a tempdir."""
    tmp_creds = tmp_path / ".claude" / ".credentials.json"
    tmp_creds.parent.mkdir(parents=True, exist_ok=True)
    tmp_cache = tmp_path / ".cache" / "usage-quota.json"
    tmp_last_good = tmp_path / ".cache" / "usage-quota-last-claude.json"
    tmp_sqlite = tmp_path / ".codex" / "logs_2.sqlite"

    monkeypatch.setattr(afg, "HOME", tmp_path)
    monkeypatch.setattr(afg, "CLAUDE_CREDS", tmp_creds)
    monkeypatch.setattr(afg, "CACHE_FILE", tmp_cache)
    monkeypatch.setattr(afg, "LAST_GOOD_CLAUDE_FILE", tmp_last_good)
    monkeypatch.setattr(afg, "CODEX_LOGS_DB", tmp_sqlite)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    return {
        "tmp": tmp_path,
        "creds": tmp_creds,
        "cache": tmp_cache,
        "last_good": tmp_last_good,
        "sqlite": tmp_sqlite,
    }


@pytest.fixture
def valid_creds_file(isolated_paths):
    """Write a syntactically valid credentials.json with a test token."""
    isolated_paths["creds"].write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-test-access-abc",
                    "refreshToken": "sk-ant-test-refresh-xyz",
                    "expiresAt": 9999999999000,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        )
    )
    return isolated_paths["creds"]
