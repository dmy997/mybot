"""Tests for utils/images.py — image-to-data-URL conversion."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import pytest

from utils.images import MAX_IMAGE_BYTES, SUPPORTED_IMAGE_EXTENSIONS, file_to_data_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_temp_image(suffix: str = ".png", content: bytes = b"\x89PNG\r\n\x1a\nfake") -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(content)
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFileToDataUrl:
    def test_png(self):
        path = _write_temp_image(".png")
        try:
            result = file_to_data_url(path)
            assert result is not None
            assert result.startswith("data:image/png;base64,")
            b64 = result.split(",", 1)[1]
            assert base64.b64decode(b64) == b"\x89PNG\r\n\x1a\nfake"
        finally:
            Path(path).unlink(missing_ok=True)

    def test_jpg(self):
        path = _write_temp_image(".jpg")
        try:
            result = file_to_data_url(path)
            assert result is not None
            assert result.startswith("data:image/jpeg;base64,")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_jpeg(self):
        path = _write_temp_image(".jpeg")
        try:
            result = file_to_data_url(path)
            assert result is not None
            assert result.startswith("data:image/jpeg;base64,")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_gif(self):
        path = _write_temp_image(".gif")
        try:
            result = file_to_data_url(path)
            assert result is not None
            assert result.startswith("data:image/gif;base64,")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_webp(self):
        path = _write_temp_image(".webp")
        try:
            result = file_to_data_url(path)
            assert result is not None
            assert result.startswith("data:image/webp;base64,")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_file_not_found(self):
        assert file_to_data_url("/nonexistent/image.png") is None

    def test_unsupported_extension(self):
        path = _write_temp_image(".bmp")
        try:
            assert file_to_data_url(path) is None
        finally:
            Path(path).unlink(missing_ok=True)

    def test_non_image_extension(self):
        path = _write_temp_image(".txt")
        try:
            assert file_to_data_url(path) is None
        finally:
            Path(path).unlink(missing_ok=True)

    def test_empty_file(self):
        path = _write_temp_image(".png", content=b"")
        try:
            result = file_to_data_url(path)
            assert result is not None
            assert result.endswith("base64,")
        finally:
            Path(path).unlink(missing_ok=True)


class TestConstants:
    def test_supported_extensions_are_lowercase(self):
        for ext in SUPPORTED_IMAGE_EXTENSIONS:
            assert ext == ext.lower()
            assert ext.startswith(".")

    def test_max_bytes_is_positive(self):
        assert MAX_IMAGE_BYTES > 0
