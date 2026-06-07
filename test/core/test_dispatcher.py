"""Tests for Dispatcher, heuristic_classifier, and LLMClassifier."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.plan_solve_agent import PlanSolveAgent
from agents.react_agent import ReActAgent
from core.dispatcher import (
    Dispatcher,
    LLMClassifier,
    _DEFAULT_PARADIGM_DESCRIPTIONS,
    _match_explicit_command,
    _match_plan_indicators,
    heuristic_classifier,
)
from core.runner import AgentCore, AgentInput, AgentOutput
from providers.base import LLMProvider, LLMResponse

# ---------------------------------------------------------------------------
# _match_explicit_command
# ---------------------------------------------------------------------------


class TestMatchExplicitCommand:
    def test_matches_react(self):
        assert _match_explicit_command("/react explain this") == "react"
        assert _match_explicit_command("/react") == "react"

    def test_matches_plan(self):
        assert _match_explicit_command("/plan design a system") == "plan_solve"
        assert _match_explicit_command("/plan") == "plan_solve"

    def test_case_insensitive(self):
        assert _match_explicit_command("/PLAN something") == "plan_solve"
        assert _match_explicit_command("/React now") == "react"

    def test_no_match(self):
        assert _match_explicit_command("hello world") is None
        assert _match_explicit_command("plan this for me") is None  # no / prefix
        assert _match_explicit_command("") is None

    def test_must_be_at_start(self):
        """Command prefix must be at the beginning of the string."""
        assert _match_explicit_command("please /react to this") is None


# ---------------------------------------------------------------------------
# _match_plan_indicators
# ---------------------------------------------------------------------------


class TestMatchPlanIndicators:
    def test_chinese_multi_step(self):
        assert _match_plan_indicators("第一步分析，然后设计") == "plan_solve"
        assert _match_plan_indicators("首先调查，之后修复") == "plan_solve"

    def test_english_multi_step(self):
        assert _match_plan_indicators("first gather data, then analyze") == "plan_solve"
        assert _match_plan_indicators("step 1 open, step 2 parse") == "plan_solve"

    def test_plan_keywords(self):
        assert _match_plan_indicators("let me plan this") == "plan_solve"
        assert _match_plan_indicators("break down the problem") == "plan_solve"
        assert _match_plan_indicators("outline the workflow") == "plan_solve"

    def test_no_match(self):
        assert _match_plan_indicators("hello") is None
        assert _match_plan_indicators("what is 2+2?") is None


# ---------------------------------------------------------------------------
# heuristic_classifier
# ---------------------------------------------------------------------------


class TestHeuristicClassifier:
    @pytest.mark.asyncio
    async def test_default_is_react(self):
        assert await heuristic_classifier("hello, how are you?") == "react"
        assert await heuristic_classifier("what is 2+2?") == "react"
        assert await heuristic_classifier("") == "react"

    @pytest.mark.asyncio
    async def test_explicit_react(self):
        assert await heuristic_classifier("/react explain this code") == "react"

    @pytest.mark.asyncio
    async def test_explicit_plan(self):
        assert await heuristic_classifier("/plan design a system") == "plan_solve"

    @pytest.mark.asyncio
    async def test_explicit_takes_priority(self):
        result = await heuristic_classifier("/react first do this then do that")
        assert result == "react"

    @pytest.mark.asyncio
    async def test_keyword_match(self):
        assert await heuristic_classifier("can you plan a trip for me?") == "plan_solve"


# ---------------------------------------------------------------------------
# Dispatcher — construction
# ---------------------------------------------------------------------------


class TestDispatcherInit:
    def test_requires_at_least_one_agent(self):
        with pytest.raises(ValueError, match="at least one agent"):
            Dispatcher({})

    def test_first_agent_is_default(self, react_agent):
        d = Dispatcher({"react": react_agent})
        assert d._default == "react"

    def test_no_provider_means_no_llm(self, react_agent):
        d = Dispatcher({"react": react_agent})
        assert d._llm is None

    def test_provider_creates_internal_llm_classifier(self, react_agent):
        d = Dispatcher({"react": react_agent}, provider=MagicMock())
        assert d._llm is not None
        assert isinstance(d._llm, LLMClassifier)

    def test_provider_classifier_uses_default_descriptions(self, react_agent):
        d = Dispatcher({"react": react_agent}, provider=MagicMock())
        assert d._llm.paradigms == {"react": _DEFAULT_PARADIGM_DESCRIPTIONS["react"]}

    def test_unknown_paradigm_uses_title_case(self, react_agent):
        d = Dispatcher({"custom_agent": react_agent}, provider=MagicMock())
        assert "custom_agent" in d._llm.paradigms
        assert d._llm.paradigms["custom_agent"] == "Custom Agent"


# ---------------------------------------------------------------------------
# Dispatcher — resolve (built-in regex layers)
# ---------------------------------------------------------------------------


class TestDispatcherResolveBuiltin:
    @pytest.mark.asyncio
    async def test_explicit_command_routes(self, dispatcher):
        assert await dispatcher.resolve("/react do something") == "react"
        assert await dispatcher.resolve("/plan design") == "plan_solve"

    @pytest.mark.asyncio
    async def test_keyword_routes_to_plan_solve(self, dispatcher):
        assert await dispatcher.resolve("first do A then do B") == "plan_solve"

    @pytest.mark.asyncio
    async def test_default_is_first_agent(self, dispatcher):
        """No regex match → default (first registered agent)."""
        # First registered is "react" (see dispatcher fixture)
        assert await dispatcher.resolve("hello world") == "react"

    @pytest.mark.asyncio
    async def test_unknown_paradigm_falls_back(self, react_agent):
        """Regex matches a paradigm not in agents → fallback to default."""
        # _match_explicit_command returns "plan_solve" but it's not registered
        d = Dispatcher({"react": react_agent})
        assert await d.resolve("/plan something") == "react"


# ---------------------------------------------------------------------------
# Dispatcher — resolve (LLM fallback)
# ---------------------------------------------------------------------------


class TestDispatcherResolveLLMFallback:
    @pytest.mark.asyncio
    async def test_llm_only_called_for_fuzzy_intents(self, react_agent, plan_solve_agent):
        """LLM is NOT called when regex already matches."""
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(return_value=LLMResponse(content="plan_solve"))

        d = Dispatcher(
            {"react": react_agent, "plan_solve": plan_solve_agent},
            provider=provider,
        )

        # Explicit command → regex matches, LLM never called
        result = await d.resolve("/react explain")
        assert result == "react"
        provider.chat.assert_not_called()

        # Keyword match → regex matches, LLM never called
        result = await d.resolve("first do this then that")
        assert result == "plan_solve"
        provider.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_called_when_no_regex_match(self, react_agent, plan_solve_agent):
        """Fuzzy intent → regex misses → LLM is called."""
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(return_value=LLMResponse(content="plan_solve"))

        d = Dispatcher(
            {"react": react_agent, "plan_solve": plan_solve_agent},
            provider=provider,
        )

        result = await d.resolve("design a scalable architecture for my app")
        assert result == "plan_solve"
        provider.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_error_falls_back_to_default(self, react_agent, plan_solve_agent):
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(side_effect=RuntimeError("API down"))

        d = Dispatcher(
            {"react": react_agent, "plan_solve": plan_solve_agent},
            provider=provider,
        )

        result = await d.resolve("some ambiguous question")
        assert result == "react"


# ---------------------------------------------------------------------------
# Dispatcher — dispatch
# ---------------------------------------------------------------------------


class TestDispatcherDispatch:
    @pytest.mark.asyncio
    async def test_dispatches_to_correct_agent(self):
        react = MagicMock(spec=ReActAgent)
        react.run = AsyncMock(return_value=AgentOutput(content="react done"))
        plansolve = MagicMock(spec=PlanSolveAgent)
        plansolve.run = AsyncMock(return_value=AgentOutput(content="plan done"))

        d = Dispatcher({"react": react, "plan_solve": plansolve})
        spec = AgentInput(init_messages=[{"role": "user", "content": "hi"}])

        react_result = await d.dispatch("hello", spec)
        assert react_result.content == "react done"

        plan_result = await d.dispatch("/plan something", spec)
        assert plan_result.content == "plan done"

    @pytest.mark.asyncio
    async def test_dispatch_passes_spec_through(self):
        react = MagicMock(spec=ReActAgent)
        react.run = AsyncMock(return_value=AgentOutput(content="ok"))

        d = Dispatcher({"react": react})
        spec = AgentInput(
            init_messages=[{"role": "user", "content": "q"}],
            goal="test goal",
            model="gpt-5",
        )
        await d.dispatch("hi", spec)
        react.run.assert_awaited_once_with(spec)


# ---------------------------------------------------------------------------
# LLMClassifier
# ---------------------------------------------------------------------------


_PARADIGMS = {
    "react": "Simple Q&A, conversation, single-step tasks",
    "plan_solve": "Complex multi-step tasks requiring planning",
}


class TestLLMClassifierInit:
    def test_requires_at_least_one_paradigm(self):
        with pytest.raises(ValueError, match="at least one paradigm"):
            LLMClassifier(MagicMock(), {})

    def test_stores_paradigms_and_fallback(self):
        c = LLMClassifier(MagicMock(), _PARADIGMS, fallback="plan_solve")
        assert c.paradigms == _PARADIGMS
        assert c.fallback == "plan_solve"
        assert c.model is None


class TestLLMClassifierCall:
    @pytest.mark.asyncio
    async def test_returns_react_for_simple_query(self):
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(return_value=LLMResponse(content="react"))
        c = LLMClassifier(provider, _PARADIGMS)

        result = await c("what is the weather?")
        assert result == "react"

    @pytest.mark.asyncio
    async def test_returns_plan_solve_for_complex_query(self):
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(return_value=LLMResponse(content="plan_solve"))
        c = LLMClassifier(provider, _PARADIGMS)

        result = await c("design a microservice architecture for my app")
        assert result == "plan_solve"

    @pytest.mark.asyncio
    async def test_trims_whitespace_from_response(self):
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(return_value=LLMResponse(content="  react\n"))
        c = LLMClassifier(provider, _PARADIGMS)

        assert await c("hi") == "react"

    @pytest.mark.asyncio
    async def test_unrecognised_paradigm_falls_back(self):
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(return_value=LLMResponse(content="unknown_paradigm"))
        c = LLMClassifier(provider, _PARADIGMS, fallback="react")

        assert await c("some query") == "react"

    @pytest.mark.asyncio
    async def test_empty_content_falls_back(self):
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(return_value=LLMResponse(content=""))
        c = LLMClassifier(provider, _PARADIGMS, fallback="plan_solve")

        assert await c("query") == "plan_solve"

    @pytest.mark.asyncio
    async def test_llm_error_falls_back(self):
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(side_effect=RuntimeError("API down"))
        c = LLMClassifier(provider, _PARADIGMS, fallback="react")

        assert await c("query") == "react"

    @pytest.mark.asyncio
    async def test_passes_model_parameter(self):
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(return_value=LLMResponse(content="react"))
        c = LLMClassifier(provider, _PARADIGMS, model="gpt-4o-mini")

        await c("hi")
        call_kwargs = provider.chat.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o-mini"
        assert call_kwargs["temperature"] == 0.0
        assert call_kwargs["max_tokens"] == 10
        assert call_kwargs["tools"] == []

    @pytest.mark.asyncio
    async def test_system_prompt_contains_all_paradigms(self):
        provider = MagicMock(spec=LLMProvider)
        provider.chat = AsyncMock(return_value=LLMResponse(content="react"))
        c = LLMClassifier(provider, _PARADIGMS)

        await c("hi")
        system_msg = provider.chat.call_args.kwargs["messages"][0]["content"]
        assert "react" in system_msg
        assert "plan_solve" in system_msg
        assert "Simple Q&A" in system_msg
        assert "Complex multi-step" in system_msg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def react_agent():
    return ReActAgent(MagicMock(spec=AgentCore))


@pytest.fixture
def plan_solve_agent():
    return PlanSolveAgent(MagicMock(spec=AgentCore))


@pytest.fixture
def dispatcher(react_agent, plan_solve_agent):
    return Dispatcher({"react": react_agent, "plan_solve": plan_solve_agent})
