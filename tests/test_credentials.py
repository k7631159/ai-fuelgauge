"""Tests for credential extraction and resolution."""
import json

from ai_fuelgauge import _extract_token_from_cred_json, read_claude_token


class TestExtractToken:
    def test_nested_claudeAiOauth(self):
        data = {"claudeAiOauth": {"accessToken": "abc"}}
        assert _extract_token_from_cred_json(data) == "abc"

    def test_flat_accessToken(self):
        data = {"accessToken": "xyz"}
        assert _extract_token_from_cred_json(data) == "xyz"

    def test_flat_access_token_snake_case(self):
        data = {"access_token": "snake"}
        assert _extract_token_from_cred_json(data) == "snake"

    def test_returns_none_if_missing(self):
        assert _extract_token_from_cred_json({}) is None
        assert _extract_token_from_cred_json({"other": "thing"}) is None

    def test_returns_none_if_value_not_string(self):
        assert _extract_token_from_cred_json({"accessToken": 123}) is None
        assert _extract_token_from_cred_json({"accessToken": None}) is None
        assert _extract_token_from_cred_json({"accessToken": ["a", "b"]}) is None

    def test_nested_preferred_over_flat(self):
        """claudeAiOauth.accessToken must win over flat accessToken."""
        data = {
            "claudeAiOauth": {"accessToken": "nested-wins"},
            "accessToken": "flat-loses",
        }
        assert _extract_token_from_cred_json(data) == "nested-wins"

    def test_malformed_nested_falls_through(self):
        """If claudeAiOauth exists but wrong shape, try flat accessToken."""
        data = {
            "claudeAiOauth": "not-an-object",
            "accessToken": "flat-wins",
        }
        assert _extract_token_from_cred_json(data) == "flat-wins"


class TestReadClaudeToken:
    def test_env_var_wins(self, isolated_paths, valid_creds_file, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-env")
        # Even with a valid file, env takes precedence
        assert read_claude_token() == "from-env"

    def test_file_used_when_no_env(self, valid_creds_file):
        assert read_claude_token() == "sk-ant-test-access-abc"

    def test_returns_none_when_no_creds_anywhere(self, isolated_paths):
        # Cred file doesn't exist, no env
        assert read_claude_token() is None

    def test_corrupt_creds_json_returns_none(self, isolated_paths):
        isolated_paths["creds"].write_text("{ this is not json")
        assert read_claude_token() is None

    def test_empty_creds_json_returns_none(self, isolated_paths):
        isolated_paths["creds"].write_text("{}")
        assert read_claude_token() is None
