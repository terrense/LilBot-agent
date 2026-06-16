from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CONTROL_OPERATORS = {";", "|", "&&", "||"}
REDIRECTION_OPERATORS = {">", ">>", "1>", "1>>", "2>", "2>>", "3>", "3>>", "4>", "4>>", "5>", "5>>", "6>", "6>>", "*>", "*>>"}
DESTRUCTIVE_COMMANDS = {
    "remove-item",
    "rm",
    "del",
    "erase",
    "rmdir",
    "rd",
    "move-item",
    "mv",
    "rename-item",
    "ren",
}
DELETE_COMMANDS = {"remove-item", "rm", "del", "erase", "rmdir", "rd"}
MOVE_COMMANDS = {"move-item", "mv", "rename-item", "ren"}
SUBPROCESS_COMMANDS = {
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "cmd",
    "cmd.exe",
    "bash",
    "bash.exe",
    "wsl",
    "wsl.exe",
    "start-process",
    "invoke-expression",
    "iex",
    "invoke-command",
}
BACKGROUND_COMMANDS = {"start-job", "start-threadjob", "start-process"}
PATH_PARAMETER_NAMES = {"path", "literalpath", "destination", "targetpath"}
PATH_PARAMETER_ALIASES = {"pspath": "path"}
NON_PATH_PARAMETER_VALUE_NAMES = {
    "erroraction",
    "warningaction",
    "informationaction",
    "verbose",
    "debug",
    "confirm",
    "whatif",
    "filter",
    "include",
    "exclude",
    "name",
    "newname",
}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass
class SafetyFinding:
    rule: str
    severity: str
    message: str
    token: str | None = None
    segment: int | None = None
    blocked: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "blocked": self.blocked,
        }
        if self.token is not None:
            data["token"] = self.token
        if self.segment is not None:
            data["segment"] = self.segment
        if self.metadata:
            data["metadata"] = self.metadata
        return data


def analyze_powershell_command(command: str, workspace_root: Path, *, background: bool = False) -> dict[str, Any]:
    root = workspace_root.resolve()
    tokens = _tokenize(command)
    segments = _segments(tokens)
    findings: list[SafetyFinding] = []
    targets: list[dict[str, Any]] = []

    for idx, token in enumerate(tokens):
        normalized = _normalize_command(token)
        if token in CONTROL_OPERATORS:
            findings.append(SafetyFinding("command_separator", "medium", f"Command uses control operator {token}.", token, None))
        if _is_redirection(token):
            findings.append(SafetyFinding("redirection", "medium", f"Command redirects output with {token}.", token, None))
            if idx + 1 < len(tokens):
                target = _path_info(tokens[idx + 1], root)
                target["kind"] = "redirection"
                targets.append(target)
                if target.get("inside_workspace") is False:
                    findings.append(SafetyFinding(
                        "redirection_outside_workspace",
                        "high",
                        "Redirection target resolves outside the workspace.",
                        tokens[idx + 1],
                        None,
                        True,
                        target,
                    ))
        if normalized in {"-encodedcommand", "-enc", "/encodedcommand"}:
            findings.append(SafetyFinding(
                "encoded_command",
                "critical",
                "Encoded PowerShell commands are not inspectable enough to run safely.",
                token,
                None,
                True,
            ))
        if token == "&":
            findings.append(SafetyFinding("background_operator", "medium", "Command uses PowerShell background operator &.", token))
        if normalized in SUBPROCESS_COMMANDS:
            findings.append(SafetyFinding(
                "subprocess_boundary",
                "high" if normalized in {"invoke-expression", "iex", "invoke-command"} else "medium",
                f"Command crosses a subprocess boundary through {token}.",
                token,
                None,
                normalized in {"invoke-expression", "iex"},
            ))
        if normalized in BACKGROUND_COMMANDS:
            findings.append(SafetyFinding("background_launch", "medium", f"Command can launch background work through {token}.", token))

    if background:
        findings.append(SafetyFinding("background_launch", "medium", "Tool requested background execution."))

    for index, segment in enumerate(segments):
        _analyze_destructive_segment(segment, index, root, findings, targets)

    risk_level = _risk_level(findings)
    blocked = any(item.blocked for item in findings)
    return {
        "shell": "powershell",
        "risk_level": risk_level,
        "blocked": blocked,
        "tokens": tokens,
        "segments": [{"index": idx, "tokens": segment} for idx, segment in enumerate(segments)],
        "findings": [item.as_dict() for item in findings],
        "targets": targets,
        "summary": _summary(risk_level, blocked, findings),
    }


