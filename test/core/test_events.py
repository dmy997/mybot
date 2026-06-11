"""Tests for core.events EventBus."""

from __future__ import annotations

import asyncio

import pytest

from core.events import (
    AgentCompleted,
    AgentStarted,
    Event,
    EventBus,
    LLMResponseReady,
    ToolExecutionCompleted,
    ToolExecutionStarted,
    bus,
)


class TestEventBus:
    def test_subscribe_and_publish(self):
        b = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        b.subscribe(AgentStarted, handler)
        ev = AgentStarted(session_key="s1", paradigm="react")
        asyncio.run(b.publish(ev))
        assert len(received) == 1
        assert received[0] is ev

    def test_multiple_subscribers(self):
        b = EventBus()
        count = 0

        async def inc(_event: Event) -> None:
            nonlocal count
            count += 1

        b.subscribe(ToolExecutionCompleted, inc)
        b.subscribe(ToolExecutionCompleted, inc)
        b.subscribe(ToolExecutionCompleted, inc)
        asyncio.run(b.publish(ToolExecutionCompleted(session_key="s1", tool_name="bash")))
        assert count == 3

    def test_unsubscribe(self):
        b = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        b.subscribe(AgentStarted, handler)
        b.unsubscribe(AgentStarted, handler)
        asyncio.run(b.publish(AgentStarted(session_key="s1")))
        assert len(received) == 0

    def test_unsubscribe_nonexistent(self):
        b = EventBus()

        async def handler(_e: Event) -> None:
            pass

        b.unsubscribe(AgentStarted, handler)  # should not raise

    def test_inheritance_matching(self):
        """Subscribers to a base class receive subclass events."""
        b = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        b.subscribe(Event, handler)
        asyncio.run(b.publish(ToolExecutionStarted(session_key="s1", tool_name="bash")))
        assert len(received) == 1
        assert isinstance(received[0], ToolExecutionStarted)

    def test_no_matching_subscriber(self):
        b = EventBus()
        received: list[Event] = []

        async def handler(event: AgentStarted) -> None:
            received.append(event)

        b.subscribe(AgentStarted, handler)
        asyncio.run(b.publish(AgentCompleted(session_key="s1")))
        assert len(received) == 0

    def test_subscriber_exception_isolated(self):
        b = EventBus()
        good_count = 0

        async def bad(_event: Event) -> None:
            raise RuntimeError("boom")

        async def good(_event: Event) -> None:
            nonlocal good_count
            good_count += 1

        b.subscribe(AgentStarted, bad)
        b.subscribe(AgentStarted, good)
        asyncio.run(b.publish(AgentStarted(session_key="s1")))
        assert good_count == 1  # good handler still ran despite bad handler

    def test_clear(self):
        b = EventBus()
        count = 0

        async def inc(_e: Event) -> None:
            nonlocal count
            count += 1

        b.subscribe(AgentStarted, inc)
        b.clear()
        asyncio.run(b.publish(AgentStarted()))
        assert count == 0

    def test_subscriber_count(self):
        b = EventBus()

        async def h(_e: Event) -> None:
            pass

        assert b.subscriber_count == 0
        b.subscribe(AgentStarted, h)
        b.subscribe(AgentCompleted, h)
        assert b.subscriber_count == 2

    def test_global_bus_singleton(self):
        """Module-level bus singleton works."""
        assert isinstance(bus, EventBus)
        assert bus.subscriber_count >= 0  # may have built-in subscribers

    def test_publish_no_subscribers(self):
        b = EventBus()
        # Should not raise with no subscribers
        asyncio.run(b.publish(AgentStarted()))


class TestEventTypes:
    def test_agent_started_defaults(self):
        ev = AgentStarted()
        assert ev.session_key == ""
        assert ev.paradigm == ""

    def test_agent_started_fields(self):
        ev = AgentStarted(session_key="abc", paradigm="react", messages_count=5, tools_count=3)
        assert ev.session_key == "abc"
        assert ev.paradigm == "react"
        assert ev.messages_count == 5
        assert ev.tools_count == 3

    def test_tool_execution_started(self):
        ev = ToolExecutionStarted(
            session_key="s1", tool_name="bash",
            arguments={"cmd": "ls"}, index=2, total=5,
        )
        assert ev.tool_name == "bash"
        assert ev.arguments == {"cmd": "ls"}
        assert ev.index == 2
        assert ev.total == 5

    def test_tool_execution_completed(self):
        ev = ToolExecutionCompleted(
            session_key="s1", tool_name="grep",
            success=True, latency_ms=123.4, error=None,
        )
        assert ev.tool_name == "grep"
        assert ev.success is True
        assert ev.latency_ms == 123.4

    def test_llm_response_ready(self):
        ev = LLMResponseReady(
            session_key="s1",
            model="gpt-4",
            latency_ms=500.0,
            messages_count=10,
            tools_count=3,
            tokens_in=1000,
            tokens_out=200,
            tokens_total=1200,
            finish_reason="stop",
        )
        assert ev.model == "gpt-4"
        assert ev.tokens_total == 1200
        assert ev.finish_reason == "stop"

    def test_agent_completed(self):
        ev = AgentCompleted(
            session_key="s1",
            paradigm="react",
            steps=5,
            total_latency_ms=3000.0,
            stop_reason="stop",
        )
        assert ev.paradigm == "react"
        assert ev.steps == 5
        assert ev.stop_reason == "stop"

    def test_event_has_timestamp(self):
        ev = AgentStarted()
        assert ev.timestamp > 0
        import time
        assert abs(ev.timestamp - time.monotonic()) < 1.0
