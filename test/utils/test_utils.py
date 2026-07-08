"""Tests for utils.utils — atomic_write and preserve_corrupt helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from utils.utils import atomic_write, preserve_corrupt


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


class TestAtomicWrite:
    def test_writes_content(self, tmpdir):
        target = tmpdir / "out.json"
        atomic_write(target, '{"a": 1}')
        assert target.read_text(encoding="utf-8") == '{"a": 1}'

    def test_creates_missing_parent_dirs(self, tmpdir):
        target = tmpdir / "nested" / "deep" / "out.txt"
        atomic_write(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_overwrites_existing(self, tmpdir):
        target = tmpdir / "out.txt"
        target.write_text("old", encoding="utf-8")
        atomic_write(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

    def test_leaves_no_tmp_file(self, tmpdir):
        target = tmpdir / "out.txt"
        atomic_write(target, "data")
        assert list(tmpdir.glob("*.tmp")) == []


class TestPreserveCorrupt:
    def test_renames_aside_with_timestamp(self, tmpdir):
        f = tmpdir / "data.json"
        f.write_text("corrupt{", encoding="utf-8")
        backup = preserve_corrupt(f)
        assert backup is not None
        assert not f.exists()
        assert backup.name.startswith("data.json.corrupt-")
        assert backup.read_text(encoding="utf-8") == "corrupt{"

    def test_missing_file_returns_none(self, tmpdir):
        assert preserve_corrupt(tmpdir / "nope.json") is None
