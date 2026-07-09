"""Tests for Session and SessionManager."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from context.session import Session, SessionManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def mgr(workspace):
    return SessionManager(workspace)


# ---------------------------------------------------------------------------
# Session dataclass
# ---------------------------------------------------------------------------


class TestSession:
    def test_defaults(self):
        s = Session(key="test")
        assert s.key == "test"
        assert s.messages == []
        assert s.consolidated_cursor == 0
        assert s.metadata == {}

    def test_with_messages(self):
        s = Session(key="k", messages=[{"role": "user", "content": "hi"}])
        assert len(s.messages) == 1


# ---------------------------------------------------------------------------
# SessionManager — get / create
# ---------------------------------------------------------------------------


class TestGetSession:
    def test_creates_new_session(self, mgr):
        s = mgr.get_session("new-session")
        assert s.key == "new-session"
        assert s.messages == []

    def test_returns_same_instance_in_memory(self, mgr):
        s1 = mgr.get_session("same")
        s2 = mgr.get_session("same")
        assert s1 is s2

    def test_persists_to_disk(self, mgr, workspace):
        mgr.get_session("disk-test")
        mgr.save_session(mgr.get_session("disk-test"))
        assert (workspace / "sessions" / "disk-test.json").exists()

    def test_loads_from_disk(self, mgr, workspace):
        # Create session via first manager
        mgr.get_session("load-test")
        mgr.add_message_to_session("load-test", {"role": "user", "content": "stored"})

        # Second manager loads from disk
        mgr2 = SessionManager(workspace)
        history = mgr2.get_session_history("load-test")
        assert len(history) == 1
        assert history[0]["content"] == "stored"

    def test_corrupt_file_returns_none(self, mgr, workspace):
        path = workspace / "sessions" / "corrupt.json"
        path.write_text("not valid json {{{", encoding="utf-8")
        # Should not crash; falls back to creating a new session
        s = mgr.get_session("corrupt")
        assert s.key == "corrupt"


# ---------------------------------------------------------------------------
# SessionManager — history
# ---------------------------------------------------------------------------


class TestHistory:
    def test_empty_history(self, mgr):
        assert mgr.get_session_history("nonexistent") == []

    def test_add_single_message(self, mgr):
        mgr.add_message_to_session("h1", {"role": "user", "content": "hello"})
        history = mgr.get_session_history("h1")
        assert len(history) == 1
        assert history[0]["content"] == "hello"

    def test_add_multiple_messages(self, mgr):
        mgr.add_message_to_session("h2", {"role": "user", "content": "q1"})
        mgr.add_message_to_session("h2", {"role": "assistant", "content": "a1"})
        mgr.add_message_to_session("h2", {"role": "user", "content": "q2"})
        history = mgr.get_session_history("h2")
        assert len(history) == 3
        assert history[2]["content"] == "q2"

    def test_add_messages_bulk(self, mgr):
        mgr.add_messages_to_session("bulk", [
            {"role": "user", "content": "m1"},
            {"role": "assistant", "content": "m2"},
        ])
        assert len(mgr.get_session_history("bulk")) == 2

    def test_set_messages_replaces(self, mgr):
        mgr.add_message_to_session("replace", {"role": "user", "content": "old"})
        mgr.set_messages("replace", [{"role": "user", "content": "new"}])
        history = mgr.get_session_history("replace")
        assert len(history) == 1
        assert history[0]["content"] == "new"


# ---------------------------------------------------------------------------
# SessionManager — consolidated cursor
# ---------------------------------------------------------------------------


class TestConsolidatedCursor:
    def test_default_zero(self, mgr):
        s = mgr.get_session("cursor-test")
        assert s.consolidated_cursor == 0

    def test_set_and_persist(self, mgr, workspace):
        mgr.get_session("cursor-test")
        mgr.set_consolidated_cursor("cursor-test", 5)

        mgr2 = SessionManager(workspace)
        s = mgr2.get_session("cursor-test")
        assert s.consolidated_cursor == 5


# ---------------------------------------------------------------------------
# SessionManager — lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_remove_from_memory(self, mgr, workspace):
        s = mgr.get_session("rm-mem")
        mgr.save_session(s)
        mgr.remove_session("rm-mem")
        assert "rm-mem" not in mgr.sessions
        # File still exists
        assert (workspace / "sessions" / "rm-mem.json").exists()

    def test_delete_from_disk(self, mgr, workspace):
        s = mgr.get_session("del-disk")
        mgr.save_session(s)
        assert mgr.delete_session("del-disk") is True
        assert not (workspace / "sessions" / "del-disk.json").exists()

    def test_delete_nonexistent(self, mgr):
        assert mgr.delete_session("ghost") is False

    def test_list_sessions(self, mgr):
        mgr.save_session(mgr.get_session("a"))
        mgr.get_session("b")
        mgr.add_message_to_session("b", {"role": "user", "content": "msg"})
        sessions = mgr.list_sessions()
        keys = {s["key"] for s in sessions}
        assert "a" in keys
        assert "b" in keys
        # b has 1 message
        b_info = [s for s in sessions if s["key"] == "b"][0]
        assert b_info["message_count"] == 1

    def test_list_sessions_empty(self, mgr):
        assert mgr.list_sessions() == []

    def test_list_handles_corrupt_file(self, mgr, workspace):
        (workspace / "sessions" / "bad.json").write_text("garbage", encoding="utf-8")
        sessions = mgr.list_sessions()
        assert any(s["key"] == "bad" for s in sessions)

    def test_list_skips_non_session_json(self, mgr, workspace):
        # A runner checkpoint shares the sessions dir but has no top-level "key".
        (workspace / "sessions" / "xiaohongshu_checkpoint.json").write_text(
            json.dumps({
                "session_key": "xiaohongshu",
                "messages": [],
                "step_count": 1,
                "updated_at": "2026-07-07T20:00:00",
            }),
            encoding="utf-8",
        )
        mgr.save_session(mgr.get_session("real"))
        keys = {s["key"] for s in mgr.list_sessions()}
        assert "real" in keys
        assert "xiaohongshu_checkpoint" not in keys


# ---------------------------------------------------------------------------
# SessionManager — update timestamp
# ---------------------------------------------------------------------------


class TestTimestamps:
    def test_add_message_updates_timestamp(self, mgr):
        s = mgr.get_session("ts-test")
        original = s.updated_at
        mgr.add_message_to_session("ts-test", {"role": "user", "content": "msg"})
        assert s.updated_at > original


# ---------------------------------------------------------------------------
# SessionManager — prune_by_count (hard cap)
# ---------------------------------------------------------------------------


class TestPruneByCount:
    def test_below_limit_no_op(self, mgr):
        """Messages under the limit are left untouched."""
        mgr.add_messages_to_session("s1", [{"role": "user", "content": f"msg{i}"} for i in range(5)])
        removed = mgr.prune_by_count("s1", max_messages=100)
        assert removed == 0
        assert len(mgr.get_session("s1").messages) == 5

    def test_at_limit_no_op(self, mgr):
        """Exactly at the limit means nothing to prune."""
        mgr.set_messages("s1", [{"role": "user", "content": f"msg{i}"} for i in range(10)])
        removed = mgr.prune_by_count("s1", max_messages=10)
        assert removed == 0
        assert len(mgr.get_session("s1").messages) == 10

    def test_excess_prunes_oldest(self, mgr):
        """When count exceeds limit, oldest messages are removed."""
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(100)]
        mgr.set_messages("s1", msgs)
        removed = mgr.prune_by_count("s1", max_messages=30)
        assert removed == 70
        remaining = mgr.get_session("s1").messages
        assert len(remaining) == 30
        assert remaining[0]["content"] == "msg70"
        assert remaining[-1]["content"] == "msg99"

    def test_cursor_adjusted(self, mgr):
        """Consolidated cursor and last_consolidated shift with the prune."""
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(50)]
        mgr.set_messages("s1", msgs)
        mgr.set_consolidated_cursor("s1", 10)
        s = mgr.get_session("s1")
        s.last_consolidated = 20
        mgr.save_session(s)

        mgr.prune_by_count("s1", max_messages=25)
        s2 = mgr.get_session("s1")
        assert s2.consolidated_cursor == max(0, 10 - 25)
        assert s2.last_consolidated == max(0, 20 - 25)

    def test_nonexistent_session(self, mgr):
        """Pruning a nonexistent session returns 0."""
        assert mgr.prune_by_count("nonexistent", max_messages=10) == 0


# ---------------------------------------------------------------------------
# SessionManager — purge_expired_sessions
# ---------------------------------------------------------------------------


class TestSessionExpiry:
    def test_empty_dir_returns_zero(self, mgr):
        """An empty sessions directory has nothing to purge."""
        assert mgr.purge_expired_sessions(ttl_days=30) == 0

    def test_new_session_not_deleted(self, mgr):
        """A session that was just saved is within TTL and should survive."""
        mgr.save_session(mgr.get_session("fresh"))
        assert mgr.purge_expired_sessions(ttl_days=30) == 0
        assert mgr.get_session("fresh").key == "fresh"

    def test_old_session_is_deleted(self, mgr, workspace):
        """A session with an old mtime is purged from disk and memory."""
        s = mgr.get_session("stale")
        mgr.save_session(s)
        # Artificially age the file mtime to 60 days ago
        path = workspace / "sessions" / "stale.json"
        old = __import__("time").time() - (60 * 86400)
        path.touch()
        os.utime(str(path), (old, old))
        # Must evict from memory cache so purge sees the file
        mgr.remove_session("stale")

        deleted = mgr.purge_expired_sessions(ttl_days=30)
        assert deleted == 1
        assert not path.exists()

    def test_mixed_old_and_new(self, mgr, workspace):
        """Only old sessions are deleted; recent ones survive."""
        mgr.save_session(mgr.get_session("new-one"))
        mgr.save_session(mgr.get_session("old-one"))

        path = workspace / "sessions" / "old-one.json"
        old = __import__("time").time() - (60 * 86400)
        path.touch()
        os.utime(str(path), (old, old))
        mgr.remove_session("old-one")

        deleted = mgr.purge_expired_sessions(ttl_days=30)
        assert deleted == 1
        assert not path.exists()
        assert (workspace / "sessions" / "new-one.json").exists()

    def test_ttl_zero_disables_expiry(self, mgr, workspace):
        """ttl_days=0 means never expire."""
        mgr.save_session(mgr.get_session("keep"))
        path = workspace / "sessions" / "keep.json"
        old = __import__("time").time() - (60 * 86400)
        path.touch()
        os.utime(str(path), (old, old))
        mgr.remove_session("keep")

        deleted = mgr.purge_expired_sessions(ttl_days=0)
        assert deleted == 0
        assert path.exists()
