from __future__ import annotations

import fnmatch
import locale
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


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
        rows: list[str] = []
        max_depth = max(0, min(max_depth, 8))
        for item in sorted(base.rglob("*") if max_depth else base.iterdir()):
            rel = item.relative_to(base)
            depth = len(rel.parts)
            if depth > max_depth + 1:
                continue
            suffix = "/" if item.is_dir() else ""
            rows.append(f"{rel.as_posix()}{suffix}")
        return rows

    def glob(self, pattern: str, path: str = ".") -> list[str]:
        base = self.resolve(path)
        matches = [p.relative_to(self.root).as_posix() for p in base.rglob(pattern)]
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
        for file_path in base.rglob("*"):
            if not file_path.is_file():
                continue
            if glob_pattern and not fnmatch.fnmatch(file_path.name, glob_pattern):
                continue
            try:
                lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
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
                shell=shell,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            out = _decode_process_output(exc.stdout) + _decode_process_output(exc.stderr)
            return CommandResult(False, out + f"\nTimed out after {timeout}s", 124)
        output = _decode_process_output(proc.stdout) + _decode_process_output(proc.stderr)
        return CommandResult(proc.returncode == 0, output.strip(), proc.returncode)
