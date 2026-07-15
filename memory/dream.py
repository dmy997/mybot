"""Dream — periodic LLM-driven memory consolidation (nanobot-style).

Triggered by CronScheduler (default every 2 hours).  Reads new entries
from ``history.jsonl`` since the last dream cursor, compares them with
the current SOUL.md, USER.md, and MEMORY.md, then asks the LLM to
produce ``[FILE]`` and ``[FILE-REMOVE]`` directives.

Phase 1 (LLM analysis) produces structured directives.
Phase 2 (programmatic merge) applies them surgically to each file.

Reference: nanobot ``agent/memory.py`` Dream class.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING

from loguru import logger

from utils import render_template

if TYPE_CHECKING:
    from memory.store import MemoryStore
    from providers.base import LLMProvider

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_BATCH_SIZE = 20          # max history entries per Dream run
_MAX_HISTORY_CHARS = 24_000   # per-entry content cap for LLM prompt

_DIRECTIVE_RE = re.compile(
    r"^\[(FILE|FILE-REMOVE)\]"
    r"\s+(SOUL\.md|USER\.md|MEMORY\.md)\s*:\s*(.+)$",
)

_SKILL_RE = re.compile(
    r"^\[SKILL\]\s+([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\s*:\s*(.+)$",
)


class Dream:
    """Periodic LLM-driven memory consolidation for SOUL.md, USER.md, MEMORY.md.

    Parameters
    ----------
    store:
        The :class:`MemoryStore` for file I/O.
    provider:
        The LLM provider (cheap model preferred).
    model:
        Model name override for Dream calls.
    """

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider | None = None,
        model: str = "",
    ):
        self.store = store
        self.provider = provider
        self.model = model

    # -- public API -----------------------------------------------------------

    async def run(self) -> bool:
        """Execute one Dream cycle.

        Returns True if any memory file was modified.
        """
        if self.provider is None:
            logger.debug("Dream: no provider configured, skipping")
            return False

        # 1. Read new history entries since last dream cursor
        dream_cursor = self.store.get_dream_cursor()
        new_entries = self.store.read_history(since_cursor=dream_cursor)
        if not new_entries:
            logger.debug("Dream: no new history entries (cursor={})", dream_cursor)
            return False

        if len(new_entries) > _MAX_BATCH_SIZE:
            logger.info(
                "Dream: capping batch from {} to {} entries",
                len(new_entries), _MAX_BATCH_SIZE,
            )
            new_entries = new_entries[-_MAX_BATCH_SIZE:]

        # 2. Read current memory files
        soul = self.store.read_soul()
        user = self.store.read_user()
        current_memory = self.store.read_memory_file()

        # Update age annotations in MEMORY.md before LLM analysis
        today = date.today().isoformat()
        last_date = self.store.get_dream_date()
        if last_date and last_date != today:
            updated_memory = self._update_age_annotations(current_memory, last_date, today)
            if updated_memory is not None:
                current_memory = updated_memory
                self.store.write_memory_file(current_memory)

        # 3. Phase 1 — LLM analysis → directives
        logger.info(
            "Dream: processing {} new history entries (cursor {} → latest)",
            len(new_entries), dream_cursor,
        )

        existing_skills = self._list_existing_skills()

        try:
            directives = await self._call_llm(
                soul, user, current_memory, new_entries,
                existing_skills=existing_skills,
            )
        except Exception:
            logger.exception("Dream Phase 1 LLM call failed")
            return False

        if not directives:
            logger.debug("Dream: LLM returned no directives")
            self._advance_cursor(new_entries)
            return False

        # 4. Phase 2 — apply directives
        adds, removes, skills = self._parse_directives(directives)
        if not adds and not removes and not skills:
            logger.debug("Dream: no valid directives parsed")
            self._advance_cursor(new_entries)
            return False

        changed = False
        changed |= self._apply_adds(adds)
        changed |= self._apply_removes(removes)
        changed |= self._apply_skills(skills)

        # 5. Advance cursor and record dream date
        self.store.set_dream_date(today)
        if changed:
            self._advance_cursor(new_entries)
            logger.info(
                "Dream: applied {} add(s), {} remove(s), {} skill(s)",
                len(adds), len(removes), len(skills),
            )
        else:
            self._advance_cursor(new_entries)
            logger.debug("Dream: directives parsed but no file changes made")

        return changed

    # -- Phase 1 (LLM) --------------------------------------------------------

    async def _call_llm(
        self,
        soul: str,
        user: str,
        current_memory: str,
        new_entries: list[dict],
        existing_skills: str = "",
    ) -> str | None:
        """Call the LLM and return the raw directive text."""
        formatted_entries = self._format_entries(new_entries)

        response = await self.provider.chat_with_retry(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": render_template(
                        "agent/dream_phase1.md",
                        strip=True,
                    ),
                },
                {
                    "role": "user",
                    "content": render_template(
                        "agent/dream_user.md",
                        soul=soul or "(empty)",
                        user=user or "(empty)",
                        current_memory=current_memory or "(empty — no long-term memories yet)",
                        new_entries=formatted_entries,
                        existing_skills=existing_skills or "(none)",
                        strip=False,
                    ),
                },
            ],
            tools=[],
        )

        if response.finish_reason == "error":
            raise RuntimeError(f"Dream LLM returned error: {response.content}")

        content = response.content
        if not content or not content.strip():
            return None
        return content.strip()

    # -- Phase 2 (parse + apply) ----------------------------------------------

    @staticmethod
    def _parse_directives(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
        """Parse LLM output into (adds, removes, skills).

        Each add/remove is ``(file_key, content)`` where *file_key* is one
        of ``"SOUL"``, ``"USER"``, ``"MEMORY"``.
        Each skill is ``(skill_name, description)``.
        """
        adds: list[tuple[str, str]] = []
        removes: list[tuple[str, str]] = []
        skills: list[tuple[str, str]] = []

        # Normalise file names: "SOUL.md" / "SOUL" → "SOUL"
        _file_key = {"SOUL.md": "SOUL", "USER.md": "USER", "MEMORY.md": "MEMORY"}

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.upper() == "[SKIP]":
                continue

            # Try [SKILL] first — different format from FILE/FILE-REMOVE
            skill_m = _SKILL_RE.match(line)
            if skill_m:
                skill_name = skill_m.group(1).strip()
                skill_desc = skill_m.group(2).strip()
                if skill_name and skill_desc:
                    skills.append((skill_name, skill_desc))
                else:
                    logger.debug("Dream: unparseable SKILL directive: {!r}", line[:120])
                continue

            m = _DIRECTIVE_RE.match(line)
            if not m:
                _DIRECTIVE_RE_UNQUOTED = re.compile(
                    r"^\[(FILE|FILE-REMOVE)\]\s+(.+?)\s*:\s*(.+)$"
                )
                m = _DIRECTIVE_RE_UNQUOTED.match(line)
                if not m:
                    logger.debug("Dream: unparseable directive: {!r}", line[:120])
                    continue
                directive_kind = m.group(1)
                file_part = m.group(2).strip()
                content = m.group(3).strip()
                file_key = _file_key.get(file_part)
                if file_key is None:
                    # Try without .md suffix
                    _alt = {"SOUL": "SOUL", "USER": "USER", "MEMORY": "MEMORY"}
                    file_key = _alt.get(file_part.upper())
                if file_key is None:
                    logger.debug("Dream: unknown file in directive: {!r}", line[:120])
                    continue
            else:
                directive_kind = m.group(1)
                file_key = _file_key.get(m.group(2))
                if file_key is None:
                    logger.debug("Dream: unknown file: {!r}", m.group(2))
                    continue
                content = m.group(3).strip()

            if directive_kind == "FILE":
                adds.append((file_key, content))
            elif directive_kind == "FILE-REMOVE":
                removes.append((file_key, content))

        return adds, removes, skills

    def _apply_adds(self, adds: list[tuple[str, str]]) -> bool:
        """Append facts to the target files.  Returns True if any file changed.

        Dedup: skips facts whose content already appears in the target file
        (case-insensitive substring match).  MEMORY.md facts get an age
        annotation (``<- 0d``).
        """
        changed = False
        by_file: dict[str, list[str]] = {"SOUL": [], "USER": [], "MEMORY": []}
        for file_key, content in adds:
            by_file[file_key].append(content)

        for file_key, items in by_file.items():
            if not items:
                continue
            current = self._read_file(file_key)
            current_lower = current.lower()
            new_lines: list[str] = []
            for item in items:
                # Dedup: skip if already present (case-insensitive substring)
                if item.lower() in current_lower:
                    logger.debug(
                        "Dream: dedup skipped {}: {!r}",
                        file_key, item[:80],
                    )
                    continue
                if file_key == "MEMORY":
                    new_lines.append(f"- {item}  <- 0d")
                else:
                    new_lines.append(f"- {item}")
            if not new_lines:
                continue
            updated = current.rstrip() + "\n" + "\n".join(new_lines) + "\n"
            self._write_file(file_key, updated)
            logger.info(
                "Dream: appended {} fact(s) to {} ({} deduped)",
                len(new_lines), file_key, len(items) - len(new_lines),
            )
            changed = True

        return changed

    def _apply_removes(self, removes: list[tuple[str, str]]) -> bool:
        """Remove matching content from target files.  Returns True if any changed."""
        changed = False
        # Apply removes one at a time so earlier removes don't shift positions
        for file_key, to_remove in removes:
            current = self._read_file(file_key)
            updated = self._remove_match(current, to_remove)
            if updated is not None and updated != current:
                self._write_file(file_key, updated)
                logger.info(
                    "Dream: removed from {}: {!r}",
                    file_key, to_remove[:80],
                )
                changed = True
            else:
                logger.debug(
                    "Dream: [FILE-REMOVE] not found in {}: {!r}",
                    file_key, to_remove[:80],
                )
        return changed

    @staticmethod
    def _remove_match(text: str, to_remove: str) -> str | None:
        """Try to find and remove *to_remove* from *text*.

        Returns the modified text, or None if no match found.
        """
        if not to_remove.strip():
            return None

        # 1. Exact match (trimmed)
        if to_remove in text:
            return text.replace(to_remove, "", 1)

        # 2. Try matching each line of to_remove independently
        lines = [l for l in to_remove.splitlines() if l.strip()]
        if len(lines) <= 1:
            return None

        # Check if ALL lines are present (anywhere, in order)
        pos = 0
        for line in lines:
            idx = text.find(line, pos)
            if idx == -1:
                return None
            pos = idx + len(line)

        # All lines found — remove the whole block
        first = text.find(lines[0])
        last = text.rfind(lines[-1]) + len(lines[-1])
        if first >= 0 and last > first:
            return text[:first] + text[last:]

        return None

    def _apply_skills(self, skills: list[tuple[str, str]]) -> bool:
        """Create SKILL.md files for extracted workflow skills.

        Returns True if any skill file was created.
        """
        if not skills:
            return False

        skills_dir = self.store.workspace / "skills"
        changed = False

        for skill_name, description in skills:
            skill_dir = skills_dir / skill_name
            skill_file = skill_dir / "SKILL.md"

            if skill_file.exists():
                logger.debug(
                    "Dream: skill {!r} already exists, skipping", skill_name,
                )
                continue

            skill_dir.mkdir(parents=True, exist_ok=True)

            body = self._build_skill_body(skill_name, description)
            tmp = skill_file.with_suffix(".md.tmp")
            try:
                tmp.write_text(body, encoding="utf-8")
                tmp.replace(skill_file)
                logger.info(
                    "Dream: created skill {!r}: {!r}", skill_name, description,
                )
                changed = True
            except Exception:
                logger.exception("Dream: failed to write skill {!r}", skill_name)
                tmp.unlink(missing_ok=True)

        return changed

    @staticmethod
    def _build_skill_body(name: str, description: str) -> str:
        """Build the SKILL.md body with YAML frontmatter."""
        return f"""---
