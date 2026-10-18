"""Model pricing for ``account_usage_daily.est_cost``.

Best-effort USD estimates so a user can see roughly what an account has spent; the
governor's hard budgets are token-based and never depend on these numbers. Rates are
per **one million tokens** and are marked ``# VERIFY`` because vendor price lists
change without notice - treat every cost figure as an estimate, not an invoice.

Matching is by longest model-id prefix, so ``claude-sonnet-4-5-20250929`` and
``claude-sonnet-4-5`` can share one row while a future ``claude-sonnet-5`` falls back
to the unknown-model default (cost 0.0, tokens still recorded).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.contracts.ledger import TokenUsage

__all__ = ["PRICES", "ModelPrice", "estimate_cost"]


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens, split by billing class."""

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0


# VERIFY: list prices against the vendors' current price sheets before trusting
# est_cost. Ordered dict below is matched by longest prefix.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-4": ModelPrice(15.0, 75.0, 1.5, 18.75),
    "claude-sonnet-4": ModelPrice(3.0, 15.0, 0.3, 3.75),
    "claude-haiku-4": ModelPrice(1.0, 5.0, 0.1, 1.25),
    "claude-3-5-haiku": ModelPrice(0.8, 4.0, 0.08, 1.0),
    "claude-3-haiku": ModelPrice(0.25, 1.25, 0.03, 0.3),
    "gpt-4o-mini": ModelPrice(0.15, 0.6, 0.075, 0.0),
    "gpt-4o": ModelPrice(2.5, 10.0, 1.25, 0.0),
    "gpt-4.1-mini": ModelPrice(0.4, 1.6, 0.1, 0.0),
    "gpt-4.1": ModelPrice(2.0, 8.0, 0.5, 0.0),
    "o4-mini": ModelPrice(1.1, 4.4, 0.55, 0.0),
}


def _price_for(model: str) -> ModelPrice | None:
    """Longest-prefix match against the price table, or ``None`` when unknown."""
    best: tuple[int, ModelPrice] | None = None
    for prefix, price in PRICES.items():
        if model.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), price)
    return best[1] if best else None


def estimate_cost(model: str, usage: TokenUsage) -> float:
    """Estimated USD for one call. Unknown models cost 0.0 and are never guessed.

    ``TokenUsage.cached`` is billed at the cache-read rate (cache *creation* is
    folded into ``in_`` by the adapters at full rate - see HANDOFF).
    """
    price = _price_for(model)
    if price is None:
        return 0.0
    return (
        usage.in_ * price.input_per_mtok
        + usage.out * price.output_per_mtok
        + usage.cached * price.cache_read_per_mtok
    ) / 1_000_000.0