def _tokenize(command: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == "`" and index + 1 < len(command):
                current.append(command[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            else:
                current.append(char)
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "#":
            break
        two = command[index : index + 2]
        if two in {"&&", "||", ">>", "*>>"}:
            _flush_token(tokens, current)
            tokens.append(two)
            index += 2
            continue
        if char in {";", "|", ">"}:
            _flush_token(tokens, current)
            tokens.append(char)
            index += 1
            continue
        if char == "&":
            _flush_token(tokens, current)
            tokens.append(char)
            index += 1
            continue
        if char.isspace():
            _flush_token(tokens, current)
            index += 1
            continue
        current.append(char)
        index += 1
    _flush_token(tokens, current)
    return tokens


def _flush_token(tokens: list[str], current: list[str]) -> None:
    if current:
        tokens.append("".join(current))
        current.clear()


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in CONTROL_OPERATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _analyze_destructive_segment(
    segment: list[str],
    index: int,
    root: Path,
    findings: list[SafetyFinding],
    targets: list[dict[str, Any]],
) -> None:
    for command_index, token in enumerate(segment):
        command_name = _normalize_command(token)
        if command_name not in DESTRUCTIVE_COMMANDS:
            continue
        dry_run = any(_normalize_command(item) == "-whatif" for item in segment)
        severity = "medium" if dry_run else "high"
        findings.append(SafetyFinding(
            "destructive_command",
            severity,
            f"Command invokes destructive PowerShell verb or alias {token}.",
            token,
            index,
        ))
        if dry_run:
            continue
        target_tokens = _destructive_target_tokens(segment[command_index + 1 :])
        if not target_tokens:
            findings.append(SafetyFinding(
                "destructive_without_target",
                "high",
                "Destructive command target could not be identified.",
                token,
                index,
                True,
            ))
            continue
        recursive = any(_normalize_command(item) == "-recurse" for item in segment)
        for target_token in target_tokens:
            info = _path_info(target_token, root)
            info["kind"] = "delete" if command_name in DELETE_COMMANDS else "move"
            info["recursive"] = recursive
            targets.append(info)
            if info.get("resolvable") is False:
                findings.append(SafetyFinding(
                    "unresolved_destructive_target",
                    "high",
                    "Destructive command target is dynamic or otherwise not safely resolvable.",
                    target_token,
                    index,
                    True,
                    info,
                ))
            elif info.get("inside_workspace") is False:
                findings.append(SafetyFinding(
                    "path_outside_workspace",
                    "critical",
                    "Destructive command target resolves outside the workspace.",
                    target_token,
                    index,
                    True,
                    info,
                ))
            elif recursive and info.get("is_workspace_root"):
                findings.append(SafetyFinding(
                    "workspace_root_destructive_target",
                    "critical",
                    "Recursive destructive command targets the workspace root.",
                    target_token,
                    index,
                    True,
                    info,
                ))


def _destructive_target_tokens(tokens: list[str]) -> list[str]:
    explicit: list[str] = []
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        normalized = _normalize_parameter(token)
        if _is_redirection(token):
            break
        if normalized in PATH_PARAMETER_NAMES or PATH_PARAMETER_ALIASES.get(normalized) in PATH_PARAMETER_NAMES:
            if index + 1 < len(tokens):
                explicit.append(tokens[index + 1])
                index += 2
                continue
        if token.startswith("-"):
            if normalized in NON_PATH_PARAMETER_VALUE_NAMES and index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
                index += 2
            else:
                index += 1
            continue
        positional.append(token)
        index += 1
    return explicit or positional[:2]


def _path_info(token: str, root: Path) -> dict[str, Any]:
    raw = str(token or "").strip()
    dynamic = any(marker in raw for marker in ("$", "$(", "%", "{", "}"))
    wildcard = any(char in raw for char in ("*", "?"))
    if not raw or dynamic:
        return {"raw": raw, "resolvable": False, "inside_workspace": None, "has_wildcard": wildcard}
    path_text = raw
    if wildcard:
        path_text = _wildcard_base(raw)
    try:
        candidate = Path(path_text).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return {"raw": raw, "resolvable": False, "inside_workspace": None, "has_wildcard": wildcard}
    inside = resolved == root or root in resolved.parents
    return {
        "raw": raw,
        "resolved": str(resolved),
        "resolvable": True,
        "inside_workspace": inside,
        "is_workspace_root": resolved == root,
        "has_wildcard": wildcard,
    }


def _wildcard_base(path_text: str) -> str:
    parts = path_text.replace("\\", "/").split("/")
    base_parts = []
    for part in parts:
        if "*" in part or "?" in part:
            break
        base_parts.append(part)
    return "/".join(base_parts) or "."


def _is_redirection(token: str) -> bool:
    return token in REDIRECTION_OPERATORS or token.endswith(">") or token.endswith(">>")


def _normalize_command(token: str) -> str:
    return str(token or "").strip().lower()


def _normalize_parameter(token: str) -> str:
    return _normalize_command(token).lstrip("-/")


def _risk_level(findings: list[SafetyFinding]) -> str:
    level = "low"
    for finding in findings:
        if RISK_ORDER.get(finding.severity, 0) > RISK_ORDER[level]:
            level = finding.severity
    return level


def _summary(risk_level: str, blocked: bool, findings: list[SafetyFinding]) -> str:
    if not findings:
        return "PowerShell safety: low risk; no notable shell hazards detected."
    rules = []
    for finding in findings:
        if finding.rule not in rules:
            rules.append(finding.rule)
    prefix = "PowerShell safety blocked command" if blocked else "PowerShell safety"
    return f"{prefix}: {risk_level} risk; " + ", ".join(rules[:6])
