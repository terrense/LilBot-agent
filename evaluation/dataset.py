"""Load declarative mock or live tasks and their text fixtures."""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from dataclasses import dataclass
from os import stat_result
from pathlib import Path


class DatasetError(ValueError):
    """The dataset or its fixture cannot be used safely."""


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    input: str
    setup: dict
    graders: tuple[dict, ...]
    timeout: float
    max_steps: int = 20
    category: str | None = None
    risk: str | None = None
    success_criteria: str | None = None


@dataclass(frozen=True)
class Dataset:
    version: str
    tasks: tuple[TaskSpec, ...]
    fixture_root: Path
    fixtures: dict[str, tuple[tuple[str, bytes], ...]]


_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{prefix}{suffix}" for prefix in ("com", "lpt") for suffix in "123456789¹²³"
}
_STATE_PATHS = {".lilbot", ".git", ".env"}
_INVALID_PATH_CHAR = re.compile(r'[<>:"|?*~\\\x00-\x1f]')
_GRADER_FIELDS = {
    "file_exists": {"path"},
    "file_not_exists": {"path"},
    "file_contains": {"path", "text"},
    "file_equals": {"path", "text"},
    "final_contains": {"text"},
    "tool_called": {"name"},
    "tool_succeeded": {"name"},
    "tool_failed": {"name"},
    "forbidden_tool_not_called": {"name"},
    "guardrail_observed": {"name"},
    "memory_contains": {"name", "text"},
    "run_completed": set(),
}


def safe_relative_path(value: object) -> str:
    """Require canonical relative syntax valid on Windows and POSIX."""
    if not isinstance(value, str) or not value or _INVALID_PATH_CHAR.search(value):
        raise DatasetError("expected a safe relative path with forward slashes")
    _string(value, "relative path")
    for part in value.split("/"):
        folded = part.casefold()
        stem = folded.split(".", 1)[0].rstrip()
        if (
            not part
            or part in {".", ".."}
            or part != part.strip()
            or part.endswith(".")
            or folded in _STATE_PATHS
            or stem in _RESERVED
        ):
            raise DatasetError("unsafe relative path component")
    return value


def _existing_stat(path: Path) -> stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise DatasetError("symbolic links and reparse points are not allowed")
    return info


def resolve_safe_path(root: Path, value: str) -> Path:
    """Return a lexical path below root after checking every existing component."""
    parts = safe_relative_path(value).split("/")
    current = root.absolute()
    for part in parts:
        info = _existing_stat(current)
        if info is not None and not stat.S_ISDIR(info.st_mode):
            raise DatasetError("relative path has a non-directory parent")
        current = current / part
    _existing_stat(current)
    return current


def _text_file_path(value: object) -> str:
    relative = safe_relative_path(value)
    if not relative.casefold().endswith(".txt"):
        raise DatasetError("fixture and tool files must use the .txt extension")
    return relative


def _fixture_name(value: object) -> str:
    name = safe_relative_path(value)
    if "/" in name:
        raise DatasetError("setup.fixture must be a single directory name")
    return name


def fixture_files(fixture_root: Path, fixture: str) -> tuple[tuple[str, bytes], ...]:
    """Snapshot a fixture directory without copying links, devices, or state."""
    root = resolve_safe_path(fixture_root, _fixture_name(fixture))
    info = _existing_stat(root)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise DatasetError(f"fixture directory does not exist: {fixture}")
    files = []
    directories = [root]
    while directories:
        directory = directories.pop()
        names = set()
        for entry in sorted(directory.iterdir()):
            relative = safe_relative_path(entry.relative_to(root).as_posix())
            name = entry.name.casefold()
            if name in names:
                raise DatasetError(f"fixture has case-insensitive path collision: {relative}")
            names.add(name)
            info = _existing_stat(entry)
            if info is None:
                raise DatasetError(f"fixture entry disappeared: {relative}")
            if stat.S_ISDIR(info.st_mode):
                directories.append(entry)
                continue
            _text_file_path(relative)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DatasetError(f"fixture must contain independent regular files: {relative}")
            content = entry.read_bytes()
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DatasetError(f"fixture must contain UTF-8 text: {relative}") from exc
            files.append((relative, content))
    return tuple(sorted(files))