name: {name}
description: {description}
---

# {name}

{description}

## Workflow

<!-- TODO: Refine the workflow steps based on repeated usage patterns. -->

1. Identify the trigger or input for this task
2. Execute the core steps specific to {name}
3. Verify the output and report results

## Notes

This skill was auto-extracted by Dream from repeated conversation patterns.
Review and refine the workflow steps before relying on it.
"""

    @staticmethod
    def _update_age_annotations(text: str, last_date: str, today: str) -> str | None:
        """Increment ``<- Nd`` annotations in *text* if the date changed.

        Returns the updated text, or None if no changes were needed.
        """
        try:
            last = date.fromisoformat(last_date)
            cur = date.fromisoformat(today)
            delta = (cur - last).days
        except (ValueError, TypeError):
            return None

        if delta <= 0:
            return None

        _AGE_RE = re.compile(r"  <- (\d+)d\b")

        def _increment(m: re.Match) -> str:
            old_n = int(m.group(1))
            return f"  <- {old_n + delta}d"

        new_text, count = _AGE_RE.subn(_increment, text)
        if count > 0:
            logger.info("Dream: incremented {} age annotation(s) by {} day(s)", count, delta)
            return new_text
        return None

    # -- file helpers ---------------------------------------------------------

    def _read_file(self, file_key: str) -> str:
        if file_key == "SOUL":
            return self.store.read_soul()
        elif file_key == "USER":
            return self.store.read_user()
        else:
            return self.store.read_memory_file()

    def _write_file(self, file_key: str, content: str) -> None:
        if file_key == "SOUL":
            self.store.write_soul(content)
        elif file_key == "USER":
            self.store.write_user(content)
        else:
            self.store.write_memory_file(content)

    # -- cursor ---------------------------------------------------------------

    def _advance_cursor(self, entries: list[dict]) -> None:
        max_cursor = max(e["cursor"] for e in entries)
        self.store.set_dream_cursor(max_cursor)

    # -- formatting -----------------------------------------------------------

    def _list_existing_skills(self) -> str:
        """Return a formatted list of existing skill names for the LLM prompt."""
        names: set[str] = set(self.store.list_skill_names())

        # Also include builtin skill names so Dream doesn't propose duplicates
        try:
            from core.skills import BUILTIN_SKILLS_DIR
            if BUILTIN_SKILLS_DIR.exists():
                for skill_dir in BUILTIN_SKILLS_DIR.iterdir():
                    if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                        names.add(skill_dir.name)
        except Exception:
            pass

        if not names:
            return "(none)"
        return "\n".join(f"- {name}" for name in sorted(names))

    @staticmethod
    def _format_entries(entries: list[dict]) -> str:
        lines: list[str] = []
        for e in entries:
            ts = e.get("timestamp", "?")
            sk = e.get("session_key", "")
            content = str(e.get("content", ""))
            if len(content) > _MAX_HISTORY_CHARS:
                content = content[:_MAX_HISTORY_CHARS] + "\n... (truncated)"
            meta = f"[{ts}] cursor={e.get('cursor', '?')}"
            if sk:
                meta += f" session={sk}"
            lines.append(f"{meta}\n{content}\n")
        return "\n".join(lines)
