import os
import time
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from loguru import logger

_TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "prompt_templates"


@lru_cache
def _environment() -> Environment:
    # Plain-text prompts: do not HTML-escape variable values.
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_ROOT)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )

def render_template(name: str, *, strip: bool = False, **kwargs: Any) -> str:
    """Render ``name`` (e.g. ``agent/identity.md``, ``agent/platform_policy.md``) under ``templates/``.

    Use ``strip=True`` for single-line user-facing strings when the file ends
    with a trailing newline you do not want preserved.
    """
    text = _environment().get_template(name).render(**kwargs)
    return text.rstrip() if strip else text

def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically and durably.

    Uses a temp-file + ``fsync`` + ``os.replace`` + parent-dir ``fsync``
    sequence so a crash or SIGKILL mid-write cannot leave the destination
    truncated or lost.  Without the parent-dir fsync the rename itself may
    not survive a power loss, silently dropping the file on next boot.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Persist the rename.  Skip on platforms (Windows) where opening a
        # directory raises PermissionError — NTFS journals metadata synchronously.
        with suppress(PermissionError):
            fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def preserve_corrupt(path: Path) -> Path | None:
    """Rename a corrupt data file aside as ``<name>.corrupt-<ts>``.

    Preserves the original bytes for forensic recovery instead of letting a
    later atomic write overwrite (and permanently lose) recoverable data.
    Returns the backup path, or ``None`` if *path* did not exist / could not
    be renamed.
    """
    path = Path(path)
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
    try:
        path.rename(backup)
        return backup
    except OSError:
        logger.warning("Failed to preserve corrupt file {}", path)
        return None
