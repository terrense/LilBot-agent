"""Redacted, append-only evidence from the runtime event iterator."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lilbot.security.env import is_secret_env_name, redact_known_secrets
from lilbot.security.secrets import PLACEHOLDER, redact_secrets


TOOL_RESULT_LIMIT = 2048
TEXT_LIMIT = 4096
_TRACE_FIELDS = {
    "tool_name", "tool_args", "tool_result", "status", "final_output",
    "tool_seq", "canonical_name", "elapsed_ms", "steps", "usage",
    "reason", "action", "attempt", "delay", "detail", "termination_reason", "agent_outcome",
    "turn_index", "failure_kind", "guardrails", "request_index", "model_name",
}
_PRIVATE_FIELDS = {"reasoning", "reasoning_content", "thinking", "chain_of_thought"}
_SECRET_FIELDS = {"authorization", "proxy_authorization", "cookie", "set_cookie", "auth", "credentials"}
_MASK_TAIL = re.compile(r"\[REDACTED\]…[^\s]{4}")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?P<key>[\w-]*(?:api[_-]?key|password|passwd|secret|token|cookie|authorization|credential)[\w-]*"
    r"[\"']?\s*[:=]\s*)(?!\[REDACTED)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|(?:(?:bearer|basic)\s+)?[^\s,;}\]]+)"
)


def _field_name(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).replace("-", "_").lower()


def sanitize(value: Any, *, known_secrets: tuple[str, ...] = ()) -> Any:
    """Remove known secrets, credential-like text and private reasoning fields."""
    if isinstance(value, str):
        value = redact_known_secrets(value)
        for secret in known_secrets:
            if secret:
                value = value.replace(secret, PLACEHOLDER)
        value = _SECRET_ASSIGNMENT.sub(lambda match: match["key"] + PLACEHOLDER, value)
        return _MASK_TAIL.sub(PLACEHOLDER, redact_secrets(value))
    if isinstance(value, dict):
        result = {}
        for key, entry in value.items():
            field = _field_name(key)
            if field in _PRIVATE_FIELDS:
                continue
            safe_key = sanitize(key, known_secrets=known_secrets)
            if field in _SECRET_FIELDS or is_secret_env_name(field):
                result[safe_key] = PLACEHOLDER
            else:
                result[safe_key] = sanitize(entry, known_secrets=known_secrets)
        return result
    if isinstance(value, (tuple, list)):
        return [sanitize(entry, known_secrets=known_secrets) for entry in value]
    return value


def _limit(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    suffix = "…[truncated]"
    return value[:limit - len(suffix)] + suffix


class TraceWriter:
    def __init__(self, path: Path, trial_id: str, known_secrets: tuple[str, ...] = (), *, staging: bool = False):
        self.path = path
        self.trial_id = trial_id
        self.known_secrets = known_secrets
        self.staging = staging

    def __enter__(self) -> TraceWriter:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("x", encoding="utf-8", errors="backslashreplace", newline="\n")
        self._seq = 0
        return self

    def __exit__(self, *exc: Any) -> None:
        self._handle.close()

    def record(
        self,
        event_type: str,
        *,
        tool_name: str | None = None,
        tool_args: dict | None = None,
        tool_result: str | None = None,
        status: str | None = None,
        final_output: str | None = None,
        **metadata: Any,
    ) -> None:
        self._write({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "tool_result": tool_result,
            "status": status,
            "final_output": final_output,
            **{key: value for key, value in metadata.items() if key in _TRACE_FIELDS},
        })

    def _write(self, event: dict) -> None:
        payload = {key: value for key, value in event.items() if key in _TRACE_FIELDS}
        # Usage contains numeric counters. Keep their field names and numeric values.
        usage = payload.pop("usage", None)
        safe = sanitize(payload, known_secrets=self.known_secrets)
        if "usage" in event:
            safe["usage"] = (
                {key: value for key, value in usage.items() if type(value) is int}
                if isinstance(usage, dict) else None
            )
        # A disposable worker trace defers clipping until the parent can redact
        # host-known values. Otherwise a clipped secret prefix could survive.
        if not self.staging:
            safe["tool_result"] = _limit(safe.get("tool_result"), TOOL_RESULT_LIMIT)
            safe["final_output"] = _limit(safe.get("final_output"), TEXT_LIMIT)
            if "detail" in safe:
                safe["detail"] = _limit(safe["detail"], TEXT_LIMIT)
        self._seq += 1
        record = {
            "schema_version": 1,
            "trial_id": self.trial_id,
            "seq": self._seq,
            "timestamp": event["timestamp"],
            "event_type": event["event_type"],
            **safe,
        }
        self._handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        self._handle.flush()


def read_trace(path: Path) -> list[dict]:
    """Read complete JSONL records, tolerating a worker killed mid-write."""
    if not path.exists():
        return []
    with path.open("rb") as handle:
        return [json.loads(line) for line in handle if line.endswith(b"\n")]


def publish_trace(
    source: Path,
    destination: Path,
    trial_id: str,
    *,
    status: str,
    final_output: str | None = None,
    detail: str | None = None,
) -> None:
    """Apply host-side secret redaction and record the supervised trial verdict."""
    events = read_trace(source)
    with TraceWriter(destination, trial_id) as trace:
        for event in events:
            trace._write(event)
        trace.record("trial_finished", status=status, final_output=final_output, detail=detail)
