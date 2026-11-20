"""USD price table and per-request cost estimation.

Token counts are the runtime's own measurement; money is a derived view. The
table below is a local snapshot, not a billing authority: providers change
prices, regional endpoints differ, and an unknown model must say "price
unknown" rather than silently pretending to be free.

The one rule that matters for the agent: a budget is enforced against the
*same* numbers the operator sees in ``/tokens``. An estimate that is off by a
few percent is acceptable; a cost that is invisible until the bill arrives is
not.
"""
from __future__ import annotations

from dataclasses import dataclass

# Snapshot date. Update this whenever the table changes; the UI shows it so a
# stale table is visible instead of silently trusted.
PRICE_TABLE_DATE = "2026-11"


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float
    cache_read_per_million: float | None = None
    source: str = "table"

    def as_dict(self) -> dict[str, object]:
        return {
            "input_per_million": self.input_per_million,
            "output_per_million": self.output_per_million,
            "cache_read_per_million": self.cache_read_per_million,
            "source": self.source,
            "table_date": PRICE_TABLE_DATE,
        }


# USD per 1,000,000 tokens. Input is the uncached prompt price; cache_read is
# the discounted price for a prompt-cache hit. Values are representative and
# intentionally easy to correct in one place.
MODEL_PRICES: dict[str, ModelPrice] = {
    # DeepSeek (the default local experiments use deepseek-chat / "flash")
    "deepseek-chat": ModelPrice(0.27, 1.10, 0.07),
    "deepseek-reasoner": ModelPrice(0.55, 2.19, 0.14),
    # OpenAI
    "gpt-4o": ModelPrice(2.50, 10.00, 1.25),
    "gpt-4o-mini": ModelPrice(0.15, 0.60, 0.075),
    "gpt-4.1": ModelPrice(2.00, 8.00, 0.50),
    "gpt-4.1-mini": ModelPrice(0.40, 1.60, 0.10),
    "o1": ModelPrice(15.00, 60.00, 7.50),
    "o1-mini": ModelPrice(1.10, 4.40, 0.55),
    "o3": ModelPrice(2.00, 8.00, 0.50),
    "o3-mini": ModelPrice(1.10, 4.40, 0.55),
    # Anthropic
    "claude-3-5-sonnet": ModelPrice(3.00, 15.00, 0.30),
    "claude-3-7-sonnet": ModelPrice(3.00, 15.00, 0.30),
    "claude-sonnet-4": ModelPrice(3.00, 15.00, 0.30),
    "claude-opus-4": ModelPrice(15.00, 75.00, 1.50),
    "claude-haiku-4": ModelPrice(1.00, 5.00, 0.10),
    # Google
    "gemini-2.5-pro": ModelPrice(1.25, 10.00, 0.31),
    "gemini-2.5-flash": ModelPrice(0.30, 2.50, 0.075),
    "gemini-2.0-flash": ModelPrice(0.10, 0.40, 0.025),
    # Alibaba / Moonshot / Zhipu
    "qwen-max": ModelPrice(1.60, 6.40, 0.40),
    "qwen-plus": ModelPrice(0.40, 1.20, 0.10),
    "qwen-turbo": ModelPrice(0.05, 0.20, 0.01),
    "kimi-k2": ModelPrice(0.60, 2.50, 0.15),
    "glm-4.5": ModelPrice(0.60, 2.20, 0.15),
}

# Ordered family patterns for ids that are not exact keys (previews, regional
# suffixes, local quantized names).
MODEL_FAMILY_PRICES: tuple[tuple[str, ModelPrice], ...] = (
    ("deepseek-chat", ModelPrice(0.27, 1.10, 0.07)),
    ("deepseek-reasoner", ModelPrice(0.55, 2.19, 0.14)),
    ("deepseek", ModelPrice(0.27, 1.10, 0.07)),
    ("gpt-4o-mini", ModelPrice(0.15, 0.60, 0.075)),
    ("gpt-4o", ModelPrice(2.50, 10.00, 1.25)),
    ("gpt-4.1-mini", ModelPrice(0.40, 1.60, 0.10)),
    ("gpt-4.1", ModelPrice(2.00, 8.00, 0.50)),
    ("o3-mini", ModelPrice(1.10, 4.40, 0.55)),
    ("o3", ModelPrice(2.00, 8.00, 0.50)),
    ("o1-mini", ModelPrice(1.10, 4.40, 0.55)),
    ("o1", ModelPrice(15.00, 60.00, 7.50)),
    ("claude", ModelPrice(3.00, 15.00, 0.30)),
    ("gemini", ModelPrice(1.25, 10.00, 0.31)),
    ("qwen", ModelPrice(0.40, 1.20, 0.10)),
    ("kimi", ModelPrice(0.60, 2.50, 0.15)),
    ("moonshot", ModelPrice(0.60, 2.50, 0.15)),
    ("glm", ModelPrice(0.60, 2.20, 0.15)),
)

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")
_OFFLINE = {"mock", "rule", "rule-based", "offline", "none", ""}


def _host(base_url: str) -> str:
    text = (base_url or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0]


def price_for(model: str = "", base_url: str = "") -> ModelPrice | None:
    """Return the price for one model, or None when it is unknown.

    Local endpoints and the offline provider are explicitly free; an unknown
    cloud model returns None so the caller can show "price unknown" instead of
    a fabricated zero.
    """
    provider = (model or "").strip().lower()
    if provider in _OFFLINE:
        return ModelPrice(0.0, 0.0, 0.0, source="offline")
    host = _host(base_url)
    if host and any(local in host for local in _LOCAL_HOSTS):
        return ModelPrice(0.0, 0.0, 0.0, source="local")
    exact = MODEL_PRICES.get(provider)
    if exact:
        return exact
    for pattern, price in MODEL_FAMILY_PRICES:
        if pattern in provider:
            return price
    return None


@dataclass(frozen=True)
class CostEstimate:
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    input_cost_usd: float
    cache_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    price: ModelPrice

    def as_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "input_cost_usd": round(self.input_cost_usd, 6),
            "cache_cost_usd": round(self.cache_cost_usd, 6),
            "output_cost_usd": round(self.output_cost_usd, 6),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "price": self.price.as_dict(),
        }


def estimate_cost(usage: dict[str, object], price: ModelPrice) -> CostEstimate:
    """Estimate one request's cost from a provider usage block.

    ``prompt_tokens`` is the full input count and ``cache_read_tokens`` is the
    cached subset of it, so the uncached input is the difference. A provider
    that reports a cache count larger than the prompt is clamped rather than
    trusted into a negative charge.
    """
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cached = int(usage.get("cache_read_tokens") or 0)
    cached = max(0, min(cached, prompt))
    uncached = max(0, prompt - cached)
    cache_price = (
        price.cache_read_per_million
        if price.cache_read_per_million is not None
        else price.input_per_million
    )
    input_cost = uncached / 1_000_000 * price.input_per_million
    cache_cost = cached / 1_000_000 * cache_price
    output_cost = completion / 1_000_000 * price.output_per_million
    return CostEstimate(
        input_tokens=uncached,
        cached_tokens=cached,
        output_tokens=completion,
        input_cost_usd=input_cost,
        cache_cost_usd=cache_cost,
        output_cost_usd=output_cost,
        total_cost_usd=input_cost + cache_cost + output_cost,
        price=price,
    )


def format_usd(value: float) -> str:
    """A compact USD string: sub-cent values keep four significant digits."""
    if value <= 0:
        return "$0.0000"
    if value < 0.01:
        return f"${value:.6f}"
    return f"${value:.4f}"
