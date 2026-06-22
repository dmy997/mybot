"""CLI entry point for the evaluation system.

Usage::

    python -m evals                          # run all custom tasks (react)
    python -m evals --paradigm plan_solve    # single paradigm
    python -m evals --paradigm react --paradigm plan_solve  # compare
    python -m evals --benchmark bfcl --category simple_python --max-samples 20
    python -m evals --benchmark gaia --level 1
    python -m evals --task file_read_basic   # single task
    python -m evals --output report.md       # write Markdown report
    python -m evals --json results.json      # write JSON output
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from loguru import logger


def _create_provider():
    """Create an LLM provider from environment config."""
    from config import Config
    from providers.openai_compatible_provider import OpenAICompatibleProvider

    return OpenAICompatibleProvider(
        api_key=Config.api_key,
        api_base=Config.api_base,
        name=Config.provider_name,
        default_model=Config.default_model,
    )


def _create_tools():
    """Auto-discover all available tools."""
    from config import Config
    from tools import discover_tools

    workspace = Path(Config.workspace).expanduser().resolve()
    return discover_tools(workspace=workspace)


def _get_workspace() -> Path | None:
    try:
        from config import Config
        return Path(Config.workspace).expanduser().resolve()
    except Exception:
        return None


async def _run_custom_tasks(args):
    """Run Layer 1 custom YAML task evaluation."""
    from evals.runner import discover_tasks, run_suite
    from evals.reporter import MarkdownReporter, TerminalReporter, to_json

    all_tasks = discover_tasks()
    if args.task:
        all_tasks = [t for t in all_tasks if t.id == args.task]
        if not all_tasks:
            print(f"Task {args.task!r} not found. Available: {[t.id for t in discover_tasks()]}")
            sys.exit(1)

    print(f"Running {len(all_tasks)} task(s)...")

    provider = _create_provider()
    tools = _create_tools()
    paradigms = args.paradigm if args.paradigm else ["react"]

    results = []
    for p in paradigms:
        print(f"\n--- Paradigm: {p} ---")
        batch = await run_suite(all_tasks, provider, tools, paradigm=p, model=args.model)
        results.extend(batch)

    # Terminal output
    print(TerminalReporter.render(results))

    # Markdown report
    if args.output:
        md = MarkdownReporter.render(results)
        Path(args.output).write_text(md, encoding="utf-8")
        print(f"Report written to {args.output}")

    # JSON output
    if args.json:
        Path(args.json).write_text(to_json(results), encoding="utf-8")
        print(f"JSON written to {args.json}")

    # Exit code
    passed = sum(1 for r in results if r.passed)
    if passed < len(results):
        sys.exit(1)


async def _run_benchmark(args):
    """Run Layer 2 community benchmark evaluation."""
    from evals.reporter import MarkdownReporter, TerminalReporter

    provider = _create_provider()

    if args.benchmark == "bfcl":
        _check_bfcl_data(args)
        from evals.benchmarks.bfcl import BFCLEvaluator

        evaluator = BFCLEvaluator(data_dir=args.bfcl_data_dir)
        category = args.category or "simple_python"

        def agent_factory(functions):
            # BFCL evaluates raw function-calling: create agent with given functions as tools
            from core.runner import AgentCore, AgentInput
            from tools import ToolRegistry

            registry = ToolRegistry()
            core = AgentCore(provider=provider, workspace=_get_workspace())
            # Build spec with functions injected into system prompt
            func_desc = json.dumps(functions, indent=2)
            spec = AgentInput(
                init_messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a function-calling assistant. "
                            "When the user asks a question, respond with a JSON function call. "
                            "Available functions:\n" + func_desc
                        ),
                    }
                ],
                tools=registry,
                session_key=f"bfcl-eval-{category}",
                checkpoint=False,
            )
            return _BFCLAgent(core, spec)

        print(f"Running BFCL benchmark: category={category}, max_samples={args.max_samples}")
        result = await evaluator.evaluate(
            agent_factory,
            category=category,
            max_samples=args.max_samples,
        )

    elif args.benchmark == "gaia":
        from evals.benchmarks.gaia import GAIAEvaluator

        evaluator = GAIAEvaluator(data_dir=args.gaia_data_dir)

        def agent_factory():
            tools = _create_tools()
            from core.runner import AgentCore, AgentInput
            from tools import ToolRegistry

            registry = ToolRegistry()
            for t in tools.values():
                registry.register(t)

            core = AgentCore(provider=provider, workspace=_get_workspace())
            spec = AgentInput(
                init_messages=[],
                tools=registry,
                session_key=f"gaia-eval-l{args.level or 'all'}",
                checkpoint=False,
            )
            return _GAIAAgent(core, spec)

        print(f"Running GAIA benchmark: level={args.level}, max_samples={args.max_samples}")
        result = await evaluator.evaluate(
            agent_factory,
            level=args.level,
            max_samples=args.max_samples,
        )
    else:
        print(f"Unknown benchmark: {args.benchmark}")
        sys.exit(1)

    # Print metrics
    metrics = result["metrics"]
    print(json.dumps(metrics, indent=2))

    if args.output:
        # Generate a simple markdown report for benchmarks
        lines = [
            f"# {args.benchmark.upper()} Benchmark Report",
            "",
            "## Metrics",
            "",
            "```json",
            json.dumps(metrics, indent=2),
            "```",
        ]
        Path(args.output).write_text("\n".join(lines), encoding="utf-8")
        print(f"Report written to {args.output}")


# -- lightweight agent wrappers for benchmarks ----------------------------


class _BFCLAgent:
    """Minimal agent wrapper for BFCL evaluation."""

    def __init__(self, core, spec):
        self.core = core
        self.spec = spec

    async def run(self, question: str | list) -> str:
        # BFCL questions can be [[{role, content}, ...]] nested format
        content = self._extract_content(question)
        self.spec.init_messages.append({"role": "user", "content": content})
        output = await self.core.run(self.spec)
        return output.content

    @staticmethod
    def _extract_content(question: str | list) -> str:
        if isinstance(question, str):
            return question
        # Drill into nested lists to find message dicts
        while isinstance(question, list) and len(question) > 0:
            question = question[0]
        if isinstance(question, dict):
            return question.get("content", str(question))
        return str(question)


class _GAIAAgent:
    """Minimal agent wrapper for GAIA evaluation."""
    def __init__(self, core, spec):
        self.core = core
        self.spec = spec

    async def run(self, question: str) -> str:
        # Use GAIA system prompt
        self.spec.init_messages = [
            {
                "role": "system",
                "content": (
                    "You are a general AI assistant. I will ask you a question. "
                    "Report your thoughts, and finish your answer with the following template: "
                    "FINAL ANSWER: [YOUR FINAL ANSWER]. "
                    "YOUR FINAL ANSWER should be a number OR as few words as possible "
                    "OR a comma separated list of numbers and/or strings. "
                    "If you are asked for a number, don't use comma to write your number "
                    "neither use units such as $ or percent sign unless specified otherwise. "
                    "If you are asked for a string, don't use articles, neither abbreviations "
                    "(e.g. for cities), and write the digits in plain text unless specified otherwise. "
                    "If you are asked for a comma separated list, apply the above rules depending "
                    "of whether the element to be put in the list is a number or a string."
                ),
            },
            {"role": "user", "content": question},
        ]
        output = await self.core.run(self.spec)
        return output.content


# -- data check -----------------------------------------------------------


def _check_bfcl_data(args):
    if args.bfcl_data_dir:
        return
    # Check common locations
    candidates = [
        "temp_gorilla/berkeley-function-call-leaderboard/bfcl_eval/data",
        "../gorilla/berkeley-function-call-leaderboard/bfcl_eval/data",
    ]
    for c in candidates:
        if Path(c).exists():
            args.bfcl_data_dir = c
            return
    print(
        "BFCL data not found. Clone it first:\n"
        "  git clone https://github.com/ShishirPatil/gorilla.git temp_gorilla\n"
        "Or pass --bfcl-data-dir <path>"
    )
    sys.exit(1)


# -- main -----------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="mybot Agent Evaluation System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--task", help="Run a single task by ID")
    parser.add_argument(
        "--paradigm", action="append", dest="paradigm",
        help="Agent paradigm (can repeat for comparison)"
    )
    parser.add_argument("--model", help="Model override")
    parser.add_argument("--benchmark", choices=["bfcl", "gaia"], help="Run a community benchmark")
    parser.add_argument("--category", help="BFCL category (default: simple_python)")
    parser.add_argument("--level", type=int, help="GAIA difficulty level (1-3)")
    parser.add_argument("--max-samples", type=int, default=0, dest="max_samples",
                        help="Cap benchmark samples (0=all)")
    parser.add_argument("--bfcl-data-dir", dest="bfcl_data_dir",
                        help="Path to BFCL data directory")
    parser.add_argument("--gaia-data-dir", dest="gaia_data_dir",
                        help="Path to GAIA local data directory")
    parser.add_argument("--output", "-o", help="Markdown report output path")
    parser.add_argument("--json", "-j", help="JSON output path")

    args = parser.parse_args()

    if args.benchmark:
        asyncio.run(_run_benchmark(args))
    else:
        asyncio.run(_run_custom_tasks(args))


if __name__ == "__main__":
    main()
