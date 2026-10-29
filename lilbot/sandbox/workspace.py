from __future__ import annotations

import fnmatch
import locale
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..security.env import scrubbed_env
from .walk import glob_matches_dirs_only, glob_to_regex, iter_files, read_texts, walk


class SandboxError(RuntimeError):
    pass


@dataclass
class CommandResult:
    ok: bool
    output: str
    returncode: int


def _decode_process_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    encodings = ["utf-8", locale.getpreferredencoding(False), "gbk"]
    for encoding in dict.fromkeys(encodings):
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")


class Sandbox:
    """Workspace-scoped filesystem and shell boundary."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def resolve(self, path: str | Path = ".") -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise SandboxError(f"path escapes workspace: {path}")
        return resolved

    def relative(self, path: str | Path) -> str:
        return str(self.resolve(path).relative_to(self.root))

    def list_dir(self, path: str = ".", max_depth: int = 1) -> list[str]:
        base = self.resolve(path)
        if not base.exists():
            raise SandboxError(f"path does not exist: {path}")
        if not base.is_dir():
            raise SandboxError(f"path is not a directory: {path}")
        max_depth = max(0, min(max_depth, 8))
        return [
            entry.rel + ("/" if entry.is_dir else "")
            for entry in walk(base, root=self.root, max_depth=max_depth + 1, include_dirs=True)
        ]

    def glob(self, pattern: str, path: str = ".") -> list[str]:
        """Like ``base.rglob(pattern)`` minus noisy and .gitignore'd trees."""
        base = self.resolve(path)
        regex = glob_to_regex(pattern)
        dirs_only = glob_matches_dirs_only(pattern)
        matches = [
            entry.path.relative_to(self.root).as_posix()
            for entry in walk(base, root=self.root, include_dirs=True, respect_gitignore=True)
            if regex.match(entry.rel) and (entry.is_dir or not dirs_only)
        ]
        return sorted(matches)

    def grep(
        self,
        pattern: str,
        path: str = ".",
        glob_pattern: str | None = None,
        max_results: int = 80,
    ) -> list[str]:
        base = self.resolve(path)
        rows: list[str] = []
        files = [base] if base.is_file() else (
            entry.path for entry in iter_files(base, root=self.root, respect_gitignore=True)
        )
        if glob_pattern:
            files = (f for f in files if fnmatch.fnmatch(f.name, glob_pattern))
        for file_path, text in read_texts(files):
            if text is None:
                continue
            lines = text.splitlines()
            for idx, line in enumerate(lines, 1):
                if pattern.lower() in line.lower():
                    rel = file_path.relative_to(self.root).as_posix()
                    rows.append(f"{rel}:{idx}: {line.strip()}")
                    if len(rows) >= max_results:
                        return rows
        return rows

    def run(self, command: str, timeout: int = 30) -> CommandResult:
        timeout = max(1, min(int(timeout), 120))
        if os.name == "nt":
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
            shell = False
        else:
            argv = command
            shell = True
        try:
            proc = subprocess.run(
                argv,
                cwd=self.root,
                env=scrubbed_env(),
                shell=shell,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            out = _decode_process_output(exc.stdout) + _decode_process_output(exc.stderr)
            return CommandResult(False, out + f"\nTimed out after {timeout}s", 124)
        output = _decode_process_output(proc.stdout) + _decode_process_output(proc.stderr)
        return CommandResult(proc.returncode == 0, output.strip(), proc.returncode)
