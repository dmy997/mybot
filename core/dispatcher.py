"""Dispatcher — routes user input to the appropriate agent paradigm.

Built-in layered routing (runs in priority order):
1. Explicit command prefixes (``/react``, ``/plan``) — zero-cost string match
2. Keyword heuristics (multi-step indicators → plan_solve) — zero-cost regex
3. LLM classification — lightweight model call (only when provider is given)
4. Default fallback — first registered agent

Typical usage::

    # Zero-cost regex-only (no LLM dependency)
    dispatcher = Dispatcher(agents={"react": react, "plan_solve": plansolve})

    # With LLM classification for fuzzy intents (classifier created internally)
    dispatcher = Dispatcher(
        agents={"react": react, "plan_solve": plansolve},
        provider=provider,
    )
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from utils import render_template

from .agent_base import BaseAgent
from .runner import AgentInput, AgentOutput

# ---------------------------------------------------------------------------
# Regex layers (zero-cost, always on)
# ---------------------------------------------------------------------------

_EXPLICIT_ROUTES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^/react\b", re.IGNORECASE), "react"),
    (re.compile(r"^/plan\b", re.IGNORECASE), "plan_solve"),
    (re.compile(r"^/research\b", re.IGNORECASE), "deep_research"),
]

_PLAN_INDICATORS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        # Chinese multi-step patterns
        r"\b(?:步骤|第一步|首先).{0,15}(?:然后|第二步|之后|最后)",
        r"\b(?:先|其次|接下来|最后)\b",
        # English multi-step patterns
        r"\b(?:first|step\s*\d).{0,20}(?:then|next|finally|step\s*\d)",
        r"\b(?:plan|step.by.step|multi.ste?p)\b",
        r"\b(?:outline|break\s*down|workflow)\b",
    ]
]


def _match_explicit_command(text: str) -> str | None:
    """Return the paradigm name if *text* starts with an explicit command."""
    for pattern, paradigm in _EXPLICIT_ROUTES:
        if pattern.search(text):
            return paradigm
    return None


def _match_plan_indicators(text: str) -> str | None:
    """Return ``"plan_solve"`` if *text* contains multi-step indicators."""
    for pattern in _PLAN_INDICATORS:
        if pattern.search(text):
            return "plan_solve"
    return None


# ---------------------------------------------------------------------------
# Heuristic classifier (convenience, backwards-compatible)
# ---------------------------------------------------------------------------


async def heuristic_classifier(user_input: str) -> str:
    """Classify using regex layers only (no LLM).

    Priority order:
    1. Explicit command (``/react``, ``/plan``)
    2. Multi-step / planning keyword patterns → ``"plan_solve"``
    3. Default → ``"react"``
    """
    text = user_input.strip()
    return (
        _match_explicit_command(text)
        or _match_plan_indicators(text)
        or "react"
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Default paradigm descriptions (used when provider enables LLM classification)
# ---------------------------------------------------------------------------

_DEFAULT_PARADIGM_DESCRIPTIONS: dict[str, str] = {
    "react": render_template("dispatcher/react_paradigm.md", strip=True),
    "plan_solve": render_template("dispatcher/plan_solve_paradigm.md", strip=True),
    "deep_research": "多智能体深度研究——协调多个研究员并行搜索和分析，综合成完整报告",
}


class Dispatcher:
    """Routes user input to the appropriate paradigm agent.

    The built-in routing is layered: regex commands first, then keyword
    heuristics, then an optional LLM fallback, and finally the default
    agent.

    Parameters
    ----------
    agents:
        Paradigm name → agent instance.  The **first** registered agent
        is used as the default fallback.
    provider:
        Optional LLM provider.  When provided, an :class:`LLMClassifier`
        is created internally and used as the final routing layer for
        fuzzy intents that aren't caught by regex layers.
    classify_model:
        Optional cheap model override for the internal LLM classifier.
    """

    def __init__(
        self,
        agents: dict[str, BaseAgent],
        *,
        provider: Any = None,
        classify_model: str | None = None,
    ) -> None:
        if not agents:
            raise ValueError("Dispatcher requires at least one agent")
        self.agents = agents
        self._default: str = next(iter(agents.keys()))

        # Internally instantiate LLMClassifier when provider is available
        if provider is not None:
            paradigms: dict[str, str] = {}
            for name in agents:
                paradigms[name] = _DEFAULT_PARADIGM_DESCRIPTIONS.get(
                    name, name.replace("_", " ").title()
                )
            self._llm: LLMClassifier | None = LLMClassifier(
                provider, paradigms, model=classify_model
            )
        else:
            self._llm = None

    # -- resolve -----------------------------------------------------------

    async def resolve(self, user_input: str) -> str:
        """Return the paradigm name for *user_input*."""
        text = user_input.strip()

        # --- Layer 1: explicit commands (zero-cost) ---
        paradigm = _match_explicit_command(text)
        if paradigm is not None:
            return self._validate(paradigm)

        # --- Layer 2: keyword heuristics (zero-cost) ---
        paradigm = _match_plan_indicators(text)
        if paradigm is not None:
            return self._validate(paradigm)

        # --- Layer 3: LLM classification (optional) ---
        if self._llm is not None:
            paradigm = await self._llm(user_input)
            return self._validate(paradigm)

        # --- Layer 4: default ---
        return self._default

    # -- dispatch ----------------------------------------------------------

    async def dispatch(
        self, user_input: str, spec: AgentInput
    ) -> AgentOutput:
        """Resolve paradigm and execute in one call.

        Convenience wrapper around :meth:`resolve` + ``agent.run()``.
        """
        paradigm = await self.resolve(user_input)
        return await self.agents[paradigm].run(spec)

    # -- internal ----------------------------------------------------------

    def _validate(self, paradigm: str) -> str:
        """Return *paradigm* if registered, otherwise the default."""
        if paradigm not in self.agents:
            logger.debug(
                "Paradigm {!r} not in agents, falling back to {!r}",
                paradigm,
                self._default,
            )
            return self._default
        return paradigm


# ---------------------------------------------------------------------------
# LLM classifier
# ---------------------------------------------------------------------------


class LLMClassifier:
    """Route user input using a lightweight LLM call.

    Intended for fuzzy intents where heuristics are insufficient.  Use a
    cheap / fast model so classification overhead is negligible.

    When used as a :class:`Dispatcher` ``llm_fallback``, this is only
    invoked after the zero-cost regex layers produce no match.

    Parameters
    ----------
    provider:
        Any :class:`~providers.base.LLMProvider` implementation.
    paradigms:
        Mapping of paradigm name → one-line description.  Used to build
        the classification prompt.
    model:
        Optional model override.  When ``None`` the provider's default is
        used.  Set this to a cheap model (e.g. ``"gpt-4o-mini"``) to
        keep classification cost low.
    fallback:
        Paradigm returned when the LLM call fails or returns an
        unrecognised value.  Defaults to ``"react"``.
    """

    def __init__(
        self,
        provider: Any,  # LLMProvider (lazy import to avoid circular dep)
        paradigms: dict[str, str],
        *,
        model: str | None = None,
        fallback: str = "react",
    ) -> None:
        if not paradigms:
            raise ValueError("LLMClassifier requires at least one paradigm")
        self.provider = provider
        self.paradigms = paradigms
        self.model = model
        self.fallback = fallback

    # -- Classifier interface -----------------------------------------------

    async def __call__(self, user_input: str) -> str:
        """Classify *user_input* via a lightweight LLM call."""
        try:
            response = await self.provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": self._build_system_prompt()},
                    {"role": "user", "content": user_input},
                ],
                tools=[],
                model=self.model,
                max_tokens=10,
                temperature=0.0,
            )
            return self._parse(response.content or "")
        except Exception:
            logger.opt(exception=True).warning(
                "LLM classification failed, falling back to '{}'", self.fallback
            )
            return self.fallback

    # -- internal ------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        paradigms_text = "\n".join(
            f"- {name}: {desc}" for name, desc in self.paradigms.items()
        )
        return render_template(
            "dispatcher/classifier_system.md",
            paradigms_text=paradigms_text,
            strip=True,
        )

    def _parse(self, raw: str) -> str:
        name = raw.strip().lower()
        if name in self.paradigms:
            return name
        logger.debug(
            "LLM classifier returned unrecognised paradigm {!r}, "
            "falling back to {!r}",
            name,
            self.fallback,
        )
        return self.fallback


if __name__ == "__main__":
    pass
