"""File history / rewind.

Before the agent runs a file-mutating tool (write_file / edit_file / fim_edit),
it snapshots the target file's current bytes. ``/rewind`` then restores the
last N changes — putting modified files back, and deleting files that did not
exist before. This is an undo for the agent's edits, independent of git.

Snapshots live under ``.lilbot/filehistory/`` (gitignored).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HistoryEntry:
    seq: int
    rel_path: str
    abs_path: str
    existed: bool          # did the file exist before this change?
    backup: str            # filename of the saved pre-change content (if existed)
    tool: str
    turn: int
    ts: float


class FileHistory:
    def __init__(self, state_dir: Path, workspace: Path) -> None:
        self.dir = Path(state_dir) / "filehistory"
        self.workspace = Path(workspace)
        self.journal_path = self.dir / "journal.jsonl"

    # -- recording --------------------------------------------------------

    def _next_seq(self) -> int:
        entries = self.list()
        return (entries[-1].seq + 1) if entries else 1

    def record(self, rel_path: str, tool: str, turn: int) -> None:
        """Snapshot the current content of rel_path before it is modified."""
        if not rel_path:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            abs_path = (self.workspace / rel_path).resolve()
            existed = abs_path.is_file()
            seq = self._next_seq()
            backup_name = ""
            if existed:
                backup_name = f"{seq}.bak"
                content = abs_path.read_bytes()
                (self.dir / backup_name).write_bytes(content)
            entry = HistoryEntry(
                seq=seq, rel_path=rel_path, abs_path=str(abs_path),
                existed=existed, backup=backup_name, tool=tool, turn=turn, ts=time.time(),
            )
            with self.journal_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")
        except OSError:
            pass  # history is best-effort

    # -- reading ----------------------------------------------------------

    def list(self) -> list[HistoryEntry]:
        if not self.journal_path.is_file():
            return []
        out: list[HistoryEntry] = []
        try:
            for line in self.journal_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                out.append(HistoryEntry(**d))
        except (OSError, json.JSONDecodeError, TypeError):
            return out
        return out

    # -- rewind -----------------------------------------------------------

    def rewind(self, steps: int = 1) -> list[str]:
        """Undo the last `steps` recorded changes, newest first.

        Returns human-readable lines describing what was restored. Consumed
        entries are removed from the journal so a second rewind goes further back.
        """
        entries = self.list()
        if not entries or steps <= 0:
            return []
        to_undo = entries[-steps:]
        remaining = entries[: len(entries) - len(to_undo)]
        results: list[str] = []
        # Undo newest-first so repeated edits to one file unwind correctly.
        for entry in reversed(to_undo):
            abs_path = Path(entry.abs_path)
            try:
                if entry.existed:
                    backup = self.dir / entry.backup
                    if backup.is_file():
                        abs_path.parent.mkdir(parents=True, exist_ok=True)
                        abs_path.write_bytes(backup.read_bytes())
                        results.append(f"restored {entry.rel_path}")
                    else:
                        results.append(f"skipped {entry.rel_path} (backup missing)")
                else:
                    if abs_path.is_file():
                        abs_path.unlink()
                    results.append(f"removed {entry.rel_path} (was newly created)")
                if entry.backup:
                    (self.dir / entry.backup).unlink(missing_ok=True)
            except OSError as exc:
                results.append(f"failed {entry.rel_path}: {exc}")
        # Rewrite the journal without the undone entries.
        try:
            with self.journal_path.open("w", encoding="utf-8") as f:
                for e in remaining:
                    f.write(json.dumps(e.__dict__, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return results
