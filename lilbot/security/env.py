"""Credential isolation for agent-launched processes.

`config.load_dotenv()` puts the model API key into ``os.environ``, and child
processes inherit it by default — so any shell command the model chose
(``echo $env:LILBOT_API_KEY``) could read the key. This module builds the
environment for those processes with secret-looking variables removed.

A denylist (not an allowlist) on purpose: PowerShell and dev toolchains on
Windows depend on many variables (SystemRoot, PSModulePath, VIRTUAL_ENV, …) and
an allowlist breaks them in hard-to-diagnose ways.

Applies only to processes whose command line the model controls (shell, code
execution). Hooks and MCP servers are configured by the user and keep the full
environment, since they may legitimately need credentials.
"""
from __future__ import annotations

import os
from typing import Mapping

# Name parts (split on "_") that mark a variable as a secret.
_SECRET_PARTS = {"TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS", "APIKEY"}
# Substrings that mark a variable as a secret.
_SECRET_SUBSTRINGS = ("API_KEY", "ACCESS_KEY", "PRIVATE_KEY", "SECRET_KEY")

# Comma-separated names the user explicitly wants passed through anyway.
PASSTHROUGH_ENV = "LILBOT_CHILD_ENV_ALLOW"


def is_secret_env_name(name: str) -> bool:
    upper = (name or "").upper()
    if not upper:
        return False
    if upper.endswith("_KEY") or any(s in upper for s in _SECRET_SUBSTRINGS):
        return True
    return any(part in _SECRET_PARTS for part in upper.split("_"))


_MIN_SECRET_LEN = 8  # shorter values would redact ordinary words


def known_secret_values(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Map secret value -> variable name for every secret LilBot itself holds.

    ``extra`` adds values that never went through the environment (``--api-key``).
    """
    found: dict[str, str] = {}
    for name, value in {**os.environ, **(extra or {})}.items():
        if value and len(value) >= _MIN_SECRET_LEN and is_secret_env_name(name):
            found.setdefault(value, name)
    return found


def redact_known_secrets(text: str, extra: Mapping[str, str] | None = None) -> str:
    """Replace exact occurrences of held secrets with ``[REDACTED:<NAME>]``.

    Unlike ``secrets.redact_secrets`` (pattern-based, display only), this runs on
    tool output *before it reaches the model*, so a file tool reading ``.env`` or
    a grep hitting the key cannot put the credential into the context, the
    session file, or a prompt-injected exfiltration command.
    """
    if not text:
        return text
    # Longest first, so a secret that contains another is replaced whole.
    for value, name in sorted(known_secret_values(extra).items(), key=lambda kv: -len(kv[0])):
        if value in text:
            text = text.replace(value, f"[REDACTED:{name}]")
    return text


def scrubbed_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy of ``base`` (default ``os.environ``) without secret-looking variables."""
    source = os.environ if base is None else base
    allow = {n.strip().upper() for n in source.get(PASSTHROUGH_ENV, "").split(",") if n.strip()}
    return {
        name: value
        for name, value in source.items()
        if name.upper() in allow or not is_secret_env_name(name)
    }
