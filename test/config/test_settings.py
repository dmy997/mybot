"""Tests for config/settings.py — model lookup, thresholds, settings loading."""

from __future__ import annotations

import json
from pathlib import Path

from config.settings import (
    ModelWindowConfig,
    _DEFAULT_MODELS,
    _DEFAULT_THRESHOLDS,
    _load_settings,
    generate_default_settings,
    get_settings,
    load_thresholds,
    lookup_model,
    reload_settings,
    resolve_context_window,
)


class TestLoadSettings:
    def test_missing_file_returns_empty(self, tmp_path: Path):
        result = _load_settings(tmp_path / "nonexistent.json")
        assert result == {}

    def test_valid_json(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text('{"models": [], "thresholds": {"compress_ratio": 0.8}}')
        result = _load_settings(path)
        assert result == {"models": [], "thresholds": {"compress_ratio": 0.8}}

    def test_invalid_json_returns_empty(self, tmp_path: Path):
        path = tmp_path / "bad.json"
        path.write_text("{not json}")
        result = _load_settings(path)
        assert result == {}

    def test_non_dict_returns_empty(self, tmp_path: Path):
        path = tmp_path / "list.json"
        path.write_text("[1, 2, 3]")
        result = _load_settings(path)
        assert result == {}


class TestLookupModel:
    def test_exact_match(self):
        models = [{"pattern": "gpt-4o", "context_window": 128000, "max_output_tokens": 16384}]
        result = lookup_model("gpt-4o", models)
        assert result.context_window == 128000
        assert result.max_output_tokens == 16384

    def test_fnmatch_wildcard(self):
        result = lookup_model("deepseek/deepseek-v4-flash", _DEFAULT_MODELS)
        assert result.context_window == 200_000
        assert result.max_output_tokens == 20_000

    def test_fnmatch_prefix(self):
        models = [{"pattern": "gpt-4o*", "context_window": 128000, "max_output_tokens": 16384}]
        result = lookup_model("gpt-4o-mini", models)
        assert result.context_window == 128000

    def test_fallback_to_catch_all(self):
        result = lookup_model("unknown-model-xyz", _DEFAULT_MODELS)
        assert result.context_window == 200_000
        assert result.max_output_tokens == 20_000

    def test_first_match_wins(self):
        models = [
            {"pattern": "gpt-4o*", "context_window": 128000, "max_output_tokens": 16384},
            {"pattern": "gpt-4*", "context_window": 64000, "max_output_tokens": 8192},
        ]
        result = lookup_model("gpt-4o-mini", models)
        assert result.context_window == 128000

    def test_missing_keys_use_defaults(self):
        models = [{"pattern": "test-*"}]
        result = lookup_model("test-model", models)
        assert result.context_window == 200_000
        assert result.max_output_tokens == 20_000

    def test_no_models_uses_builtin_defaults(self, monkeypatch):
        monkeypatch.setattr("config.settings.get_settings", lambda: {"models": []})
        monkeypatch.setattr("config.settings._settings", None)
        result = lookup_model("any-model")
        assert result.context_window == 200_000

    def test_resolve_context_window_convenience(self):
        result = resolve_context_window("gpt-4o-mini")
        assert isinstance(result, ModelWindowConfig)
        assert result.context_window > 0


class TestLoadThresholds:
    def test_all_defaults_when_missing(self):
        result = load_thresholds({})
        assert result.warning_buffer_ratio == _DEFAULT_THRESHOLDS["warning_buffer_ratio"]
        assert result.auto_compact_buffer_ratio == _DEFAULT_THRESHOLDS["auto_compact_buffer_ratio"]
        assert result.block_buffer_ratio == _DEFAULT_THRESHOLDS["block_buffer_ratio"]
        assert result.compress_ratio == _DEFAULT_THRESHOLDS["compress_ratio"]
        assert result.consolidation_ratio == _DEFAULT_THRESHOLDS["consolidation_ratio"]
        assert result.idle_compress_seconds == _DEFAULT_THRESHOLDS["idle_compress_seconds"]

    def test_custom_values_propagate(self):
        data = {"thresholds": {"compress_ratio": 0.8, "idle_compress_seconds": 600}}
        result = load_thresholds(data)
        assert result.compress_ratio == 0.8
        assert result.idle_compress_seconds == 600
        assert result.consolidation_ratio == _DEFAULT_THRESHOLDS["consolidation_ratio"]

    def test_non_dict_thresholds_uses_defaults(self):
        result = load_thresholds({"thresholds": [1, 2, 3]})
        assert result.compress_ratio == _DEFAULT_THRESHOLDS["compress_ratio"]

    def test_none_data_uses_get_settings(self, monkeypatch):
        monkeypatch.setattr(
            "config.settings.get_settings",
            lambda: {"thresholds": {"compress_ratio": 0.6}},
        )
        monkeypatch.setattr("config.settings._settings", None)
        result = load_thresholds()
        assert result.compress_ratio == 0.6


class TestGenerateDefaultSettings:
    def test_generates_file(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        result = generate_default_settings(path)
        assert result == path
        assert path.exists()
        data = json.loads(path.read_text())
        assert "models" in data
        assert "thresholds" in data
        assert len(data["models"]) == len(_DEFAULT_MODELS)

    def test_no_overwrite_existing(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text('{"custom": true}')
        generate_default_settings(path)
        assert json.loads(path.read_text()) == {"custom": True}


class TestReloadSettings:
    def test_reload_clears_cache(self, tmp_path: Path, monkeypatch):
        path = tmp_path / "reload_test.json"
        monkeypatch.setattr("config.settings._settings_path", lambda: path)
        monkeypatch.setattr("config.settings._settings", None)
        path.write_text('{"custom": "first"}')
        assert get_settings() == {"custom": "first"}
        path.write_text('{"custom": "second"}')
        result = reload_settings()
        assert result == {"custom": "second"}
