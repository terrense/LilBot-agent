"""Per-model context windows.

The compaction trigger and the context bar must agree on how large the model's
window is. A single hardcoded constant cannot do that: the same runtime talks
to a 64k model and a 1M model, and it also talks to a local server whose limit
is whatever the operator configured.

Resolution order, first match wins:

1. an **explicit** operator setting (``LILBOT_CONTEXT_WINDOW``) always wins;
2. an **exact model id** in the table;
3. the longest matching **model-family pattern** in the table;
4. the **provider default** derived from the endpoint host;
5. the configured fallback.

The resolver returns the window together with its ``source`` so the UI and
``/tokens`` can say *why* a number was chosen instead of presenting a magic
constant.
"""
from __future__ import annotations

from dataclasses import dataclass

# Fallback when nothing else matches: a conservative window that keeps the
# runtime usable on an unknown endpoint without pretending to know the real
# limit. Providers that accept more still work; the runtime just compacts
# earlier than strictly necessary.
DEFAULT_CONTEXT_WINDOW = 32_000

# Exact model ids. Kept separate from the family patterns so a specific model
# can be corrected without touching its family.
MODEL_WINDOWS: dict[str, int] = {
    # DeepSeek
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4-turbo": 128_000,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    # Anthropic
    "claude-3-5-sonnet": 200_000,
    "claude-3-7-sonnet": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "claude-haiku-4": 200_000,
    # Google
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    # Alibaba
    "qwen-max": 32_768,
    "qwen-plus": 131_072,
    "qwen-turbo": 1_000_000,
    "qwen2.5-72b-instruct": 131_072,
    "qwen2.5-coder": 131_072,
    "qwen3-235b-a22b": 262_144,
    # Moonshot
    "moonshot-v1-8k": 8_192,
    "moonshot-v1-32k": 32_768,
    "moonshot-v1-128k": 131_072,
    "kimi-k2": 131_072,
    # Zhipu
    "glm-4": 128_000,
    "glm-4.5": 128_000,
    # Meta / Mistral / Groq style ids
    "llama-3.3-70b-versatile": 131_072,
    "llama-3.1-405b-instruct": 131_072,
    "mistral-large-latest": 131_072,
    "mixtral-8x7b-instruct": 32_768,
}

# Family patterns, matched as substrings. Ordered longest-first at lookup time
# so "qwen2.5-coder" wins over "qwen2.5".
MODEL_FAMILY_WINDOWS: tuple[tuple[str, int], ...] = (
    ("deepseek", 64_000),
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("gpt-5", 400_000),
    ("gpt-4", 128_000),
    ("o3-mini", 200_000),
    ("o3", 200_000),
    ("o1", 200_000),
    ("claude", 200_000),
    ("gemini", 1_048_576),
    ("qwen3", 262_144),
    ("qwen2.5", 131_072),
    ("qwen", 131_072),
    ("kimi", 131_072),
    ("moonshot", 131_072),
    ("glm", 128_000),
    ("llama-3.3", 131_072),
    ("llama-3.1", 131_072),
    ("llama", 131_072),
    ("mistral", 131_072),
    ("mixtral", 32_768),
)

# Endpoint-derived defaults, used when the model id is unknown.
PROVIDER_WINDOWS: tuple[tuple[str, int], ...] = (
    ("api.deepseek.com", 64_000),
    ("api.anthropic.com", 200_000),
    ("api.openai.com", 128_000),
    ("openrouter.ai", 128_000),
    ("generativelanguage.googleapis.com", 1_048_576),
    ("dashscope.aliyuncs.com", 131_072),
    ("api.moonshot.cn", 131_072),
    ("open.bigmodel.cn", 128_000),
    ("api.groq.com", 131_072),
    ("api.mistral.ai", 131_072),
)

# Endpoints whose window is whatever the operator runs locally.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


@dataclass(frozen=True)
class ContextWindow:
    """A resolved window and the reason it was chosen."""

    window: int
    source: str
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"window": self.window, "source": self.source, "detail": self.detail}


def _host(base_url: str) -> str:
    text = (base_url or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0]


def resolve_context_window(
    model: str = "",
    base_url: str = "",
    configured: int = 0,
    explicit: bool = False,
) -> ContextWindow:
    """Resolve the context window for one endpoint and model.

    ``configured`` is the operator's fallback value; ``explicit`` says whether
    they actually chose it. An explicit choice is never overridden by the
    table, because the table is a guess about a remote service while the
    operator's setting is a statement about their own setup.
    """
    if explicit and configured > 0:
        return ContextWindow(configured, "configured", "explicit setting")

    host = _host(base_url)
    if host and any(local in host for local in _LOCAL_HOSTS):
        # A local server's context length is whatever the operator configured
        # on that server, not what the cloud service for the same model
        # happens to offer. The model table describes hosted endpoints.
        return ContextWindow(configured or DEFAULT_CONTEXT_WINDOW, "local_endpoint", host)

    name = (model or "").strip().lower()
    if name:
        exact = MODEL_WINDOWS.get(name)
        if exact:
            return ContextWindow(exact, "model", name)
        for pattern, window in sorted(MODEL_FAMILY_WINDOWS, key=lambda item: -len(item[0])):
            if pattern in name:
                return ContextWindow(window, "model_family", pattern)

    if host:
        for pattern, window in PROVIDER_WINDOWS:
            if pattern in host:
                return ContextWindow(window, "provider", host)

    if configured > 0:
        return ContextWindow(configured, "configured", "fallback setting")
    return ContextWindow(DEFAULT_CONTEXT_WINDOW, "default", "unknown endpoint")
