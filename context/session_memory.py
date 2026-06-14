"""SessionMemory — per-conversation structured notes for compaction quality.

Maintains a continuously-updated notes file (``sessions/<key>_notes.md``)
that captures the key information from each exchange in a structured format.

Reference: Claude Code ``SessionMemory`` system.

Two update modes:

1. **Rule-based** (``update()``) — appends a short structured entry with query,
   tools used, and response snippet.  No LLM call.  Always runs.

2. **Fork-agent** (``update_async()``) — calls a lightweight LLM to extract
   key decisions, modified files, task progress, and errors from the exchange,
   then merges them into the appropriate template sections.  Runs as a
   fire-and-forget background task.

Why this matters:
    When ``CompactionService`` needs to summarise old messages, it can use
    these structured notes as input instead of raw dehydrated messages.
    Structured notes produce better summaries because they already distill
    the key facts, decisions, and context — reducing information loss
    during compression.  If the notes are of sufficient quality, the
    summarisation LLM call can be **skipped entirely** (Path B shortcut),
    saving one API call.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

NOTES_TEMPLATE = """# Session Notes

## Current Task
(no task recorded yet)

## Key Decisions
(none yet)

## Files Modified
(none yet)

## Errors & Fixes
(none yet)

## Work Log
"""

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_MAX_NOTES_TOKENS = 12_000  # max tokens for truncate_for_context
_ESTIMATE_CHARS_PER_TOKEN = 4
_MIN_QUALITY_CHARS = 400     # notes must have >400 chars of real content for Path B
_MAX_NOTES_AGE_SECONDS = 3600  # notes older than 1h are "stale" for Path B


class SessionMemory:
    """Per-conversation structured notes.

    Parameters
    ----------
    session_key:
        Session identifier used in the notes filename.
    notes_path:
        Full path to the notes file (typically
        ``workspace/sessions/<key>_notes.md``).
    """

    def __init__(self, session_key: str, notes_path: Path) -> None:
        self.session_key = session_key
        self.path = Path(notes_path)
        self._last_fork_update: datetime | None = None
        self._ensure_file()

    # ========================================================================
    # File lifecycle
    # ========================================================================

    def _ensure_file(self) -> None:
        """Create the notes file from template if it doesn't exist."""
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(NOTES_TEMPLATE, encoding="utf-8")
            logger.debug("Session notes created for {!r}", self.session_key)

    def exists(self) -> bool:
        """Return True if the notes file exists on disk."""
        return self.path.exists()

    def read(self) -> str:
        """Read the full notes file."""
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")

    def write(self, content: str) -> None:
        """Overwrite the notes file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content, encoding="utf-8")

    # ========================================================================
    # Rule-based update (always runs, no LLM call)
    # ========================================================================

    def update(
        self,
        user_input: str = "",
        assistant_content: str = "",
        tools_used: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        """Append a structured entry to the notes file.

        Called after each ``save_exchange()`` to keep notes up to date.
        This is the fast, synchronous path — no LLM call.

        Parameters
        ----------
        user_input:
            The user's message.
        assistant_content:
            The assistant's final response (first 300 chars used).
        tools_used:
            List of tool names used during the exchange.
        errors:
            List of error messages encountered.
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        parts = [f"\n### {ts}"]

        # User query (truncated)
        if user_input:
            short = user_input[:200].replace("\n", " ")
            if len(user_input) > 200:
                short += "..."
            parts.append(f"- **Query**: {short}")

        # Tools used
        if tools_used:
            parts.append(f"- **Tools**: {', '.join(tools_used[:10])}")

        # Assistant response snippet
        if assistant_content:
            snippet = assistant_content[:300].replace("\n", " ")
            if len(assistant_content) > 300:
                snippet += "..."
            parts.append(f"- **Response**: {snippet}")

        # Errors
        if errors:
            for err in errors[:3]:
                parts.append(f"- **Error**: {err[:200]}")

        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n".join(parts) + "\n")
        except OSError:
            logger.opt(exception=True).warning(
                "Failed to update session notes for {!r}", self.session_key,
            )

    # ========================================================================
    # Fork-agent update (LLM-extracted, fire-and-forget)
    # ========================================================================

    async def update_async(
        self,
        provider: Any,
        user_input: str,
        assistant_content: str,
        *,
        tools_used: list[str] | None = None,
        errors: list[str] | None = None,
        model: str | None = None,
        max_tokens: int = 300,
    ) -> None:
        """Use a lightweight LLM to extract structured info and update notes.

        This is the "fork agent" — it runs a cheap side-query that reads the
        current notes + the latest exchange and extracts:

        - **Key decisions** made in this exchange
        - **Files modified** (if tools were used)
        - **Task progress** (advances the Current Task section)
        - **New errors** encountered

        The LLM response is merged into the appropriate template sections.
        When the provider is unavailable, falls back to :meth:`update`.

        Parameters
        ----------
        provider:
            LLM provider for the extraction call (use cheap model).
        user_input:
            The user's message.
        assistant_content:
            The assistant's response (truncated to first 2000 chars for the prompt).
        tools_used:
            Tool names used during the exchange.
        errors:
            Error messages encountered.
        model:
            Optional model override (defaults to provider's default, typically
            the configured lightweight model).
        max_tokens:
            Max tokens for the extraction response.
        """
        if provider is None:
            self.update(user_input, assistant_content, tools_used, errors)
            return

        current_notes = self.read()
        prompt = self._build_fork_prompt(
            current_notes, user_input, assistant_content,
            tools_used=tools_used, errors=errors,
        )

        try:
            response = await provider.chat_with_retry(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                model=model,
                max_tokens=max_tokens,
                temperature=0.0,
            )
            content = response.content or ""
            if content.strip():
                self._apply_fork_update(current_notes, content.strip())
                self._last_fork_update = datetime.now(timezone.utc)
                logger.debug(
                    "Fork-agent updated session notes for {!r}", self.session_key,
                )
        except Exception:
            logger.opt(exception=True).debug(
                "Fork-agent update failed for {!r}, using rule-based fallback",
                self.session_key,
            )
            self.update(user_input, assistant_content, tools_used, errors)

    def _build_fork_prompt(
        self,
        current_notes: str,
        user_input: str,
        assistant_content: str,
        *,
        tools_used: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> str:
        """Build the extraction prompt for the fork agent."""
        parts: list[str] = []

        parts.append("You are a session note-keeper.  Below is the current session "
                      "notes file followed by the latest exchange.")
        parts.append("")
        parts.append("## Current Session Notes")
        parts.append("```")
        parts.append(current_notes[-4000:] if len(current_notes) > 4000 else current_notes)
        parts.append("```")
        parts.append("")
        parts.append("## Latest Exchange")
        parts.append(f"- **User Query**: {user_input[:500]}")
        snippet = assistant_content[:2000].replace("\n", " ")
        parts.append(f"- **Assistant Response**: {snippet}")
        if tools_used:
            parts.append(f"- **Tools Used**: {', '.join(tools_used[:10])}")
        if errors:
            for err in errors[:3]:
                parts.append(f"- **Error**: {err[:200]}")
        parts.append("")
        parts.append("## Instructions")
        parts.append("Extract the following from the latest exchange and return "
                      "ONLY a Markdown snippet to append to the notes file:")
        parts.append("")
        parts.append("1. **Current Task**: If the task changed, write a one-line update. "
                      "If unchanged, write \"(unchanged)\".")
        parts.append("2. **Key Decisions**: List any decisions made in THIS exchange "
                      "(max 3 bullet points). If none, write \"(none)\".")
        parts.append("3. **Files Modified**: List files that were read/written/created "
                      "by tools in THIS exchange. If none, write \"(none)\".")
        parts.append("4. **Errors & Fixes**: List new errors encountered and how they "
                      "were resolved. If none, write \"(none)\".")
        parts.append("")
        parts.append("Format your response EXACTLY as follows (the parser expects "
                      "these section markers):")
        parts.append("")
        parts.append("<!-- TASK -->")
        parts.append("one-line task update or (unchanged)")
        parts.append("<!-- DECISIONS -->")
        parts.append("- decision 1")
        parts.append("- decision 2")
        parts.append("<!-- FILES -->")
        parts.append("- path/to/file.py (read)")
        parts.append("- path/to/other.py (modified)")
        parts.append("<!-- ERRORS -->")
        parts.append("- error description → how it was fixed")

        return "\n".join(parts)

    def _apply_fork_update(self, current_notes: str, llm_response: str) -> None:
        """Parse the LLM response and merge into the template sections."""
        # Extract each section from the LLM response
        sections: dict[str, str] = {}
        markers = ["<!-- TASK -->", "<!-- DECISIONS -->", "<!-- FILES -->", "<!-- ERRORS -->"]
        for i, marker in enumerate(markers):
            start = llm_response.find(marker)
            if start < 0:
                continue
            start += len(marker)
            if i + 1 < len(markers):
                end = llm_response.find(markers[i + 1], start)
            else:
                end = len(llm_response)
            section_text = llm_response[start:end].strip()
            if section_text and section_text.lower() not in ("(none)", "(unchanged)", "none", "unchanged"):
                sections[marker] = section_text

        if not sections:
            return

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Build updated notes by replacing section placeholders or appending
        updated = current_notes

        # Current Task
        if "<!-- TASK -->" in sections:
            task = sections["<!-- TASK -->"]
            updated = self._replace_section_content(
                updated, "## Current Task", task,
            )

        # Key Decisions
        if "<!-- DECISIONS -->" in sections:
            decs = sections["<!-- DECISIONS -->"]
            updated = self._replace_section_content(
                updated, "## Key Decisions", decs, append=True,
            )

        # Files Modified
        if "<!-- FILES -->" in sections:
            files = sections["<!-- FILES -->"]
            updated = self._replace_section_content(
                updated, "## Files Modified", files, append=True,
            )

        # Errors & Fixes
        if "<!-- ERRORS -->" in sections:
            errs = sections["<!-- ERRORS -->"]
            updated = self._replace_section_content(
                updated, "## Errors & Fixes", errs, append=True,
            )

        # Always append a Work Log entry
        updated += f"\n### {ts}\n- **Query**: {sections.get('<!-- TASK -->', '(exchange recorded)')[:200]}"

        self.write(updated)

    @staticmethod
    def _replace_section_content(
        notes: str, section_header: str, new_content: str, *, append: bool = False,
    ) -> str:
        """Replace or append content under a markdown section header.

        Finds ``section_header`` in *notes* and replaces everything between it
        and the next ``##`` header or end-of-file.

        If *append* is True and the section already has non-placeholder content,
        the new content is appended after existing lines.
        """
        header_idx = notes.find(section_header)
        if header_idx < 0:
            return notes + f"\n\n{section_header}\n{new_content}"

        # Find the next ## header after this section
        body_start = header_idx + len(section_header)
        next_header = notes.find("\n## ", body_start)
        if next_header < 0:
            next_header = len(notes)

        existing = notes[body_start:next_header].strip()
        existing_clean = existing.replace("(none yet)", "").replace("(no task recorded yet)", "").strip()

        if append and existing_clean:
            combined = existing_clean + "\n" + new_content
        else:
            combined = new_content

        return notes[:body_start] + "\n" + combined + "\n" + notes[next_header:]

    # ========================================================================
    # Quality scoring (for Path B gating)
    # ========================================================================

    def quality_score(self) -> int:
        """Return a 0-100 heuristic score for notes quality.

        Used by CompactionService to decide whether to skip the summarisation
        LLM call (Path B).  Scores >= 50 are considered sufficient.

        Factors:
        - Content length (more = better, up to 40 pts)
        - Section richness (how many sections have non-placeholder content, up to 40 pts)
        - Recency (how recently fork-agent updated, up to 20 pts)
        """
        content = self.read()
        if not content.strip() or content.strip() == NOTES_TEMPLATE.strip():
            return 0

        score = 0

        # Length score (0-40): notes > 2000 chars = full marks
        content_len = len(content)
        score += min(40, content_len // 50)

        # Section richness (0-40): how many sections have real content
        sections = ["## Current Task", "## Key Decisions", "## Files Modified", "## Errors & Fixes"]
        populated = 0
        for section in sections:
            section_content = self._extract_section(content, section)
            if section_content and section_content not in (
                "(no task recorded yet)", "(none yet)", "(none)",
            ) and len(section_content.strip()) > 10:
                populated += 1
        score += populated * 10

        # Recency (0-20): fork-agent updated in last 30 min = full marks
        if self._last_fork_update is not None:
            age = (datetime.now(timezone.utc) - self._last_fork_update).total_seconds()
            if age < 1800:
                score += 20
            elif age < 3600:
                score += 10
            else:
                score += 5

        return min(score, 100)

    def is_fresh(self) -> bool:
        """Return True if notes are recent enough for Path B bypass."""
        if self._last_fork_update is None:
            return False
        age = (datetime.now(timezone.utc) - self._last_fork_update).total_seconds()
        return age < _MAX_NOTES_AGE_SECONDS

    def has_substance(self) -> bool:
        """Return True if notes have enough content to serve as a summary."""
        content = self.read()
        template = NOTES_TEMPLATE.strip()
        real_content = content.strip()
        if real_content == template or len(real_content) <= len(template) + 100:
            return False
        return len(real_content) > _MIN_QUALITY_CHARS

    @staticmethod
    def _extract_section(content: str, section_header: str) -> str:
        """Extract the text under a markdown section header."""
        idx = content.find(section_header)
        if idx < 0:
            return ""
        start = idx + len(section_header)
        next_header = content.find("\n## ", start)
        if next_header < 0:
            return content[start:].strip()
        return content[start:next_header].strip()

    # ========================================================================
    # Compact input (for CompactionService)
    # ========================================================================

    def get_compact_input(
        self, fallback_messages: list[dict[str, Any]] | None = None,
    ) -> str:
        """Format session notes as input for LLM summarisation.

        Structured notes produce higher-quality summaries than raw dehydrated
        messages because the key information has already been distilled.

        If the notes file is empty or unavailable, falls back to *fallback_messages*
        (typically dehydrated raw messages).
        """
        notes = self.read()
        if notes.strip() and notes.strip() != NOTES_TEMPLATE.strip():
            return (
                "# Session Notes (for context summarisation)\n\n"
                "The following are structured notes from the conversation. "
                "Use them to produce a concise summary.\n\n"
                f"{notes}"
            )

        # Fallback: format messages as text
        if fallback_messages:
            lines: list[str] = []
            for msg in fallback_messages:
                role = msg.get("role", "?")
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    snippet = content[:300].replace("\n", " ")
                    lines.append(f"[{role}] {snippet}")
            return "\n".join(lines) if lines else "(empty)"

        return "(no session notes available)"

    def get_compact_summary(self, max_chars: int = 2000) -> str:
        """Return notes content formatted as a compression summary.

        This is the Path B output — used directly as the compression summary
        without an LLM call.  Extracts only the non-template sections.
        """
        content = self.read()
        if not content.strip() or content.strip() == NOTES_TEMPLATE.strip():
            return ""

        parts: list[str] = []
        sections = [
            ("## Current Task", "Current Task"),
            ("## Key Decisions", "Key Decisions"),
            ("## Files Modified", "Files Modified"),
            ("## Errors & Fixes", "Errors & Fixes"),
        ]

        for header, label in sections:
            section_content = self._extract_section(content, header)
            if section_content and section_content not in (
                "(no task recorded yet)", "(none yet)", "(none)",
            ) and len(section_content.strip()) > 5:
                parts.append(f"### {label}\n{section_content}")

        if not parts:
            return ""

        result = "\n\n".join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... (truncated)"
        return result

    # ========================================================================
    # Truncation for context injection
    # ========================================================================

    def truncate_for_context(self, max_chars: int | None = None) -> str:
        """Return notes truncated for injection into the system prompt.

        Caps the content at *max_chars* (default derived from
        ``_MAX_NOTES_TOKENS`` × ``_ESTIMATE_CHARS_PER_TOKEN``).
        """
        limit = max_chars if max_chars is not None else _MAX_NOTES_TOKENS * _ESTIMATE_CHARS_PER_TOKEN
        notes = self.read()
        if not notes.strip() or notes.strip() == NOTES_TEMPLATE.strip():
            return ""

        if len(notes) <= limit:
            return notes

        # Truncate to limit, keeping the header
        header_end = notes.find("## Work Log")
        if header_end > 0 and header_end < limit:
            # Keep header + as much of work log as fits
            header = notes[:header_end]
            remaining = limit - len(header)
            body_start = header_end
            truncated_body = notes[body_start:body_start + remaining]
            return header + truncated_body + "\n... (truncated)"
        else:
            return notes[:limit] + "\n... (truncated)"

    # ========================================================================
    # Reset
    # ========================================================================

    def reset(self) -> None:
        """Reset notes to the empty template."""
        self.path.unlink(missing_ok=True)
        self._ensure_file()
        self._last_fork_update = None
        logger.debug("Session notes reset for {!r}", self.session_key)
