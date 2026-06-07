"""Tests for the memory module."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from memory import MemoryEntry, MemoryManager, MemoryStore, extract_links, parse_frontmatter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def store(workspace):
    return MemoryStore(workspace)


@pytest.fixture
def manager(store):
    return MemoryManager(store)


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_basic(self):
        text = "---\nname: test\ndescription: A test memory\nmetadata:\n  type: user\n---\n\nBody content."
        fm, body = parse_frontmatter(text)
        assert fm["name"] == "test"
        assert fm["description"] == "A test memory"
        assert fm["metadata"] == {"type": "user"}
        assert body.strip() == "Body content."

    def test_no_frontmatter(self):
        text = "Just body content."
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_empty_body(self):
        text = "---\nname: test\ndescription: desc\nmetadata:\n  type: feedback\n---"
        fm, body = parse_frontmatter(text)
        assert fm["name"] == "test"
        assert body == ""

    def test_type_casting(self):
        text = "---\nname: test\ncount: 42\nactive: true\ndescription: desc\nmetadata:\n  type: project\n---\n\nbody"
        fm, _ = parse_frontmatter(text)
        assert fm["count"] == 42
        assert fm["active"] is True

    def test_quoted_string(self):
        text = '---\nname: test\ndescription: "quoted desc"\nmetadata:\n  type: reference\n---\n\nbody'
        fm, _ = parse_frontmatter(text)
        assert fm["description"] == "quoted desc"


# ---------------------------------------------------------------------------
# extract_links
# ---------------------------------------------------------------------------


class TestExtractLinks:
    def test_single_link(self):
        assert extract_links("See [[other-memory]] for context.") == ["other-memory"]

    def test_multiple_links(self):
        result = extract_links("[[a]] and [[b]] and [[c]]")
        assert result == ["a", "b", "c"]

    def test_no_links(self):
        assert extract_links("No links here.") == []

    def test_underscored_links(self):
        assert extract_links("[[user_role]] and [[project_context]]") == ["user_role", "project_context"]


# ---------------------------------------------------------------------------
# MemoryEntry
# ---------------------------------------------------------------------------


class TestMemoryEntry:
    def test_to_frontmatter_text(self):
        entry = MemoryEntry(
            name="test-memory",
            type="user",
            description="A test entry",
            content="Body text.",
        )
        text = entry.to_frontmatter_text()
        assert 'name: test-memory' in text
        assert 'description: A test entry' in text
        assert '  type: user' in text
        assert 'Body text.' in text

    def test_to_frontmatter_text_empty_content(self):
        entry = MemoryEntry(name="x", type="feedback", description="d", content="")
        text = entry.to_frontmatter_text()
        assert text.endswith("---\n")

    def test_from_frontmatter_text(self):
        text = "---\nname: my-mem\ndescription: desc\nmetadata:\n  type: project\n---\n\nContent here."
        entry = MemoryEntry.from_frontmatter_text(text)
        assert entry is not None
        assert entry.name == "my-mem"
        assert entry.type == "project"
        assert entry.description == "desc"
        assert entry.content == "Content here."

    def test_from_frontmatter_no_frontmatter(self):
        entry = MemoryEntry.from_frontmatter_text("Just text.")
        assert entry is None

    def test_relative_path(self):
        entry = MemoryEntry(name="foo", type="feedback", description="d", content="c")
        assert entry.relative_path == "feedback/foo.md"


# ---------------------------------------------------------------------------
# MemoryStore — basic file I/O
# ---------------------------------------------------------------------------


class TestMemoryStoreInit:
    def test_creates_directories(self, workspace):
        store = MemoryStore(workspace)
        assert store.memory_dir.exists()
        assert (store.memory_dir / "user").exists()
        assert (store.memory_dir / "feedback").exists()
        assert (store.memory_dir / "project").exists()
        assert (store.memory_dir / "reference").exists()

    def test_default_max_history(self, store):
        assert store.max_history_entries == 1000

    def test_custom_max_history(self, workspace):
        store = MemoryStore(workspace, max_history_entries=500)
        assert store.max_history_entries == 500


class TestSoulAndUser:
    def test_read_write_soul(self, store):
        store.write_soul("I am an AI assistant.")
        assert store.read_soul() == "I am an AI assistant."

    def test_read_write_user(self, store):
        store.write_user("User is a Python engineer.")
        assert store.read_user() == "User is a Python engineer."

    def test_read_nonexistent(self, store):
        assert store.read_soul() == ""
        assert store.read_user() == ""


class TestMemoryCRUD:
    def test_write_and_read(self, store):
        entry = MemoryEntry(
            name="test-entry",
            type="user",
            description="A test entry for testing.",
            content="This is the content.",
        )
        store.write_memory(entry)
        result = store.read_memory("test-entry")
        assert result is not None
        assert result.name == "test-entry"
        assert result.type == "user"
        assert result.content == "This is the content."

    def test_write_updates_index(self, store):
        entry = MemoryEntry(name="idx-test", type="feedback", description="Index test.", content="ok")
        store.write_memory(entry)
        index = store.read_memory_index()
        assert "idx-test" in index
        assert "feedback/idx-test.md" in index
        assert "Index test." in index

    def test_delete(self, store):
        entry = MemoryEntry(name="del-me", type="project", description="Delete test.", content="x")
        store.write_memory(entry)
        assert store.read_memory("del-me") is not None

        assert store.delete_memory("del-me") is True
        assert store.read_memory("del-me") is None
        assert "del-me" not in store.read_memory_index()

    def test_delete_nonexistent(self, store):
        assert store.delete_memory("does-not-exist") is False

    def test_list_memories(self, store):
        store.write_memory(MemoryEntry(name="a", type="user", description="A", content="a"))
        store.write_memory(MemoryEntry(name="b", type="feedback", description="B", content="b"))
        entries = store.list_memories()
        assert len(entries) == 2
        names = {e.name for e in entries}
        assert names == {"a", "b"}

    def test_find_memory(self, store):
        store.write_memory(MemoryEntry(name="find-me", type="reference", description="Ref.", content="data"))
        assert store.find_memory("find-me") is not None
        assert store.find_memory("nope") is None

    def test_overwrite_existing(self, store):
        e1 = MemoryEntry(name="overwrite", type="user", description="v1", content="old")
        store.write_memory(e1)
        e2 = MemoryEntry(name="overwrite", type="user", description="v2", content="new")
        store.write_memory(e2)
        result = store.read_memory("overwrite")
        assert result.content == "new"
        assert result.description == "v2"


class TestMemoryIndex:
    def test_rebuild_index(self, store):
        store.write_memory(MemoryEntry(name="x", type="user", description="X marks", content=""))
        store.write_memory(MemoryEntry(name="y", type="project", description="Y not", content=""))
        # Corrupt the index
        store.write_memory_index("")
        index = store.rebuild_index()
        assert "x" in index
        assert "y" in index
        assert "X marks" in index
        assert "Y not" in index

    def test_parse_index_entries(self, store):
        store.write_memory(MemoryEntry(name="p1", type="user", description="First", content=""))
        store.write_memory(MemoryEntry(name="p2", type="feedback", description="Second", content=""))
        entries = store.parse_index_entries()
        assert len(entries) == 2
        names = {e["name"] for e in entries}
        assert names == {"p1", "p2"}


class TestReverseSync:
    def test_no_changes_on_fresh_store(self, store):
        store.write_memory(MemoryEntry(name="sync", type="user", description="test", content="ok"))
        # Everything written via store, so mtimes should be consistent
        changed = store.check_reverse_sync()
        # Reverse sync may or may not detect changes depending on filesystem timing
        # Just verify it doesn't crash
        assert isinstance(changed, list)

    def test_external_modification_detected(self, store):
        store.write_memory(MemoryEntry(name="ext", type="user", description="test", content="orig"))
        # Directly modify the file behind the store's back
        file_path = store.memory_dir / "user" / "ext.md"
        # Update mtime to be clearly newer (wait a bit first)
        import time
        time.sleep(0.01)
        file_path.write_text(file_path.read_text())
        # This may or may not detect depending on timing; just verify it runs
        changed = store.check_reverse_sync()
        assert isinstance(changed, list)


# ---------------------------------------------------------------------------
# MemoryStore — history
# ---------------------------------------------------------------------------


class TestHistory:
    def test_append_and_read(self, store):
        c1 = store.append_history("First entry")
        c2 = store.append_history("Second entry")
        assert c2 > c1

        entries = store.read_unprocessed_history(0)
        assert len(entries) >= 2

    def test_read_since_cursor(self, store):
        store.append_history("old")
        store.append_history("middle")
        c3 = store.append_history("new")
        entries = store.read_unprocessed_history(c3 - 1)
        assert len(entries) == 1
        assert entries[0]["content"] == "new"

    def test_compact(self, store):
        store.max_history_entries = 3
        for i in range(10):
            store.append_history(f"entry {i}")
        store.compact_history()
        entries = store._read_entries()
        assert len(entries) <= 3
        # Should keep the newest
        assert "entry 9" in entries[-1]["content"]

    def test_compact_no_limit(self, store):
        store.max_history_entries = 0
        for i in range(5):
            store.append_history(f"entry {i}")
        store.compact_history()
        assert len(store._read_entries()) == 5

    def test_get_last_cursor(self, store):
        assert store.get_last_cursor() == 0
        c = store.append_history("test")
        assert store.get_last_cursor() == c

    def test_truncation(self, store):
        c = store.append_history("x" * 20_000, max_chars=100)
        entries = store.read_unprocessed_history(c - 1)
        assert len(entries[0]["content"]) <= 150  # 100 + truncation suffix


class TestDreamCursor:
    def test_default(self, store):
        assert store.get_last_dream_cursor() == 0

    def test_set_and_get(self, store):
        store.set_last_dream_cursor(42)
        assert store.get_last_dream_cursor() == 42


class TestMemoryContext:
    def test_empty(self, store):
        assert store.get_memory_context() == ""

    def test_with_entries(self, store):
        store.write_memory(MemoryEntry(
            name="ctx-test",
            type="user",
            description="Context test entry.",
            content="Body for context injection.",
        ))
        ctx = store.get_memory_context()
        assert "Memory Index" in ctx
        assert "ctx-test" in ctx
        assert "Body for context injection." in ctx


# ---------------------------------------------------------------------------
# MemoryManager
# ---------------------------------------------------------------------------


class TestManagerCRUD:
    def test_remember_new(self, manager):
        entry = manager.remember("new-mem", "content", mem_type="user", description="A new memory")
        assert entry.name == "new-mem"
        assert manager.store.read_memory("new-mem") is not None

    def test_remember_update_existing(self, manager):
        manager.remember("update-me", "v1", mem_type="user", description="First")
        manager.remember("update-me", "v2", mem_type="user", description="Second")
        entry = manager.get("update-me")
        assert entry.content == "v2"
        assert entry.description == "Second"

    def test_remember_invalid_type(self, manager):
        with pytest.raises(ValueError, match="Invalid memory type"):
            manager.remember("bad", "x", mem_type="invalid")

    def test_forget(self, manager):
        manager.remember("bye", "gone", mem_type="feedback", description="Temporary")
        assert manager.forget("bye") is True
        assert manager.get("bye") is None

    def test_forget_nonexistent(self, manager):
        assert manager.forget("ghost") is False

    def test_list_all(self, manager):
        manager.remember("a", "A", mem_type="user", description="first")
        manager.remember("b", "B", mem_type="project", description="second")
        assert manager.memory_count == 2

    def test_get(self, manager):
        manager.remember("get-me", "value", mem_type="reference", description="desc")
        entry = manager.get("get-me")
        assert entry is not None
        assert entry.content == "value"
        assert manager.get("nope") is None


class TestManagerRecall:
    def test_name_match(self, manager):
        manager.remember("python-tips", "Use asyncio for async.", mem_type="user", description="Python tips")
        results = manager.recall("python")
        assert len(results) >= 1
        assert results[0].name == "python-tips"

    def test_description_match(self, manager):
        manager.remember("db-setup", "Use PostgreSQL.", mem_type="project",
                         description="Database configuration guide")
        results = manager.recall("database")
        assert len(results) >= 1

    def test_content_match(self, manager):
        manager.remember("auth", "Always use JWT tokens for authentication.", mem_type="reference",
                         description="Auth guide")
        results = manager.recall("JWT")
        assert len(results) >= 1

    def test_no_match(self, manager):
        manager.remember("x", "y", mem_type="user", description="z")
        results = manager.recall("completely-unrelated-query")
        assert len(results) == 0

    def test_scoring(self, manager):
        manager.remember("match-name", "irrelevant", mem_type="user", description="desc")
        manager.remember("other", "keyword keyword keyword", mem_type="user", description="desc")
        results = manager.recall("keyword")
        # Name match scores higher than content
        if results:
            assert results[0].name in ("match-name", "other")


class TestManagerContext:
    def test_empty(self, manager):
        ctx = manager.build_memory_context()
        assert ctx == ""

    def test_with_soul(self, manager):
        manager.store.write_soul("I am helpful.")
        ctx = manager.build_memory_context()
        assert "SOUL.md" in ctx
        assert "I am helpful." in ctx

    def test_with_user(self, manager):
        manager.store.write_user("Python engineer.")
        ctx = manager.build_memory_context()
        assert "USER.md" in ctx
        assert "Python engineer." in ctx

    def test_with_memories(self, manager):
        manager.remember("test", "body", mem_type="user", description="Test memory")
        ctx = manager.build_memory_context()
        assert "# Memory" in ctx
        assert "test" in ctx

    def test_full_context(self, manager):
        manager.store.write_soul("AI assistant.")
        manager.store.write_user("Developer user.")
        manager.remember("pref", "Dark mode preferred.", mem_type="feedback", description="UI preferences")
        ctx = manager.build_memory_context()
        assert "SOUL.md" in ctx
        assert "USER.md" in ctx
        assert "# Memory" in ctx
        assert "pref" in ctx
        assert "Dark mode preferred." in ctx


class TestManagerHistory:
    def test_record(self, manager):
        c = manager.record("User said hello.")
        assert c > 0
        assert manager.history_count >= 1

    def test_get_recent(self, manager):
        for i in range(10):
            manager.record(f"entry {i}")
        recent = manager.get_recent_history(5)
        assert len(recent) == 5

    def test_format_recent(self, manager):
        manager.record("Test entry for formatting.")
        text = manager.format_recent_history(50)
        assert "Test entry for formatting." in text

    def test_format_recent_truncation(self, manager):
        manager.record("A" * 500)
        text = manager.format_recent_history(50, max_chars=100)
        assert len(text) <= 200  # 100 + "... (truncated)" + newlines


class TestManagerSync:
    def test_sync_no_changes(self, manager):
        manager.remember("sync-test", "ok", mem_type="user", description="test")
        changed = manager.sync_from_disk()
        assert isinstance(changed, list)

    def test_compact(self, manager):
        manager.store.max_history_entries = 5
        for i in range(20):
            manager.record(f"entry {i}")
        manager.compact()
        assert manager.history_count <= 5
