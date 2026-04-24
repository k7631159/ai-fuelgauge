"""Tests for cache load/save and TTL behaviour."""
import json
import time

from ai_fuelgauge import load_cache, save_cache


class TestLoadCache:
    def test_missing_cache_returns_none(self, isolated_paths):
        assert load_cache(30) is None

    def test_fresh_cache_returned(self, isolated_paths):
        path = isolated_paths["cache"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "codex": {"foo": 1},
                    "claude": {"bar": 2},
                    "_cached_at": int(time.time()),
                }
            )
        )
        result = load_cache(30)
        assert result is not None
        assert result["codex"] == {"foo": 1}
        assert result["claude"] == {"bar": 2}

    def test_expired_cache_returns_none(self, isolated_paths):
        path = isolated_paths["cache"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "codex": {"foo": 1},
                    "_cached_at": int(time.time()) - 60,  # 60s ago
                }
            )
        )
        assert load_cache(30) is None

    def test_corrupt_cache_returns_none(self, isolated_paths):
        path = isolated_paths["cache"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this ain't JSON")
        assert load_cache(30) is None

    def test_missing_cached_at_treated_as_stale(self, isolated_paths):
        """If _cached_at is missing, the cache should be treated as stale (epoch 0)."""
        path = isolated_paths["cache"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"codex": {}}))
        assert load_cache(30) is None

    def test_non_dict_json_null_returns_none(self, isolated_paths):
        """Regression H1: cache content `"null"` parses as `None` — the old
        code then crashed with AttributeError on `None.get(...)`. Must now
        return None gracefully so the cache self-heals on next write."""
        path = isolated_paths["cache"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("null")
        assert load_cache(30) is None

    def test_non_dict_json_list_returns_none(self, isolated_paths):
        """Regression H1: cache content `[]` must not crash the caller."""
        path = isolated_paths["cache"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]")
        assert load_cache(30) is None

    def test_non_dict_json_number_returns_none(self, isolated_paths):
        """Regression H1: cache content `42` (a bare number) must return None."""
        path = isolated_paths["cache"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("42")
        assert load_cache(30) is None

    def test_non_dict_json_string_returns_none(self, isolated_paths):
        """Regression H1: cache content `"hello"` must return None."""
        path = isolated_paths["cache"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"hello"')
        assert load_cache(30) is None

    def test_non_numeric_cached_at_returns_none(self, isolated_paths):
        """Regression H1: a valid dict with non-numeric _cached_at used to
        ValueError in the TTL math. Must return None cleanly."""
        path = isolated_paths["cache"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"_cached_at": "not-a-number", "codex": {}}))
        assert load_cache(30) is None


class TestSaveCache:
    def test_creates_parent_dir(self, isolated_paths):
        assert not isolated_paths["cache"].parent.exists()
        save_cache({"codex": {"x": 1}})
        assert isolated_paths["cache"].exists()

    def test_writes_valid_json(self, isolated_paths):
        save_cache({"codex": {"x": 1}, "claude": {"y": 2}})
        data = json.loads(isolated_paths["cache"].read_text(encoding="utf-8"))
        assert data["codex"] == {"x": 1}
        assert data["claude"] == {"y": 2}
        assert "_cached_at" in data

    def test_stamps_timestamp(self, isolated_paths):
        before = int(time.time())
        save_cache({"codex": {}})
        after = int(time.time())
        data = json.loads(isolated_paths["cache"].read_text(encoding="utf-8"))
        assert before <= data["_cached_at"] <= after

    def test_roundtrip_via_load_cache(self, isolated_paths):
        original = {"codex": {"hello": "world"}, "claude": None}
        save_cache(original)
        loaded = load_cache(ttl=30)
        assert loaded is not None
        assert loaded["codex"] == {"hello": "world"}
        assert loaded["claude"] is None
