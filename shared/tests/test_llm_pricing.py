"""Tests for shared.llm.pricing -- best-effort USD cost estimation.

See shared/llm/pricing.py's module docstring for why this is an estimate,
not a billing record, and why OpenRouter-routed models aren't priced here.
"""

from decimal import Decimal

from shared.llm.pricing import PRICES, estimate_cost_usd


class TestEstimateCostUsd:
    def test_known_model_computes_expected_cost(self):
        input_price, output_price = PRICES["gemini-2.5-flash"]
        cost = estimate_cost_usd("gemini-2.5-flash", 1_000_000, 1_000_000)

        assert cost == input_price + output_price

    def test_scales_with_token_count(self):
        cost_1m = estimate_cost_usd("gemini-2.5-flash", 1_000_000, 0)
        cost_2m = estimate_cost_usd("gemini-2.5-flash", 2_000_000, 0)

        assert cost_2m == cost_1m * 2

    def test_zero_tokens_is_zero_cost_not_none(self):
        # A known, priced model with zero usage has a real cost of $0 --
        # this is different from "cost unknown", which must be None.
        cost = estimate_cost_usd("gemini-2.5-flash", 0, 0)

        assert cost == Decimal("0")

    def test_unknown_model_returns_none(self):
        cost = estimate_cost_usd("some-model-not-in-the-table", 1000, 1000)

        assert cost is None

    def test_none_model_returns_none(self):
        cost = estimate_cost_usd(None, 1000, 1000)

        assert cost is None

    def test_empty_string_model_returns_none(self):
        cost = estimate_cost_usd("", 1000, 1000)

        assert cost is None

    def test_openrouter_normalized_model_id_is_unpriced(self):
        # See pricing.py's module docstring: OpenRouter's provider-slug ids
        # (google/gemini-2.5-flash) are deliberately not in PRICES.
        cost = estimate_cost_usd("google/gemini-2.5-flash", 1000, 1000)

        assert cost is None

    def test_never_guesses_a_default_price_for_unknown_model(self):
        # Regression guard for the "never fall back to a default price" rule
        # in the module docstring -- an unknown model must not silently
        # inherit gemini-2.5-flash's (or any other model's) price.
        assert "definitely-not-a-real-model-id" not in PRICES
        cost = estimate_cost_usd("definitely-not-a-real-model-id", 999, 999)

        assert cost is None

    def test_all_priced_models_have_positive_rates(self):
        # Guards against a copy-paste zero making a model look free.
        for model, (input_price, output_price) in PRICES.items():
            assert input_price > 0, f"{model} has non-positive input price"
            assert output_price > 0, f"{model} has non-positive output price"
