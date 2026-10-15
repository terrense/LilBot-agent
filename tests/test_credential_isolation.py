"""Credential isolation: the model must not be able to read LilBot's own API key.

Regression for: `.env` is loaded into os.environ, child processes inherited it,
and `echo $env:LILBOT_API_KEY` / `cat .env` were auto-allowed as read-only —
even under --permission-mode deny-all.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lilbot import cli
from lilbot.config import LilBotConfig
from lilbot.sandbox.execpolicy import classify
from lilbot.sandbox.workspace import Sandbox
from lilbot.security.env import is_secret_env_name, redact_known_secrets, scrubbed_env
from lilbot.tui.classic import LilBotUI

FAKE_KEY = "sk-cp-test0123456789abcdefFAKEKEY"


@pytest.mark.parametrize("name", [
    "LILBOT_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN", "GH_TOKEN", "HF_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "DB_PASSWORD", "MY_SECRET", "SSH_KEY", "lilbot_api_key",
])
def test_secret_names_detected(name):
    assert is_secret_env_name(name)


@pytest.mark.parametrize("name", [
    "PATH", "SystemRoot", "PSModulePath", "USERPROFILE", "VIRTUAL_ENV", "PYTHONPATH",
    "TOKENIZERS_PARALLELISM", "LILBOT_MODEL", "LILBOT_BASE_URL", "KEYBOARD_LAYOUT",
])
def test_ordinary_names_kept(name):
    assert not is_secret_env_name(name)


def test_scrubbed_env_drops_secrets_and_honors_passthrough():
    base = {"PATH": "/bin", "LILBOT_API_KEY": FAKE_KEY, "GITHUB_TOKEN": "ghp_x", "LILBOT_CHILD_ENV_ALLOW": "github_token"}
    env = scrubbed_env(base)
    assert env["PATH"] == "/bin"
    assert "LILBOT_API_KEY" not in env
    assert env["GITHUB_TOKEN"] == "ghp_x"  # explicitly passed through


def test_sandbox_child_process_cannot_see_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LILBOT_API_KEY", FAKE_KEY)
    code = "import os; print(os.environ.get('LILBOT_API_KEY', 'ABSENT'))"
    result = Sandbox(tmp_path).run(f'"{sys.executable}" -c "{code}"' if sys.platform != "win32"
                                   else f"& '{sys.executable}' -c \"{code}\"")
    assert result.ok, result.output
    assert "ABSENT" in result.output
    assert FAKE_KEY not in result.output


@pytest.mark.parametrize("cmd", [
    "echo $env:LILBOT_API_KEY",
    "echo ${env:GITHUB_TOKEN}",
    "echo $OPENAI_API_KEY",
    "echo %DEEPSEEK_API_KEY%",
    "Get-ChildItem env:",
    "gci Env:",
    "[Environment]::GetEnvironmentVariables()",
    "printenv",
    "cat .env",
    "cat ./config/.env.local",
    "type .envrc",
    "cat ~/.ssh/id_rsa",
    "cat ~/.aws/credentials",
    "cat ~/.git-credentials",
])
def test_secret_commands_never_auto_allowed(cmd):
    decision, reason = classify(cmd)
    assert decision == "ask", (cmd, decision, reason)
    assert reason


@pytest.mark.parametrize("cmd", [
    "echo $env:PATH", "echo hello", "cat README.md", "cat .venv/pyvenv.cfg",
    "git status", "grep -r process.env src",
])
def test_ordinary_commands_still_auto_allowed(cmd):
    assert classify(cmd)[0] == "allow", cmd


class _Rules:
    def __init__(self, decision):
        self.decision = decision

    def evaluate(self, tool, cmd):
        class _Rule:
            source = "user"

            def spec(self):
                return "Bash(cat *)"
        return self.decision, _Rule()


def test_broad_allow_rule_does_not_unlock_secrets():
    assert classify("cat .env", rules=_Rules("allow"))[0] == "ask"
    assert classify("cat README.md", rules=_Rules("allow"))[0] == "allow"


def test_deny_rule_still_denies_secret_commands():
    assert classify("cat .env", rules=_Rules("deny"))[0] == "deny"


def test_redact_known_secrets(monkeypatch):
    monkeypatch.setenv("LILBOT_API_KEY", FAKE_KEY)
    out = redact_known_secrets(f"LILBOT_API_KEY={FAKE_KEY}\nother line")
    assert FAKE_KEY not in out
    assert "[REDACTED:LILBOT_API_KEY]" in out
    assert "other line" in out
    assert redact_known_secrets("x", {"LILBOT_API_KEY": "short"}) == "x"


def _runtime(tmp_path: Path, mode: str):
    cfg = LilBotConfig(workspace=tmp_path, permission_mode=mode)
    _agent, registry, ctx = cli.build_runtime(cfg, LilBotUI(enabled=False), interactive=False)
    return registry, ctx


def test_file_tools_never_return_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LILBOT_API_KEY", FAKE_KEY)
    (tmp_path / ".env").write_text(f"LILBOT_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    registry, ctx = _runtime(tmp_path, "accept-all")
    for name, args in [
        ("read_file", {"path": ".env"}),
        ("grep_files", {"pattern": "LILBOT_API_KEY", "path": "."}),
    ]:
        result, _ = registry.execute(name, args, ctx)
        assert result.ok, (name, result.output)
        assert FAKE_KEY not in result.output, name
        assert "[REDACTED:LILBOT_API_KEY]" in result.output, name


def test_key_passed_only_via_config_is_redacted(tmp_path, monkeypatch):
    monkeypatch.delenv("LILBOT_API_KEY", raising=False)
    (tmp_path / "notes.txt").write_text(f"key is {FAKE_KEY}\n", encoding="utf-8")
    registry, ctx = _runtime(tmp_path, "accept-all")
    ctx.config.api_key = FAKE_KEY  # e.g. supplied with --api-key
    result, _ = registry.execute("read_file", {"path": "notes.txt"}, ctx)
    assert FAKE_KEY not in result.output


def test_deny_all_blocks_secret_shell_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("LILBOT_API_KEY", FAKE_KEY)
    registry, ctx = _runtime(tmp_path, "deny-all")
    result, _ = registry.execute("exec_shell", {"command": "echo $env:LILBOT_API_KEY"}, ctx)
    assert not result.ok
    assert FAKE_KEY not in result.output
