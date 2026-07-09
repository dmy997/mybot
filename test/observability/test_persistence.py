"""Tests for observability/persistence.py."""

from __future__ import annotations

import json
import time

from observability.persistence import ObservabilityStore


class TestSaveAndLoad:
    def test_save_and_load_events(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        store.save_event("s1", "LLMCallEvent", {"model": "gpt-4", "tokens_in": 100})
        store.save_event("s1", "ToolCallEvent", {"tool_name": "bash", "success": True})
        store.save_event("s1", "AgentRunEvent", {"steps": 5})

        events = store.load_events("s1")
        assert len(events) == 3
        # newest first
        assert events[0]["event_type"] == "AgentRunEvent"
        assert events[1]["event_type"] == "ToolCallEvent"
        assert events[2]["event_type"] == "LLMCallEvent"

    def test_save_and_load_spans(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        store.save_span("s1", {
            "trace_id": "abc", "span_id": "s1", "parent_span_id": None,
            "name": "agent.run", "latency_ms": 2500.0, "status": "ok",
            "attributes": {"session_key": "s1"},
        })
        store.save_span("s1", {
            "trace_id": "abc", "span_id": "s2", "parent_span_id": "s1",
            "name": "llm.chat", "latency_ms": 1234.0, "status": "ok",
            "attributes": {},
        })

        spans = store.load_spans("s1")
        assert len(spans) == 2
        # newest first
        assert spans[0]["name"] == "llm.chat"
        assert spans[1]["name"] == "agent.run"

    def test_load_returns_newest_first(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        for i in range(5):
            store.save_event("s1", "LLMCallEvent", {
                "model": "gpt-4", "tokens_in": 10 + i,
            })
            time.sleep(0.01)

        events = store.load_events("s1")
        assert events[0]["data"]["tokens_in"] == 14

    def test_load_limit(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        for i in range(20):
            store.save_event("s1", "LLMCallEvent", {"tokens_in": i})

        events = store.load_events("s1", limit=5)
        assert len(events) == 5

    def test_empty_session(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        assert store.load_events("nonexistent") == []
        assert store.load_spans("nonexistent") == []


class TestSessionIsolation:
    def test_different_sessions_independent(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        store.save_event("a", "LLMCallEvent", {"model": "gpt-4"})
        store.save_event("b", "ToolCallEvent", {"tool_name": "bash"})

        a_events = store.load_events("a")
        b_events = store.load_events("b")
        assert len(a_events) == 1
        assert len(b_events) == 1
        assert a_events[0]["event_type"] == "LLMCallEvent"
        assert b_events[0]["event_type"] == "ToolCallEvent"

    def test_special_chars_in_session_key(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        store.save_event("test/../escape", "LLMCallEvent", {"model": "gpt-4"})
        events = store.load_events("test/../escape")
        assert len(events) == 1
        alt = store.load_events("test_.._escape")
        assert len(alt) == 1


class TestListSessions:
    def test_list_sessions_newest_first(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        store.save_event("older", "LLMCallEvent", {"model": "gpt-4"})
        time.sleep(0.02)
        store.save_event("newer", "LLMCallEvent", {"model": "gpt-4"})

        sessions = store.list_sessions()
        assert sessions[0] == "newer"
        assert sessions[1] == "older"

    def test_list_sessions_empty(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        assert store.list_sessions() == []


class TestTrim:
    def test_trim_keeps_most_recent(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        for i in range(100):
            store.save_event("s1", "LLMCallEvent", {"tokens_in": i})

        store.trim("s1", max_events=10)
        events = store.load_events("s1")
        assert len(events) == 10
        assert events[-1]["data"]["tokens_in"] > 80

    def test_trim_missing_session(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        store.trim("nonexistent")


class TestInitStore:
    def test_init_store_sets_singleton(self, tmp_path):
        from observability.persistence import init_store

        obs = init_store(tmp_path)
        # Re-import to see the updated module-level singleton
        import observability.persistence as _mod
        assert _mod.store is obs


class TestCorruptFile:
    def test_corrupt_lines_skipped(self, tmp_path):
        store = ObservabilityStore(tmp_path)
        path = store._path("s1")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "event", "session_key": "s1",
                "event_type": "FirstEvent", "timestamp": 1.0,
                "data": {},
            }) + "\n")
            f.write("this is not valid json\n")
            f.write(json.dumps({
                "type": "event", "session_key": "s1",
                "event_type": "SecondEvent", "timestamp": 2.0,
                "data": {},
            }) + "\n")

        events = store.load_events("s1")
        assert len(events) == 2
        # newest first
        assert events[0]["event_type"] == "SecondEvent"
        assert events[1]["event_type"] == "FirstEvent"
