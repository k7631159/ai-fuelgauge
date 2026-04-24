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
