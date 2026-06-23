"""Secret redaction for anything shown to the human (trace / TUI / logs).

This is defense-in-depth, prompted by a real incident: a tool result printed a
DeepSeek API key (`sk-…`) into the visible trace, and a `.env` diff exposed it
again. Redaction masks API keys, tokens, private keys, and secret-looking
`KEY=value` assignments BEFORE they reach the screen.

It is applied at the presentation layer only — the model's own context keeps
raw values so functionality is unaffected; the human just never sees the secret.
"""
from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = "[REDACTED]"

# High-confidence token shapes. Order matters: private-key block first.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

_TOKEN_PATTERNS: list[re.Pattern[str]] = [
    # OpenAI / DeepSeek /  style (sk-, sk-proj-, sk-ant-)
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}\b"),
    # GitHub tokens
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # AWS access key id
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Google API key
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    # Slack
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # JWT (three base64url segments)
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # Bearer header
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}"),
]

# Secret-looking assignments: FOO_API_KEY=..., password: "...", token=...
# AUTH is intentionally specific (AUTHORIZATION / AUTH_TOKEN) so it does not hit
# AUTHOR. Purely numeric values (e.g. MAX_TOKENS=128000) are left alone in _assign_sub.
_ASSIGNMENT = re.compile(
    r"(?i)([A-Z0-9_]*(?:SECRET|TOKEN|API[_-]?KEY|APIKEY|PASSWORD|PASSWD|"
    r"PRIVATE[_-]?KEY|ACCESS[_-]?KEY|CLIENT[_-]?SECRET|AUTHORIZATION|AUTH[_-]?TOKEN)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)(['\"]?)([^\s'\"]{6,})(\3)"
)


def _mask(secret: str) -> str:
    s = secret.strip("'\"")
    if len(s) <= 8:
        return PLACEHOLDER
    # Keep a short tail so the user can recognize *which* key without recovering it.
    return f"{PLACEHOLDER}…{s[-4:]}"


def redact_secrets(text: str) -> str:
    """Return text with secrets masked. Safe to call on any string."""
    if not text or not isinstance(text, str):
        return text

    out = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)

    def _assign_sub(m: re.Match[str]) -> str:
        val = m.group(4)
        # Leave purely numeric config values alone (e.g. MAX_TOKENS=128000).
        if val.isdigit():
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{_mask(val)}{m.group(5)}"

    out = _ASSIGNMENT.sub(_assign_sub, out)

    for pat in _TOKEN_PATTERNS:
        out = pat.sub(lambda m: _mask(m.group(0)), out)
    return out


def redact_args(value: Any) -> Any:
    """Recursively redact secrets in tool-call arguments for display."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: redact_args(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_args(v) for v in value]
    return value
