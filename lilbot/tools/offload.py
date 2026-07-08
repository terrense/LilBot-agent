"""Large tool-result offloading.

Instead of hard-truncating a big tool result and losing the tail forever,
LilBot writes the full output to ``.lilbot/session/tool-results/<id>.txt`` and
returns a bounded preview plus a pointer. The model can recover the full text
on demand with the ``retrieve_tool_result`` / ``handle_read`` tools.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

# Results at or below this many characters are returned inline unchanged.
INLINE_LIMIT = 16_000
# How much of the head we keep in the inline preview when offloading.
PREVIEW_CHARS = 2_000

PERSISTED_TAG = "<persisted-output>"
SESSION_SUBDIR = ("session", "tool-results")


def session_dir(state_dir: Path | None) -> Path | None:
    """Resolve (and create) the tool-results directory under .lilbot state."""
    if state_dir is None:
        return None
    try:
        d = Path(state_dir).joinpath(*SESSION_SUBDIR)
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None


def persist_tool_result(content: str, target_dir: Path) -> Path:
    """Write the full content to a fresh file and return its path."""
    file_path = target_dir / f"{uuid4().hex}.txt"
    try:
        with open(file_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(content)
    except OSError:
        return file_path
    return file_path


def make_preview(content: str, file_path: Path) -> str:
    size_kb = max(1, len(content.encode("utf-8")) // 1024)
    preview = content[:PREVIEW_CHARS]
    return (
        f"{PERSISTED_TAG}\n"
        f"Output was large ({size_kb}KB); the full content is saved to:\n"
        f"{file_path}\n\n"
        f"To read it, call retrieve_tool_result with path=\"{file_path}\" "
        f"(supports offset/limit), or handle_read for a bounded slice.\n\n"
        f"Preview (first {PREVIEW_CHARS} chars):\n"
        f"{preview}\n"
        f"</persisted-output>"
    )


def maybe_offload(output: str, state_dir: Path | None, limit: int | None = None) -> tuple[str, dict]:
    """Return (possibly-replaced output, extra metadata).

    Small results pass through untouched. Large results are written to disk and
    replaced by a preview pointer. If no session dir is available (e.g. no
    config), fall back to a plain truncation so behaviour stays bounded.

    ``limit`` is the per-tool cap (CC's maxResultSizeChars):
      * None or 0 -> use the global default INLINE_LIMIT
      * negative  -> never offload (CC's Infinity; e.g. read_file)
      * positive  -> this tool's own threshold
    """
    if limit is not None and limit < 0:
        return output, {}  # opt-out: this tool's output is never persisted
    effective = INLINE_LIMIT if not limit else limit
    if len(output) <= effective:
        return output, {}
    target = session_dir(state_dir)
    if target is None:
        return output[:effective] + "\n... truncated ...", {"truncated": True}
    path = persist_tool_result(output, target)
    return make_preview(output, path), {
        "persisted": True,
        "persisted_path": str(path),
        "original_chars": len(output),
    }


def is_within_session(path: Path, state_dir: Path | None) -> bool:
    """True when path lives inside the managed tool-results directory."""
    target = session_dir(state_dir)
    if target is None:
        return False
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(target))
        return True
    except (ValueError, OSError):
        return False
