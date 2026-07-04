"""Tests for context/token_budget.py — threshold computation."""

from __future__ import annotations

from context.token_budget import TokenBudget


class TestTokenBudgetThresholds:
    def test_default_thresholds_200k(self):
        tb = TokenBudget(context_window=200_000, max_output_tokens=20_000)
        assert tb.effective_window == 180_000
        assert tb.warning_threshold > 0
        assert tb.auto_compact_threshold > 0
        assert tb.block_threshold > 0
        # block > auto_compact > warning (block fires when most filled)
        assert tb.block_threshold > tb.auto_compact_threshold > tb.warning_threshold

    def test_thresholds_scale_for_128k(self):
        # max_output_tokens capped at 10%: 128_000 * 0.1 = 12_800
        tb = TokenBudget(context_window=128_000, max_output_tokens=16_384)
        assert tb.effective_window == 128_000 - 12_800
        tb200 = TokenBudget(context_window=200_000, max_output_tokens=20_000)
        assert tb.warning_threshold < tb200.warning_threshold
        assert tb.block_threshold < tb200.block_threshold

    def test_custom_ratios_reflect_in_computed_properties(self):
        tb = TokenBudget(
            context_window=100_000,
            max_output_tokens=10_000,
            warning_buffer_ratio=0.2,
            auto_compact_buffer_ratio=0.1,
            block_buffer_ratio=0.05,
        )
        ew = 90_000
        assert tb.effective_window == ew
        assert tb.warning_threshold == int(ew * 0.8)
        assert tb.auto_compact_threshold == int(ew * 0.9)
        assert tb.block_threshold == int(ew * 0.95)
        assert tb.block_threshold > tb.auto_compact_threshold > tb.warning_threshold

    def test_max_output_tokens_capped_at_10_percent(self):
        tb = TokenBudget(context_window=100_000, max_output_tokens=50_000)
        assert tb.effective_window == 90_000

    def test_compress_ratio_passed_through(self):
        tb = TokenBudget(compress_ratio=0.6)
        assert tb.compress_ratio == 0.6

    def test_idle_compress_seconds_passed_through(self):
        tb = TokenBudget(idle_compress_seconds=600)
        assert tb.idle_compress_seconds == 600
