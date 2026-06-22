"""pytest integration for eval tasks.

Each YAML task file under ``evals/tasks/`` becomes a parametrized test case.
In CI mode (default), the eval pipeline (loading -> scoring -> reporting) is
tested without live LLM calls.  Use ``--live-eval`` to run against a real
LLM provider.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live-eval",
        action="store_true",
        default=False,
        help="Run eval tasks against a real LLM provider",
    )


@pytest.fixture
def live_eval(request):
    return request.config.getoption("--live-eval")
