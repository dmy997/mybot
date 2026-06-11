"""Tests for core.message_bus."""

from __future__ import annotations

import asyncio

import pytest

from core.message_bus import InboundMessage, MessageBus, OutboundMessage


class TestInboundMessage:
    def test_defaults(self):
        msg = InboundMessage(session_key="s1", content="hello")
        assert msg.session_key == "s1"
        assert msg.content == "hello"
        assert msg.source == ""
        assert msg.correlation_id == ""
        assert msg.model is None

    def test_full_fields(self):
        msg = InboundMessage(
            session_key="s1", content="hello", source="cli",
            correlation_id="abc", model="gpt-4", goal="test",
            skills=["s1"], timestamp=1234.5,
        )
        assert msg.source == "cli"
        assert msg.correlation_id == "abc"
        assert msg.model == "gpt-4"
        assert msg.goal == "test"
        assert msg.skills == ["s1"]
        assert msg.timestamp == 1234.5


class TestOutboundMessage:
    def test_fields(self):
        msg = OutboundMessage(
            session_key="s1", correlation_id="abc",
            msg_type="delta", data="hello world",
        )
        assert msg.session_key == "s1"
        assert msg.correlation_id == "abc"
        assert msg.msg_type == "delta"
        assert msg.data == "hello world"
        assert msg.timestamp > 0

    def test_tool_end_payload(self):
        ev = {"name": "bash", "status": "ok", "duration_ms": 123.4, "detail": "done"}
        msg = OutboundMessage("s1", "abc", "tool_end", ev)
        assert msg.data["name"] == "bash"
        assert msg.data["status"] == "ok"

    def test_final_payload(self):
        data = {"content": "hi", "usage": {"total_tokens": 100}}
        msg = OutboundMessage("s1", "abc", "final", data)
        assert msg.data["content"] == "hi"


class TestMessageBus:
    def test_inbound_creates_queue_on_demand(self):
        b = MessageBus()
        q = b.inbound("default")
        assert q is not None
        assert isinstance(q, asyncio.Queue)

    def test_inbound_returns_same_queue(self):
        b = MessageBus()
        q1 = b.inbound("default")
        q2 = b.inbound("default")
        assert q1 is q2

    def test_inbound_different_sessions(self):
        b = MessageBus()
        q1 = b.inbound("s1")
        q2 = b.inbound("s2")
        assert q1 is not q2

    def test_sessions_property(self):
        b = MessageBus()
        assert b.sessions == []
        b.inbound("s1")
        b.inbound("s2")
        assert sorted(b.sessions) == ["s1", "s2"]

    def test_remove_session(self):
        b = MessageBus()
        b.inbound("s1")
        assert "s1" in b.sessions
        b.remove_session("s1")
        assert "s1" not in b.sessions

    def test_outbound_shared(self):
        b = MessageBus()
        assert b.outbound is b.outbound  # same object

    @pytest.mark.asyncio
    async def test_put_get_inbound(self):
        b = MessageBus()
        msg = InboundMessage(session_key="s1", content="hello")
        await b.inbound("s1").put(msg)
        result = await b.inbound("s1").get()
        assert result is msg

    @pytest.mark.asyncio
    async def test_put_get_outbound(self):
        b = MessageBus()
        msg = OutboundMessage("s1", "abc", "delta", "hi")
        await b.outbound.put(msg)
        result = await b.outbound.get()
        assert result is msg

    @pytest.mark.asyncio
    async def test_backpressure_inbound(self):
        b = MessageBus(inbound_maxsize=2)
        await b.inbound("s1").put(InboundMessage("s1", "1"))
        await b.inbound("s1").put(InboundMessage("s1", "2"))
        assert b.inbound("s1").full()

    @pytest.mark.asyncio
    async def test_backpressure_outbound(self):
        b = MessageBus(outbound_maxsize=2)
        await b.outbound.put(OutboundMessage("s1", "", "delta", "1"))
        await b.outbound.put(OutboundMessage("s1", "", "delta", "2"))
        assert b.outbound.full()

    @pytest.mark.asyncio
    async def test_close_puts_sentinels(self):
        b = MessageBus()
        b.inbound("s1")
        b.inbound("s2")

        close_task = asyncio.create_task(b.close())
        # Each inbound queue gets None
        assert await b.inbound("s1").get() is None
        assert await b.inbound("s2").get() is None
        await close_task

    @pytest.mark.asyncio
    async def test_correlation_id_routing(self):
        """Outbound messages can be filtered by correlation_id."""
        b = MessageBus()
        await b.outbound.put(OutboundMessage("s1", "req1", "delta", "a"))
        await b.outbound.put(OutboundMessage("s1", "req2", "delta", "b"))
        await b.outbound.put(OutboundMessage("s1", "req1", "final", {"c": 1}))

        req1_msgs: list[OutboundMessage] = []
        req2_msgs: list[OutboundMessage] = []

        # Consume all and filter
        for _ in range(3):
            msg = await b.outbound.get()
            if msg.correlation_id == "req1":
                req1_msgs.append(msg)
            elif msg.correlation_id == "req2":
                req2_msgs.append(msg)

        assert len(req1_msgs) == 2
        assert len(req2_msgs) == 1
        assert req1_msgs[0].data == "a"
        assert req1_msgs[1].data == {"c": 1}
