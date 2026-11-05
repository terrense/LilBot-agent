"""Per-turn safeguards against repeated failed or unproductive tool calls."""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any


SAME_CALL_WARNING_AFTER = 2
SAME_CALL_BLOCK_AFTER = 5
SAME_TOOL_WARNING_AFTER = 3


@dataclass(frozen=True)
class GuardrailNotice:
    kind: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _signature(tool_name: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(f"{tool_name}\0{canonical}".encode("utf-8")).hexdigest()


class ToolCallGuardrailController:
    """Track tool failures and repeated read-only results for one agent turn.

    The controller is thread-safe because consecutive read-only calls may run
    in the agent's parallel tool executor. It warns first; only an identical
    call that has already failed four times is blocked on its fifth attempt.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._signature_failures: dict[str, int] = {}
        self._tool_failures: dict[str, int] = {}
        self._read_outputs: dict[str, tuple[str, int]] = {}

    def reset(self) -> None:
        with self._lock:
            self._signature_failures.clear()
            self._tool_failures.clear()
            self._read_outputs.clear()

    def before_call(self, tool_name: str, arguments: dict[str, Any]) -> GuardrailNotice | None:
        signature = _signature(tool_name, arguments)
        with self._lock:
            failures = self._signature_failures.get(signature, 0)
        if failures < SAME_CALL_BLOCK_AFTER - 1:
            return None
        return GuardrailNotice(
            "repeated_failure_block",
            (
                f"Guardrail blocked {tool_name}: this exact tool call has already failed "
                f"{failures} times. Change the arguments or use a different approach."
            ),
            {"tool": tool_name, "signature": signature, "failures": failures},
        )

    def after_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        ok: bool,
        output: str,
        read_only: bool,
    ) -> list[GuardrailNotice]:
        signature = _signature(tool_name, arguments)
        notices: list[GuardrailNotice] = []
        with self._lock:
            if not ok:
                signature_failures = self._signature_failures.get(signature, 0) + 1
                tool_failures = self._tool_failures.get(tool_name, 0) + 1
                self._signature_failures[signature] = signature_failures
                self._tool_failures[tool_name] = tool_failures
                if signature_failures == SAME_CALL_WARNING_AFTER:
                    notices.append(GuardrailNotice(
                        "repeated_failure_warning",
                        "This exact call has failed twice. Inspect the error and change the approach before retrying.",
                        {"tool": tool_name, "signature": signature, "failures": signature_failures},
                    ))
                if tool_failures == SAME_TOOL_WARNING_AFTER:
                    notices.append(GuardrailNotice(
                        "tool_failure_warning",
                        f"{tool_name} has failed three times this turn. Use a different strategy or ask for help.",
                        {"tool": tool_name, "failures": tool_failures},
                    ))
                self._read_outputs.pop(tool_name, None)
                return notices

            self._signature_failures.pop(signature, None)
            if not read_only:
                self._read_outputs.pop(tool_name, None)
                return notices

            output_hash = hashlib.sha256((output or "").encode("utf-8", errors="replace")).hexdigest()
            previous = self._read_outputs.get(tool_name)
            repeats = previous[1] + 1 if previous and previous[0] == output_hash else 1
            self._read_outputs[tool_name] = (output_hash, repeats)
            if repeats == 2:
                notices.append(GuardrailNotice(
                    "read_only_no_progress",
                    "This read-only call returned the same result again. No new information was found; change the query or proceed with the current evidence.",
                    {"tool": tool_name, "signature": signature, "repeats": repeats},
                ))
        return notices
