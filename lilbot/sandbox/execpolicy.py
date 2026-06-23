"""Command-safety / execution policy.

Two jobs, evaluated before a shell command runs:

  * DENY clearly catastrophic commands (rm -rf /, fork bombs, curl|sh, …) so a
    model mistake cannot wreck the machine.
  * AUTO-ALLOW known read-only commands using *arity-aware prefix matching*:
    an allow rule `git status` matches `git status -s` and `git status
    --porcelain` (flags ignored) but NOT `git push`. This removes pointless
    approval prompts for safe inspection commands.

Everything else falls through to the normal permission prompt ("ask").
"""
from __future__ import annotations

import re
import shlex

# Arity = number of leading *positional* tokens (flags excluded, base word
# included) that form a command's canonical prefix. Kept for reference / future
# use; the allow matcher derives arity from each allow-rule's own length.
BASH_ARITY: dict[str, int] = {
    "git status": 2, "git diff": 2, "git log": 2, "git show": 2, "git blame": 2,
    "git branch": 2, "git remote": 2, "git config": 2, "git rev-parse": 2,
    "npm run": 3, "npm ls": 2, "cargo build": 2, "cargo test": 2,
    "ls": 1, "cat": 1, "pwd": 1, "echo": 1, "head": 1, "tail": 1, "wc": 1,
    "which": 1, "whoami": 1, "date": 1, "env": 1, "df": 1, "uname": 1,
}

# Commands auto-allowed (read-only / harmless). Matching ignores flags.
SAFE_ALLOW_PREFIXES: set[str] = {
    "git status", "git diff", "git log", "git show", "git blame", "git branch",
    "git remote", "git rev-parse", "git config --get", "git stash list",
    "ls", "pwd", "cat", "head", "tail", "wc", "which", "whoami", "date",
    "uname", "df", "echo", "grep", "find", "tree", "file", "stat",
    "python --version", "python3 --version", "node --version", "npm --version",
    "cargo --version", "go version", "rustc --version", "pip --version",
}

# Shell operators that make a command compound — never auto-classify those.
_SHELL_OPERATORS = re.compile(r"[;&|<>`]|\$\(|\bsudo\b|\n")

# Targets that make an `rm` catastrophic (root / home / cwd / glob-everything).
_CATASTROPHIC_RM_TARGETS = {"/", "/*", "~", "~/", "~/*", "*", ".", "./", "$HOME", "$HOME/*"}

# Catastrophic patterns — denied outright (case-insensitive). `rm` is handled
# separately by _rm_is_catastrophic so that deleting a normal subdir is allowed.
_DANGEROUS = [
    (re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\b(mkfs|fdisk|wipefs)\b", re.I), "filesystem format"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/", re.I), "dd to a raw device"),
    (re.compile(r">\s*/dev/(sd|nvme|hd)", re.I), "overwrite a raw disk device"),
    (re.compile(r"\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(sh|bash|zsh)\b", re.I), "pipe remote script to a shell"),
    (re.compile(r"\bchmod\s+-[a-z]*R[a-z]*\s+777\s+/", re.I), "chmod 777 on root"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I), "power/reboot command"),
    (re.compile(r"\bdel\s+/[a-z]\s", re.I), "Windows recursive force delete"),
    (re.compile(r"\bformat\s+[a-z]:", re.I), "Windows drive format"),
    (re.compile(r"\bRemove-Item\b[^\n]*-Recurse[^\n]*-Force[^\n]*[\\/]\s*$", re.I), "PowerShell recursive force delete of a root path"),
]


def has_shell_operators(command: str) -> bool:
    return bool(_SHELL_OPERATORS.search(command))


def _rm_is_catastrophic(command: str) -> bool:
    """True if any `rm` in the command targets a root/home/cwd/glob-everything path.

    Deleting a normal subdir (`rm -rf build/`, `rm tmp.txt`) is NOT catastrophic.
    """
    for m in re.finditer(r"\brm\b([^;&|\n]*)", command, re.I):
        args = m.group(1).split()
        targets = [a for a in args if not a.startswith("-")]
        if any(t in _CATASTROPHIC_RM_TARGETS for t in targets):
            return True
    return False


def is_dangerous(command: str) -> tuple[bool, str]:
    if _rm_is_catastrophic(command):
        return True, "delete of a root/home/cwd/glob-everything path"
    for pat, reason in _DANGEROUS:
        if pat.search(command):
            return True, reason
    return False, ""


def _positional_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    return [t for t in tokens if not t.startswith("-")]


def matches_allow_rule(command: str, allow_prefixes: set[str] | None = None) -> bool:
    """True when the command's leading positional tokens equal an allow rule.

    Flags are ignored (they are not positional), so `git status -s` matches the
    `git status` rule while `git push` does not.
    """
    if allow_prefixes is None:
        allow_prefixes = SAFE_ALLOW_PREFIXES
    if has_shell_operators(command):
        return False
    positional = _positional_tokens(command)
    if not positional:
        return False
    for rule in allow_prefixes:
        rule_tokens = rule.split()
        n = len(rule_tokens)
        # An allow rule may itself contain a flag-looking token (e.g.
        # "git config --get"); compare against the raw tokens for those.
        if any(t.startswith("-") for t in rule_tokens):
            try:
                raw = shlex.split(command, posix=True)
            except ValueError:
                raw = command.split()
            if raw[:n] == rule_tokens:
                return True
            continue
        if positional[:n] == rule_tokens:
            return True
    return False


def classify(command: str, allow_prefixes: set[str] | None = None) -> tuple[str, str]:
    """Return (decision, reason): decision is 'deny' | 'allow' | 'ask'."""
    cmd = (command or "").strip()
    if not cmd:
        return "ask", ""
    dangerous, reason = is_dangerous(cmd)
    if dangerous:
        return "deny", reason
    if matches_allow_rule(cmd, allow_prefixes):
        return "allow", "known safe read-only command"
    return "ask", ""
