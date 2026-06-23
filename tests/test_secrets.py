"""Tests for secret redaction (M1).

All secret-shaped fixtures are ASSEMBLED at runtime from fragments so the source
file contains no contiguous secret literal (avoids tripping secret scanners) and
no real credential is ever committed.
"""
from __future__ import annotations

from lilbot.security import redact_args, redact_secrets

# Fake, assembled fixtures — never real, never a contiguous literal.
_FAKE_SK = "sk-" + "0123456789abcdef" * 2          # DeepSeek/OpenAI shape (32 hex)
_FAKE_GHP = "ghp_" + "b" * 32                        # GitHub token shape
_FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"              # AWS access-key-id shape
_FAKE_JWT = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12   # JWT shape (3 segments)


def test_redacts_env_api_key_assignment():
    line = "DEEPSEEK_API_KEY=" + _FAKE_SK
    out = redact_secrets(line)
    assert _FAKE_SK not in out
    assert "[REDACTED]" in out
    assert out.endswith(_FAKE_SK[-4:])  # short tail kept for identification


def test_bare_sk_token_masked():
    out = redact_secrets("here is the key " + _FAKE_SK + " and more")
    assert _FAKE_SK not in out
    assert "[REDACTED]" in out


def test_github_and_aws_masked():
    assert _FAKE_GHP not in redact_secrets("token " + _FAKE_GHP)
    assert _FAKE_AWS not in redact_secrets("aws " + _FAKE_AWS + " here")


def test_jwt_masked():
    assert _FAKE_JWT not in redact_secrets("auth " + _FAKE_JWT)


def test_private_key_block_redacted():
    block = "-----BEGIN RSA PRIVATE KEY-----\n" + ("M" * 20) + "\n-----END RSA PRIVATE KEY-----"
    out = redact_secrets(block)
    assert ("M" * 20) not in out
    assert "[REDACTED PRIVATE KEY]" in out


def test_non_secret_lines_preserved():
    assert redact_secrets("LILBOT_BASE_URL=https://api.deepseek.com") == "LILBOT_BASE_URL=https://api.deepseek.com"
    assert redact_secrets("x = compute(1, 2, 3)") == "x = compute(1, 2, 3)"


def test_numeric_config_not_masked():
    assert redact_secrets("MAX_TOKENS=128000") == "MAX_TOKENS=128000"


def test_author_not_masked():
    assert redact_secrets("AUTHOR=shenxin") == "AUTHOR=shenxin"


def test_redact_args_recurses():
    args = {"path": ".env", "content": "TOKEN=" + _FAKE_GHP, "n": 5}
    out = redact_args(args)
    assert _FAKE_GHP not in out["content"]
    assert out["path"] == ".env"
    assert out["n"] == 5


def test_redact_secrets_safe_on_non_string():
    assert redact_secrets("") == ""
    assert redact_args(5) == 5
    assert redact_args(None) is None