def _object(value: object, required: set[str], optional: set[str], label: str) -> dict:
    if not isinstance(value, dict) or required - value.keys() or value.keys() - required - optional:
        raise DatasetError(f"invalid fields for {label}")
    return value


def _string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise DatasetError(f"{label} must be a string" + ("" if allow_empty else " with content"))
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DatasetError(f"{label} must be valid UTF-8 text") from exc
    return value


def _number(value: object, label: str) -> float:
    if type(value) not in {int, float}:
        raise DatasetError(f"{label} must be a finite number")
    try:
        numeric = float(value)
    except OverflowError as exc:
        raise DatasetError(f"{label} must be a finite number") from exc
    if not math.isfinite(numeric):
        raise DatasetError(f"{label} must be a finite number")
    return numeric


def _validate_script(value: object) -> None:
    if not isinstance(value, list) or not value:
        raise DatasetError("setup.script must be a non-empty list")
    for step in value:
        if not isinstance(step, dict):
            raise DatasetError("setup.script steps must be objects")
        if set(step) == {"tool", "args"}:
            name = _string(step["tool"], "script tool")
            if name == "read_file":
                args = _object(step["args"], {"path"}, set(), "read_file arguments")
                _text_file_path(args["path"])
            elif name == "write_file":
                args = _object(step["args"], {"path", "content"}, set(), "write_file arguments")
                _text_file_path(args["path"])
                _string(args["content"], "write_file content", allow_empty=True)
            elif name == "exec_shell":
                _object(step["args"], set(), set(), "unregistered exec_shell sentinel arguments")
            else:
                raise DatasetError("unsupported script tool")
        elif set(step) == {"final"}:
            _string(step["final"], "script final", allow_empty=True)
        elif set(step) == {"error"} and step["error"] == "provider_error":
            continue
        elif set(step) == {"sleep"} and 0 <= _number(step["sleep"], "script sleep") <= 60:
            continue
        else:
            raise DatasetError("invalid script operation")


def _validate_setup(value: object, mode: str) -> dict:
    required = {"script"} if mode == "mock" else set()
    optional = {"fixture", "writable_files"}
    if mode == "live":
        optional.update({"followup", "memory"})
    setup = _object(value, required, optional, "setup")
    if "fixture" in setup:
        _fixture_name(setup["fixture"])
    writable = setup.get("writable_files", [])
    if not isinstance(writable, list):
        raise DatasetError("setup.writable_files must be a list")
    names = [_text_file_path(path).casefold() for path in writable]
    if len(set(names)) != len(names):
        raise DatasetError("duplicate writable file path")
    if mode == "mock":
        _validate_script(setup["script"])
    else:
        followup = setup.get("followup", [])
        if not isinstance(followup, list) or len(followup) > 2:
            raise DatasetError("setup.followup must be a list of up to two user messages")
        for message in followup:
            _string(message, "follow-up input")
        if type(setup.get("memory", False)) is not bool:
            raise DatasetError("setup.memory must be a boolean")
    return setup


def _validate_graders(value: object) -> tuple[dict, ...]:
    if not isinstance(value, list) or not value:
        raise DatasetError("graders must be a non-empty list")
    for grader in value:
        if not isinstance(grader, dict):
            raise DatasetError("graders must contain objects")
        kind = _string(grader.get("type"), "grader type")
        if kind not in _GRADER_FIELDS:
            raise DatasetError("unsupported grader type")
        optional = {"kind"} if kind == "guardrail_observed" else set()
        _object(grader, {"type", *_GRADER_FIELDS[kind]}, optional, f"{kind} grader")
        if "path" in grader:
            safe_relative_path(grader["path"])
        if "text" in grader:
            _string(grader["text"], f"{kind} text", allow_empty=kind == "file_equals")
        if "name" in grader:
            _string(grader["name"], "grader tool name")
        if "kind" in grader:
            _string(grader["kind"], "guardrail notice kind")
    return tuple(value)


