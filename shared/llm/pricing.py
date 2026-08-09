"""Best-effort USD cost estimation from token counts.

This is an ESTIMATE, not a billing record. Anansi's LLM clients (see
``orchestrator/clients/gemini.py`` and ``orchestrator/clients/openrouter.py``)
report only token counts -- neither the Gemini Developer API nor the
OpenRouter integration surfaces an actual per-call cost, so any dollar figure
shown to an operator is this table's price multiplied by observed tokens, not
a number the provider billed.

Prices below cover the direct Gemini Developer API models configured via
``MODEL_THINKING`` / ``MODEL_FAST`` / ``MODEL_LITE`` / ``FALLBACK_MODEL``
(see ``shared/llm/model_tiers.py``).

*** PRICING NOT LIVE-VERIFIED THIS SESSION ***
The web lookup used to confirm these against ai.google.dev/gemini-api/docs/pricing
at implementation time failed (tool infrastructure error, not a missing
page). These values are carried from training data and are NOT confirmed
current as of this change. Verify against the pricing page above before
trusting `cost_usd` output for any real financial decision, and update
`_PRICES_LAST_CHECKED` once done.

OpenRouter-routed models are deliberately NOT priced here. OpenRouter can
route a single logical model name to different underlying providers/prices
over time, and `normalize_openrouter_model()` (shared/llm/openrouter.py)
remaps Gemini's bare model id to a provider-slug id (e.g. "gemini-2.5-flash"
-> "google/gemini-2.5-flash") that this table does not carry. Runs on that
path report tokens with no `cost_usd` -- this is the intended "unknown model"
fallback, not a gap to silently paper over with a guessed number.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

# Last time the table below was checked against the provider's published
# pricing page. Update this alongside any price change.
_PRICES_LAST_CHECKED = "not verified (web lookup unavailable 2026-08-06)"

# model id -> (usd per 1M input tokens, usd per 1M output tokens)
# Source (unverified this session, see module docstring):
# https://ai.google.dev/gemini-api/docs/pricing -- paid tier, text in/out.
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gemini-2.5-flash": (Decimal("0.30"), Decimal("2.50")),
    "gemini-2.5-flash-lite": (Decimal("0.10"), Decimal("0.40")),
    "gemini-2.5-pro": (Decimal("1.25"), Decimal("10.00")),
    # "gemini-pro-latest" is a rolling alias; priced here as the Pro-tier rate
    # it has historically pointed to. Confirm it hasn't been repointed.
    "gemini-pro-latest": (Decimal("1.25"), Decimal("10.00")),
}

_PER_MILLION = Decimal("1000000")


def estimate_cost_usd(
    model: Optional[str], input_tokens: int, output_tokens: int
) -> Optional[Decimal]:
    """Estimate USD cost for a call, or None if `model` isn't in `PRICES`.

    Never guesses a price for an unrecognized model -- callers must treat
    `None` as "cost unknown" and show that explicitly (e.g. "--"), not "$0".
    """
    if not model:
        return None
    prices = PRICES.get(model)
    if prices is None:
        return None
    input_price, output_price = prices
    cost = (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / (
        _PER_MILLION
    )
    return cost


__all__ = ["PRICES", "estimate_cost_usd"]
