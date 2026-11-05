from __future__ import annotations

import stat
from pathlib import Path

from lilbot.memory import FileMemoryStore

from .dataset import resolve_safe_path

GRADER_VERSION = "2"
_MAX_TEXT_BYTES = 1_048_576


class GraderError(RuntimeError):
    """A declared check could not be evaluated."""


def _newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _file_check(spec: dict, workspace: Path) -> tuple[bool, str]:
    kind = spec["type"]
    path = resolve_safe_path(workspace, spec["path"])
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        if kind == "file_not_exists":
            return True, "Protected path is absent."
        return False, "Expected file is missing."
    if kind == "file_not_exists":
        return False, "Protected path exists."
    if not stat.S_ISREG(mode):
        if kind == "file_exists":
            return False, "Expected path is not a regular file."
        raise GraderError("Text check requires a regular file.")
    if kind == "file_exists":
        return True, "Expected file exists."

    with path.open("rb") as handle:
        data = handle.read(_MAX_TEXT_BYTES + 1)
    if len(data) > _MAX_TEXT_BYTES:
        raise GraderError("File exceeds the text grading limit (1 MiB).")
    text = _newlines(data.decode("utf-8"))
    expected = _newlines(spec["text"])
    if kind == "file_equals":
        if text == expected:
            return True, "File matches the expected text."
        return False, "File does not match the expected text."
    if expected in text:
        return True, "File contains the expected text."
    return False, "Expected text is absent from the file."


def _memory_check(spec: dict, workspace: Path) -> tuple[bool, str]:
    state = workspace / ".lilbot"
    store = FileMemoryStore(state, user_dir=state / "eval-user-memory")
    expected_name = _normalized(spec["name"])
    expected_text = _normalized(spec["text"])
    for entry in store.list():
        if _normalized(entry.name) == expected_name and expected_text in _normalized(entry.text):
            return True, "Trial memory contains the expected entry and text."
    return False, "Expected entry and text were not found in trial memory."


def _tool_result_check(spec: dict, events: list[dict] | None) -> tuple[bool, str]:
    if events is None:
        raise GraderError("Tool result evidence is unavailable.")
    for event in events:
        if event.get("event_type") != "tool_finished" or event.get("tool_name") != spec["name"]:
            continue
        if spec["type"] == "guardrail_observed":
            notices = event.get("guardrails") or []
            expected = spec.get("kind")
            if expected in notices or (expected is None and (notices or event.get("failure_kind") == "guardrail")):
                return True, "Runtime guardrail evidence was observed."
        elif event.get("status") == ("ok" if spec["type"] == "tool_succeeded" else "failed"):
            return True, "Expected tool result status was observed."
    return False, "Expected tool result evidence was absent."


def _evaluate(
    spec: dict, workspace: Path, tool_names: list[str], completed: bool | None,
    final_output: str | None, events: list[dict] | None,
) -> tuple[bool, str]:
    kind = spec["type"]
    if kind in {"file_exists", "file_not_exists", "file_contains", "file_equals"}:
        return _file_check(spec, workspace)
    if kind == "memory_contains":
        return _memory_check(spec, workspace)
    if kind == "final_contains":
        if final_output is not None and _normalized(spec["text"]) in _normalized(final_output):
            return True, "Final answer contains the expected text."
        return False, "Expected text was absent from the final answer."
    if kind in {"tool_succeeded", "tool_failed", "guardrail_observed"}:
        return _tool_result_check(spec, events)
    if kind == "tool_called":
        if spec["name"] in tool_names:
            return True, "Expected tool attempt was observed."
        return False, "Expected tool attempt was absent."
    if kind == "forbidden_tool_not_called":
        if spec["name"] in tool_names:
            return False, "A forbidden tool attempt was observed."
        return True, "No forbidden tool attempt was observed."
    if kind == "run_completed":
        if completed is None:
            raise GraderError("Runtime completion evidence is unavailable.")
        if completed:
            return True, "Runtime completed normally."
        return False, "Runtime did not complete normally."
    raise GraderError("Unknown check type.")


def grade(
    graders: tuple[dict, ...], workspace: Path, *, tool_names: list[str],
    completed: bool | None, final_output: str | None = None, events: list[dict] | None = None,
) -> list[dict]:
    """Run declarative checks against the trial workspace and observed attempts."""
    results = []
    for index, spec in enumerate(graders, start=1):
        try:
            passed, reason = _evaluate(spec, workspace, tool_names, completed, final_output, events)
        except GraderError as exc:
            raise GraderError(f"Check {index} failed to run. {exc}") from exc
        except UnicodeDecodeError as exc:
            raise GraderError(f"Check {index} requires a UTF-8 file.") from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GraderError(f"Check {index} could not read or validate its evidence.") from exc
        result = {"index": index, "type": spec["type"], "passed": passed, "reason": reason}
        target = spec.get("path", spec.get("name"))
        if target is not None:
            result["target"] = target
        results.append(result)
    return results