def _parse_task(value: object, mode: str) -> TaskSpec:
    required = {"task_id", "input", "setup", "graders", "timeout"}
    optional = {"max_steps"}
    if mode == "live":
        required.update({"category", "risk", "success_criteria", "max_steps"})
    task = _object(value, required, optional, "task")
    task_id = _string(task["task_id"], "task_id")
    if not _TASK_ID.fullmatch(task_id):
        raise DatasetError("task_id must be 1–80 ASCII letters, digits, dots, underscores or hyphens")
    user_input = _string(task["input"], "input")
    setup = _validate_setup(task["setup"], mode)
    graders = _validate_graders(task["graders"])
    timeout = _number(task["timeout"], "timeout")
    if timeout <= 0:
        raise DatasetError("timeout must be positive")
    max_steps = task.get("max_steps", 20)
    if type(max_steps) is not int or max_steps <= 0:
        raise DatasetError("max_steps must be a positive integer")
    if mode == "mock":
        return TaskSpec(task_id, user_input, setup, graders, timeout, max_steps)

    if timeout > 180 or max_steps > 12:
        raise DatasetError("live tasks require timeout <= 180 seconds and max_steps <= 12")
    category = _string(task["category"], "category")
    if not _TASK_ID.fullmatch(category):
        raise DatasetError("category must be an ASCII identifier")
    risk = _string(task["risk"], "risk")
    if risk not in {"low", "medium"}:
        raise DatasetError("live risk must be low or medium")
    success_criteria = _string(task["success_criteria"], "success criteria")
    if any(grader["type"] == "memory_contains" for grader in graders) and not setup.get("memory"):
        raise DatasetError("memory_contains requires setup.memory")
    return TaskSpec(task_id, user_input, setup, graders, timeout, max_steps, category, risk, success_criteria)


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DatasetError("duplicate JSON key")
        result[key] = value
    return result


def load_dataset(path: Path, *, mode: str = "mock") -> Dataset:
    """Validate a UTF-8 JSONL dataset and fingerprint its exact fixture inputs."""
    if mode not in {"mock", "live"}:
        raise DatasetError("dataset mode must be mock or live")
    path = Path(path)
    try:
        raw = path.read_bytes()
        source = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise DatasetError(f"could not read UTF-8 dataset: {path.name}") from exc

    tasks = []
    ids = set()
    fixtures = {}
    fixture_root = path.absolute().parent / "fixtures"
    for line_number, line in enumerate(source.split("\n"), 1):
        if not line.strip():
            continue
        try:
            task = _parse_task(json.loads(line, object_pairs_hook=_unique_keys), mode)
            if task.task_id in ids:
                raise DatasetError(f"duplicate task_id: {task.task_id}")
            fixture = task.setup.get("fixture")
            if fixture is not None and fixture not in fixtures:
                fixtures[fixture] = fixture_files(fixture_root, fixture)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path.name}:{line_number}: invalid JSON") from exc
        except (OSError, ValueError, RecursionError) as exc:
            raise DatasetError(f"{path.name}:{line_number}: {exc}") from exc
        ids.add(task.task_id)
        tasks.append(task)
    if not tasks:
        raise DatasetError("dataset contains no tasks")

    digest = hashlib.sha256(raw)
    for fixture, files in sorted(fixtures.items()):
        for relative, contents in files:
            # JSON frames the path and file digest unambiguously.
            frame = json.dumps([fixture, relative, hashlib.sha256(contents).hexdigest()])
            digest.update(frame.encode("ascii"))
    return Dataset(f"sha256:{digest.hexdigest()}", tuple(tasks), fixture_root, fixtures)
