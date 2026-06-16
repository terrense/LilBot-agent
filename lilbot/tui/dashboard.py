from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from datetime import datetime
from time import monotonic
from typing import Iterable

from ..core.agent import Agent
from ..cli import handle_slash, run_prompt, slash_commands_matching
from ..core.events import TextDelta, ToolFinished, ToolStarted, TurnFinished
from ..tools import ToolContext, ToolRegistry

try:
    from prompt_toolkit import Application
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.cursor_shapes import CursorShape
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, HSplit, Layout, VSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.layout.margins import Margin
    from prompt_toolkit.lexers import Lexer
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
    from prompt_toolkit.styles import Style
    from prompt_toolkit.widgets import Box, Frame, TextArea
except ImportError:  # pragma: no cover - optional dependency fallback
    Application = None


SYSTEM_MAP = """\
memory core
  -> skill deck
  -> subagent bay
  -> mcp dock
        |
        v
tool registry
  -> permission gate
  -> workspace sandbox
"""

WAVE_FRAMES = [
    "▁▂▃▄▅▆▇█▇▆▅▄▃▂",
    "▂▃▄▅▆▇█▇▆▅▄▃▂▁",
    "▃▄▅▆▇█▇▆▅▄▃▂▁▂",
    "▄▅▆▇█▇▆▅▄▃▂▁▂▃",
    "▅▆▇█▇▆▅▄▃▂▁▂▃▄",
    "▆▇█▇▆▅▄▃▂▁▂▃▄▅",
    "▇█▇▆▅▄▃▂▁▂▃▄▅▆",
    "█▇▆▅▄▃▂▁▂▃▄▅▆▇",
]

QUIT_CONFIRM_SECONDS = 1.6
TRACE_MAX_LINES = 900
TRACE_OUTPUT_SMALL_CHARS = 1800
TRACE_OUTPUT_MAX_LINE_CHARS = 180
TRACE_TOOL_LINE_LIMITS = {
    "bash": 10,
    "glob": 10,
    "grep": 18,
    "web_search": 14,
    "fetch_url": 16,
    "web_fetch": 16,
    "list_dir": 10,
    "read_file": 10,
    "write_file": 36,
    "edit_file": 36,
    "mcp_call": 14,
}
NOISY_PATH_MARKERS = (
    ".git/objects/",
    ".git\\objects\\",
    "__pycache__/",
    "__pycache__\\",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
)

LILBOT_AGENT_LOGO_ROWS = [
    "██╗     ██╗██╗     ██████╗  ██████╗ ████████╗- █████╗  ██████╗ ███████╗███╗   ██╗████████╗",
    "██╗     ██╗██╗     ██████╗  ██████╗ ████████╗- █████╗  ██████╗ ███████╗███╗   ██╗████████╗",
    "██║     ██║██║     ██╔══██╗██╔═══██╗╚══██╔══╝-██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝",
    "██║     ██║██║     ██████╔╝██║   ██║   ██║   -███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║",
    "██║     ██║██║     ██████╔╝██║   ██║   ██║   -███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║",
    "██║     ██║██║     ██╔══██╗██║   ██║   ██║   -██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║",
    "███████╗██║███████╗██████╔╝╚██████╔╝   ██║   -██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║",
    "╚══════╝╚═╝╚══════╝╚═════╝  ╚═════╝    ╚═╝   -╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝",
]
LILBOT_AGENT_LOGO_COMPACT_ROWS = [
    "╦  ╦╦  ╔╗ ╔═╗╔╦╗ ─ ╔═╗╔═╗╔═╗╔╗╔╔╦╗",
    "║  ║║  ╠╩╗║ ║ ║  ─ ╠═╣║ ╦║╣ ║║║ ║ ",
    "╩═╝╩╩═╝╚═╝╚═╝ ╩  ─ ╩ ╩╚═╝╚═╝╝╚╝ ╩ ",
]
LILBOT_LOGO_STYLES = [
    "class:logo.hot",
    "class:logo.hot",
    "class:logo.mid",
    "class:logo.mid",
    "class:logo.cool",
    "class:logo.shadow",
    "class:logo.mid",
    "class:logo.hot",
    "class:logo.hot",
    "class:logo.mid",
    "class:logo.mid",
    "class:logo.cool",
    "class:logo.shadow",
]

WELCOME_TRACE_ROWS = [
    "██╗    ██╗███████╗██╗      ██████╗ ██████╗ ███╗   ███╗███████╗",
    "██║    ██║██╔════╝██║     ██╔════╝██╔═══██╗████╗ ████║██╔════╝",
    "██║ █╗ ██║█████╗  ██║     ██║     ██║   ██║██╔████╔██║█████╗  ",
    "██║███╗██║██╔══╝  ██║     ██║     ██║   ██║██║╚██╔╝██║██╔══╝  ",
    "╚███╔███╔╝███████╗███████╗╚██████╗╚██████╔╝██║ ╚═╝ ██║███████╗",
    " ╚══╝╚══╝ ╚══════╝╚══════╝ ╚═════╝ ╚═════╝ ╚═╝     ╚═╝╚══════╝",
]


STYLE = Style.from_dict(
    {
        "root": "bg:#12091f #f8d8ec bold",
        "topbar": "bg:#211234 #ffd6ea bold",
        "topbar.dim": "bg:#211234 #bda4df",
        "frame": "bg:#170d29 #f8d8ec bold",
        "frame.border": "#d8b4fe",
        "frame.label": "#f9a8d4 bold",
        "logo": "#f9a8d4 bold",
        "logo.hot": "#f9a8d4 bold",
        "logo.mid": "#c4b5fd bold",
        "logo.cool": "#93c5fd bold",
        "logo.shadow": "#8b5cf6 bold",
        "signature": "#f0abfc bold",
        "accent": "#d8b4fe bold",
        "panel.title": "#fde68a bold",
        "panel.label": "#f9a8d4 bold",
        "panel.value": "#f8d8ec bold",
        "panel.count": "#86efac bold",
        "muted": "#b8a6d9 bold",
        "deep": "#93c5fd bold",
        "ok": "#86efac bold",
        "warn": "#fde68a bold",
        "error": "#fca5a5 bold",
        "composer": "bg:#211234 #ffe4f1 bold",
        "composer.prompt": "bg:#211234 #f9a8d4 bold",
        "toolbar": "bg:#211234 #c4b5fd bold",
        "hotkey": "bg:#211234 #f9a8d4 bold",
        "wave": "bg:#211234 #f9a8d4 bold",
        "trace": "bg:#0f1226 #f8d8ec bold",
        "trace.user": "bg:#0f1226 #f9a8d4 bold",
        "trace.agent": "bg:#0f1226 #f8d8ec bold",
        "trace.agent.label": "bg:#0f1226 #93c5fd bold",
        "trace.heading": "bg:#0f1226 #93c5fd bold",
        "trace.bold": "bg:#0f1226 #ffffff bold",
        "trace.dim": "bg:#0f1226 #bda4df bold",
        "trace.code": "bg:#1e1530 #d8b4fe bold italic",
        "trace.code.inline": "bg:#26304a #fde68a bold",
        "trace.bullet": "bg:#0f1226 #93c5fd bold",
        "trace.table": "bg:#0f1226 #c4b5fd bold",
        "trace.tool": "bg:#0f1226 #e9d5ff bold",
        "trace.tool.rail": "bg:#0f1226 #8b5cf6 bold",
        "trace.tool.ok": "bg:#0f1226 #86efac bold",
        "trace.tool.error": "bg:#0f1226 #fca5a5 bold",
        "trace.separator": "bg:#0f1226 #6d5c85",
        "selected": "bg:#5b2c78 #fff5fb",
        "scrollbar.track": "bg:#0f1226 #8b5cf6",
        "scrollbar.thumb": "bg:#0f1226 #d8b4fe bold",
        "scrollbar.background": "bg:#0f1226 #0f1226",
        "scrollbar.button": "bg:#0f1226 #d8b4fe bold",
        "scrollbar.arrow": "bg:#0f1226 #8b5cf6",
        "permission.frame": "bg:#1b0f2e #ffe4f1 bold",
        "permission.title": "bg:#1b0f2e #f9a8d4 bold",
        "permission.text": "bg:#1b0f2e #fff5fb bold",
        "permission.dim": "bg:#1b0f2e #c4b5fd bold",
        "permission.option": "bg:#3b1b55 #fde68a bold",
        "permission.alert": "bg:#1b0f2e #93c5fd bold",
        "slash.frame": "bg:#111f38 #eaf2ff bold",
        "slash.title": "bg:#111f38 #93c5fd bold",
        "slash.match": "bg:#334966 #ffffff bold",
        "slash.name": "bg:#111f38 #f8d8ec bold",
        "slash.name.selected": "bg:#334966 #ffffff bold",
        "slash.desc": "bg:#111f38 #b8c7e0 bold",
        "slash.desc.selected": "bg:#334966 #ffffff bold",
        "slash.footer": "bg:#111f38 #d8b4fe bold",
        "command.frame": "bg:#111f38 #eaf2ff bold",
        "command.title": "bg:#111f38 #fde68a bold",
        "command.text": "bg:#111f38 #f8d8ec bold",
        "command.dim": "bg:#111f38 #b8c7e0 bold",
        "command.ok": "bg:#111f38 #86efac bold",
        "command.error": "bg:#111f38 #fca5a5 bold",
    }
)


class TraceLexer(Lexer):
    def lex_document(self, document):
        def get_line(lineno: int):
            try:
                line = document.lines[lineno]
            except IndexError:
                return []
            return _highlight_trace_line(line)

        return get_line


class TraceScrollbarMargin(Margin):
    def __init__(self, owner: "DashboardUI", target: str):
        self.owner = owner
        self.target = target

    def get_width(self, get_ui_content) -> int:
        return 2

    def create_margin(self, window_render_info, width: int, height: int):
        render_height = max(1, int(height or 1))
        self.owner._remember_scrollbar_height(self.target, render_height)
        result = []
        for row in range(render_height):
            result.extend(self.owner._scrollbar_line_fragments(self.target, row, render_height))
            if row < render_height - 1:
                result.append(("", "\n"))
        return result


def _highlight_trace_line(line: str):
    stripped = line.strip()
    if not line:
        return [("class:trace", "")]
    if line.startswith("> "):
        return [("class:trace.user", "> "), *_inline_fragments(line[2:], "class:trace.user")]
    if line in WELCOME_TRACE_ROWS:
        idx = WELCOME_TRACE_ROWS.index(line)
        style = LILBOT_LOGO_STYLES[min(idx, len(LILBOT_LOGO_STYLES) - 1)]
        return [(style, line)]
    if stripped == "LILBOT":
        return [("class:trace.agent.label", "LILBOT")]
    if re.match(r"^\s{0,3}#{1,6}\s+", line):
        return [("class:trace.heading", stripped)]
    if line.startswith("╭") or line.startswith("├"):
        return [("class:trace.tool.rail", line[:2]), *_inline_fragments(line[2:], "class:trace.tool")]
    if line.startswith("│"):
        return [("class:trace.tool.rail", "│ "), *_inline_fragments(line[1:].lstrip(), "class:trace.dim")]
    if line.startswith("╰"):
        style = "class:trace.tool.error" if "error" in line.lower() else "class:trace.tool.ok"
        return [("class:trace.tool.rail", line[:2]), (style, line[2:])]
    if stripped.startswith("completed "):
        return [("class:trace.tool.ok", line)]
    if stripped.startswith("ERROR:") or stripped.startswith("error "):
        return [("class:trace.tool.error", line)]
    if re.match(r"^\s*[-*]\s+", line) or re.match(r"^\s*\d+\.\s+", line):
        marker, rest = line.split(" ", 1)
        return [("class:trace.bullet", marker + " "), *_inline_fragments(rest, "class:trace.agent")]
    if stripped.startswith("|") or " | " in line or stripped[:1] in {"┌", "┬", "┐", "├", "┼", "┤", "└", "┴", "┘", "│"}:
        return _inline_fragments(line, "class:trace.table")
    if stripped.startswith("```") or line.startswith("    "):
        return [("class:trace.code", line)]
    if re.match(r"^\s*-{3,}\s*$", line):
        return [("class:trace.separator", line)]
    return _inline_fragments(line, "class:trace.agent")


def _inline_fragments(text: str, base_style: str):
    fragments = []
    pattern = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*)")
    pos = 0
    for match in pattern.finditer(text):
        if match.start() > pos:
            fragments.append((base_style, text[pos:match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            fragments.append(("class:trace.code.inline", token[1:-1]))
        else:
            fragments.append(("class:trace.bold", token[2:-2]))
        pos = match.end()
    if pos < len(text):
        fragments.append((base_style, text[pos:]))
    return fragments or [(base_style, "")]


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def _pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _table_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or "|" not in stripped[1:]:
        return None
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _is_table_separator(cells: list[str] | None) -> bool:
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _render_table(rows: list[list[str]]) -> list[str]:
    columns = max(len(row) for row in rows)
    normalized = [row + [""] * (columns - len(row)) for row in rows]
    widths = [max(_display_width(row[index]) for row in normalized) for index in range(columns)]

    def border(left: str, middle: str, right: str) -> str:
        return left + middle.join("─" * (width + 2) for width in widths) + right

    rendered = [border("┌", "┬", "┐")]
    for index, row in enumerate(normalized):
        rendered.append("│ " + " │ ".join(_pad_display(cell, widths[col]) for col, cell in enumerate(row)) + " │")
        rendered.append(border("├" if index == 0 else "├", "┼", "┤") if index < len(normalized) - 1 else border("└", "┴", "┘"))
    return rendered


def _format_markdown_tables(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        first = _table_cells(lines[index])
        second = _table_cells(lines[index + 1]) if index + 1 < len(lines) else None
        if first and _is_table_separator(second):
            rows = [first]
            index += 2
            while index < len(lines):
                cells = _table_cells(lines[index])
                if not cells or _is_table_separator(cells):
                    break
                rows.append(cells)
                index += 1
            out.extend(_render_table(rows))
            continue
        out.append(lines[index])
        index += 1
    return "\n".join(out)


def _compact_json(value: object, limit: int = 220) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return _clip_line(text, limit)


def _clip_line(value: str, limit: int = TRACE_OUTPUT_MAX_LINE_CHARS) -> str:
    value = value.replace("\r", "").replace("\t", "    ")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _is_noisy_path_line(line: str) -> bool:
    normalized = line.replace("\\", "/")
    return any(marker.replace("\\", "/") in normalized for marker in NOISY_PATH_MARKERS)


def _windows_clipboard_payload(text: str) -> bytes:
    return str(text).encode("utf-16-le") + b"\x00\x00"


def _write_windows_unicode_clipboard(text: str) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    cf_unicodetext = 13
    gmem_moveable = 0x0002
    payload = _windows_clipboard_payload(text)
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE

    handle = kernel32.GlobalAlloc(gmem_moveable, len(payload))
    if not handle:
        return False
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(locked, payload, len(payload))
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False
    try:
        if not user32.EmptyClipboard():
            kernel32.GlobalFree(handle)
            return False
        if not user32.SetClipboardData(cf_unicodetext, handle):
            kernel32.GlobalFree(handle)
            return False
        handle = None
        return True
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


def _summarize_interim_text(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    if len(lines) <= 4 and len(text) <= 700:
        return [_clip_line(line) for line in lines]
    shown = [_clip_line(line, 160) for line in lines[:3]]
    hidden = max(0, len(lines) - len(shown))
    return [
        "planning tool work; condensed intermediate reasoning.",
        *shown,
        f"... hidden {hidden} planning lines while tools run.",
    ]


def _summarize_tool_output(name: str, output: str, metadata: dict | None = None, ok: bool = True) -> list[str]:
    if not output:
        return []
    metadata = metadata or {}
    lines = output.splitlines() or [output]
    line_limit = TRACE_TOOL_LINE_LIMITS.get(name, 12)
    char_count = len(output)
    hidden_noisy = 0
    candidate_lines = lines

    if name in {"list_dir", "glob"}:
        filtered = []
        for line in lines:
            if _is_noisy_path_line(line):
                hidden_noisy += 1
            else:
                filtered.append(line)
        candidate_lines = filtered or ["(only noisy cache/git paths matched)"]

    should_summarize = (
        hidden_noisy > 0
        or len(candidate_lines) > line_limit
        or len(lines) > line_limit
        or char_count > TRACE_OUTPUT_SMALL_CHARS
        or any(len(line) > TRACE_OUTPUT_MAX_LINE_CHARS for line in candidate_lines)
    )
    if not should_summarize:
        return [_clip_line(line) for line in candidate_lines]

    shown = [_clip_line(line) for line in candidate_lines[:line_limit]]
    hidden_lines = max(0, len(lines) - len(shown) - hidden_noisy)
    summary = [
        f"output summarized: {len(lines)} lines, {char_count} chars; showing {len(shown)} representative lines."
    ]
    if hidden_noisy:
        summary.append(f"omitted {hidden_noisy} noisy cache/git path lines.")
    if metadata.get("truncated"):
        summary.append("tool result was truncated by the registry before it reached the model.")
    summary.extend(shown)
    if hidden_lines:
        summary.append(f"... hidden {hidden_lines} more lines to keep Trace responsive.")
    if ok:
        summary.append("full tool result is kept in the agent turn context, subject to registry limits.")
    return summary


class DashboardUI:
    def __init__(self, agent: Agent, registry: ToolRegistry, ctx: ToolContext):
        if Application is None:
            raise RuntimeError("prompt_toolkit is required for the dashboard UI")
        self.agent = agent
        self.registry = registry
        self.ctx = ctx
        self.lines: list[str] = [
            *WELCOME_TRACE_ROWS,
            "",
            "Boot sequence ready.",
            "Trace is the main conversation and tool-execution stream.",
            "Trace keeps final answers readable and summarizes noisy tool output.",
            "Copy: terminal Ctrl+Shift+C or F2. Exit: press Ctrl+C twice.",
        ]
        self.work_items: list[str] = ["No active work."]
        self.tool_count = 0
        self.busy = False
        self.wave_index = 0
        self.auto_scroll = True
        self.work_auto_scroll = True
        self.pending_permission: str | None = None
        self.permission_answer = ""
        self.permission_event = threading.Event()
        self.permission_lock = threading.Lock()
        self.quit_armed_until = 0.0
        self.drag_target: str | None = None
        self.drag_last_y = 0
        self.scrollbar_render_heights = {"trace": 1, "work": 1}
        self.slash_selection = 0
        self.slash_hidden_for_text = ""
        self.command_popup_title = ""
        self.command_popup_lines: list[str] = []
        self.command_popup_error = False
        self.route_slash_to_popup = False
        self.ctx.permissions.quiet = True
        self.ctx.permissions.prompt = self.permission_prompt

        self.trace = TextArea(
            text=self._trace_text(),
            read_only=True,
            focusable=True,
            focus_on_click=True,
            scrollbar=False,
            lexer=TraceLexer(),
            wrap_lines=True,
            style="class:trace",
        )
        self.work = TextArea(
            text=self._work_text(),
            read_only=True,
            focusable=True,
            focus_on_click=True,
            scrollbar=False,
            lexer=TraceLexer(),
            wrap_lines=True,
            style="class:trace",
        )
        self.trace.window.right_margins = [TraceScrollbarMargin(self, "trace")]
        self.work.window.right_margins = [TraceScrollbarMargin(self, "work")]
        self._install_scrollbar_margin_mouse_handler(self.trace, "trace")
        self._install_scrollbar_margin_mouse_handler(self.work, "work")
        self.input = TextArea(
            height=5,
            multiline=True,
            focusable=True,
            focus_on_click=True,
            wrap_lines=True,
            prompt=[("class:composer.prompt", " LilBot / compose > ")],
            accept_handler=self._accept,
            style="class:composer",
        )
        self.app = Application(
            layout=Layout(self._root_container(), focused_element=self.input),
            key_bindings=self._keys(),
            style=STYLE,
            full_screen=True,
            mouse_support=True,
            refresh_interval=0.16,
            cursor=CursorShape.BLINKING_BEAM,
        )
        self._install_mouse_handlers()

    def run(self) -> int:
        self.app.run()
        return 0

    def print(self, value: str = "", style: str | None = None) -> None:
        if self.route_slash_to_popup:
            self._show_command_popup("Command", str(value).splitlines() or [""])
            return
        self._append(value)

    def error(self, message: str) -> None:
        if self.route_slash_to_popup:
            self._show_command_popup("Command error", [message], is_error=True)
            return
        self._append(f"ERROR: {message}")

    def table(self, title: str, columns: list[str], rows: Iterable[Iterable[str]]) -> None:
        if self.route_slash_to_popup:
            self._show_command_popup(title, self._table_lines(columns, rows))
            return
        self._append(title)
        self._append(" | ".join(columns))
        self._append("-" * min(96, max(8, len(title) + 20)))
        for row in rows:
            self._append(" | ".join(str(item) for item in row))

    def _table_lines(self, columns: list[str], rows: Iterable[Iterable[str]]) -> list[str]:
        data = [[str(cell) for cell in columns], *[[str(cell) for cell in row] for row in rows]]
        if not data:
            return []
        width_count = max(len(row) for row in data)
        normalized = [row + [""] * (width_count - len(row)) for row in data]
        widths = [max(_display_width(row[index]) for row in normalized) for index in range(width_count)]
        lines = [" | ".join(_pad_display(cell, widths[index]) for index, cell in enumerate(normalized[0]))]
        lines.append("-+-".join("-" * width for width in widths))
        for row in normalized[1:]:
            lines.append(" | ".join(_pad_display(cell, widths[index]) for index, cell in enumerate(row)))
        return lines

    def _show_command_popup(self, title: str, lines: list[str], is_error: bool = False) -> None:
        self.command_popup_title = title
        self.command_popup_lines = lines[:18] or ["(no output)"]
        self.command_popup_error = is_error
        self._refresh()

    def _clear_command_popup(self) -> None:
        self.command_popup_title = ""
        self.command_popup_lines = []
        self.command_popup_error = False

    def help(self, compact: bool = False) -> None:
        rows = [
            ("/help", "show commands"),
            ("/copy", "copy all Trace to clipboard"),
            ("/theme", "show theme preview"),
            ("/model [flash|pro]", "switch DeepSeek model"),
            ("/tools", "list tools"),
            ("/skills", "list skills"),
            ("/memory list|search|save|delete", "manage memory"),
            ("/agents", "list subagents"),
            ("/permissions MODE", "ask / accept-all / deny-all"),
            ("/display", "show terminal and font diagnostics"),
            ("/exit", "quit"),
        ]
        self.table("Command deck", ["Command", "Purpose"], rows)

    def theme_demo(self) -> None:
        if self.route_slash_to_popup:
            self._show_command_popup(
                "Theme deck",
                [
                    "1  nebula blush      selected",
                    "2  pale violet       soon",
                    "3  soft midnight     soon",
                    "4  ansi compatible   soon",
                    "",
                    '  1  function greet() {',
                    '- 2    console.log("Hello, World!");',
                    '+ 2    console.log("Hello, LilBot!");',
                    "  3  }",
                ],
            )
            return
        self._append("Theme deck")
        self._append("1  nebula blush      selected")
        self._append("2  pale violet       soon")
        self._append("3  soft midnight     soon")
        self._append("4  ansi compatible   soon")
        self._append("")
        self._append("  1  function greet() {")
        self._append('- 2    console.log("Hello, World!");')
        self._append('+ 2    console.log("Hello, LilBot!");')
        self._append("  3  }")

    def event(self, event: object) -> None:
        if isinstance(event, TextDelta):
            if event.interim:
                summary = _summarize_interim_text(event.text)
                if summary:
                    self._append("")
                    self._append("╭─ ◌ planning")
                    for line in summary:
                        self._append(f"│ {line}")
                    self._append("╰─ waiting for tool results")
            else:
                self._append("")
                self._append("LILBOT")
                self._append(_format_markdown_tables(event.text))
        elif isinstance(event, ToolStarted):
            self.tool_count += 1
            stamp = datetime.now().strftime("%H%M%S")
            arg_limit = 120 if event.name == "bash" else 220
            args = _compact_json(event.arguments, arg_limit)
            self.work_items = [
                f"step {self.tool_count}",
                f"tool  {event.name}",
                f"args  {_compact_json(event.arguments, 90 if event.name == 'bash' else 150)}",
            ]
            self._append("")
            self._append(f"╭─ ▷ run {stamp}-{self.tool_count:02d}  {event.name}")
            self._append(f"│ args {args}")
        elif isinstance(event, ToolFinished):
            mark = "done" if event.ok else "error"
            summary = _summarize_tool_output(event.name, event.output, event.metadata, event.ok)
            for line in summary:
                self._append(f"│ {line}")
            self._append(f"╰─ {mark} {event.name} {event.elapsed_ms}ms")
            self.work_items = [
                f"{mark} {event.name}",
                f"{event.elapsed_ms}ms",
                f"{len(event.output.splitlines()) if event.output else 0} output lines",
            ]
        elif isinstance(event, TurnFinished):
            self._append(f"completed {event.steps} steps")
            self.work_items = ["No active work."]

    def _permission_popup(self):
        label = self.pending_permission or ""
        display_label = _clip_line(label, 140)
        fragments = [
            ("class:permission.title", "  >> PERMISSION GATE\n"),
            ("class:permission.dim", "  Local sandbox request is paused until you choose.\n\n"),
            ("class:permission.alert", "  request  "),
            ("class:permission.text", display_label + "\n\n"),
            ("class:permission.option", "  y  "),
            ("class:permission.text", " allow once        "),
            ("class:permission.option", "  a  "),
            ("class:permission.text", " always allow\n"),
            ("class:permission.option", "  n  "),
            ("class:permission.text", " deny once         "),
            ("class:permission.option", "  d  "),
            ("class:permission.text", " always deny\n\n"),
            ("class:permission.dim", "  Type the letter in Composer, then press Enter."),
        ]
        if display_label != label:
            fragments.append(("class:permission.dim", "\n  Display shortened here; the full request remains intact."))
        return FormattedText(fragments)

    def permission_prompt(self, label: str) -> str:
        with self.permission_lock:
            self.permission_answer = ""
            self.pending_permission = label
            self.permission_event.clear()
        self.work_items = ["waiting for permission", "type y/a/n/d in Composer"]
        self._refresh()
        try:
            self.app.layout.focus(self.input)
        except Exception:
            pass
        self.permission_event.wait()
        with self.permission_lock:
            answer = self.permission_answer or "n"
            self.pending_permission = None
        self._refresh()
        return answer

    def _accept(self, buffer) -> bool:
        line = buffer.text.strip()
        buffer.text = ""
        self.slash_hidden_for_text = ""
        if not line:
            return False
        if self._answer_permission(line):
            return False
        if line in {"/exit", "/quit", "/q"}:
            self.app.exit(result=0)
            return False
        if line == "/copy":
            self._copy_trace()
            return False
        if self.busy:
            self._append("Agent is still working. Wait for completion before sending another prompt.")
            return False
        if self._is_popup_slash_command(line):
            self._clear_command_popup()
            threading.Thread(target=self._process_line, args=(line,), daemon=True).start()
            return False
        self._clear_command_popup()
        self._force_trace_autoscroll()
        self._append(f"> {line}")
        threading.Thread(target=self._process_line, args=(line,), daemon=True).start()
        return False

    def _answer_permission(self, line: str) -> bool:
        with self.permission_lock:
            if self.pending_permission is None:
                return False
            self.permission_answer = line
            self.permission_event.set()
        self.work_items = [f"permission answered: {line[:24]}", "resuming tool execution"]
        self._refresh()
        return True

    def _process_line(self, line: str) -> None:
        if self._is_popup_slash_command(line):
            self.work_items = ["command palette", line, "routing output to popup"]
            self.route_slash_to_popup = True
            self._refresh()
            try:
                if not handle_slash(line, self.agent, self.registry, self.ctx, self):
                    self._show_command_popup("Command", [f"Unknown command: {line}"], is_error=True)
            except KeyboardInterrupt:
                self.app.exit(result=0)
            except Exception as exc:  # pragma: no cover - interactive guard
                self._show_command_popup("Command error", [f"{type(exc).__name__}: {exc}"], is_error=True)
            finally:
                self.route_slash_to_popup = False
                self.work_items = ["No active work."]
                self._refresh()
            return

        self.busy = True
        self.work_items = ["thinking", "LLM turn in progress", "watch the wave strip below"]
        self._refresh()
        try:
            if not handle_slash(line, self.agent, self.registry, self.ctx, self):
                run_prompt(self.agent, self, line)
        except KeyboardInterrupt:
            self.app.exit(result=0)
        except Exception as exc:  # pragma: no cover - interactive guard
            self.error(f"{type(exc).__name__}: {exc}")
        finally:
            self.busy = False
            self._refresh()

    def _is_popup_slash_command(self, line: str) -> bool:
        if not line.startswith("/"):
            return False
        command = line[1:].split(maxsplit=1)[0].lower()
        return command not in {"skill"}

    def _append(self, text: str) -> None:
        for line in str(text).splitlines() or [""]:
            self.lines.append(line)
        self.lines = self.lines[-TRACE_MAX_LINES:]
        self._refresh()

    def _force_trace_autoscroll(self) -> None:
        self.auto_scroll = True
        try:
            self.trace.buffer.exit_selection()
        except Exception:
            pass
        self._scroll_trace_to_bottom(self.trace.text)

    def _scroll_trace_to_bottom(self, text: str) -> None:
        self._scroll_text_area_to_bottom(self.trace, text)

    def _scroll_text_area_to_bottom(self, area: TextArea, text: str) -> None:
        area.buffer.cursor_position = len(text)
        area.window.vertical_scroll = self._max_scroll_for_area(area)
        area.window.vertical_scroll_2 = 0

    def _sync_text_area_text(self, area: TextArea, text: str, auto_scroll: bool) -> None:
        if area.text == text:
            if auto_scroll:
                self._scroll_text_area_to_bottom(area, text)
            return

        previous_scroll = int(getattr(area.window, "vertical_scroll", 0) or 0)
        previous_scroll_2 = int(getattr(area.window, "vertical_scroll_2", 0) or 0)
        previous_cursor = min(int(getattr(area.buffer, "cursor_position", 0) or 0), len(text))

        area.text = text
        if auto_scroll:
            self._scroll_text_area_to_bottom(area, text)
            return

        area.window.vertical_scroll = self._clamp_scroll(area, previous_scroll)
        area.window.vertical_scroll_2 = previous_scroll_2
        area.buffer.cursor_position = previous_cursor

    def _refresh(self) -> None:
        text = self._trace_text()
        self._sync_text_area_text(self.trace, text, self.auto_scroll)
        work_text = self._work_text()
        self._sync_text_area_text(self.work, work_text, self.work_auto_scroll)
        try:
            self.app.invalidate()
        except Exception:
            pass

    def _trace_text(self) -> str:
        return "\n".join(self.lines)

    def _slash_query(self) -> str | None:
        text = self.input.text.strip()
        if not text.startswith("/") or "\n" in text:
            return None
        if " " in text:
            return None
        if text == self.slash_hidden_for_text:
            return None
        return text[1:]

    def _slash_matches(self):
        query = self._slash_query()
        if query is None:
            return []
        matches = slash_commands_matching(query)[:7]
        if self.slash_selection >= len(matches):
            self.slash_selection = 0
        return matches

    def _slash_suggestions_visible(self) -> bool:
        return bool(self._slash_matches()) and not self.pending_permission

    def _slash_suggestions_popup(self):
        matches = self._slash_matches()
        fragments = [("class:slash.title", "  COMMAND DECK\n")]
        if not matches:
            fragments.append(("class:slash.desc", "  No slash commands match.\n"))
        for index, command in enumerate(matches):
            selected = index == self.slash_selection
            name_style = "class:slash.name.selected" if selected else "class:slash.name"
            desc_style = "class:slash.desc.selected" if selected else "class:slash.desc"
            prefix_style = "class:slash.match" if selected else "class:slash.title"
            aliases = f"  aliases: {', '.join('/' + alias for alias in command.aliases)}" if command.aliases else ""
            marker = "  > " if selected else "    "
            fragments.extend(
                [
                    (prefix_style, marker),
                    (name_style, _pad_display(command.usage, 34)),
                    (desc_style, f" {command.description}{aliases}\n"),
                ]
            )
        fragments.append(("class:slash.footer", "  Up/Down move   Tab accept   Esc close"))
        return FormattedText(fragments)

    def _accept_slash_suggestion(self) -> bool:
        matches = self._slash_matches()
        if not matches:
            return False
        command = matches[self.slash_selection]
        self.input.buffer.text = command.palette_text
        self.input.buffer.cursor_position = len(self.input.buffer.text)
        self.slash_hidden_for_text = self.input.buffer.text.strip()
        self.app.layout.focus(self.input)
        self._refresh()
        return True

    def _move_slash_selection(self, delta: int) -> bool:
        matches = self._slash_matches()
        if not matches:
            return False
        self.slash_selection = (self.slash_selection + delta) % len(matches)
        self._refresh()
        return True

    def _command_popup_visible(self) -> bool:
        return bool(self.command_popup_lines) and not self.input.text.strip() and not self._slash_suggestions_visible()

    def _command_popup(self):
        style = "class:command.error" if self.command_popup_error else "class:command.text"
        fragments = [("class:command.title", f"  {self.command_popup_title or 'Command'}\n")]
        for line in self.command_popup_lines:
            fragments.append((style, f"  {line}\n"))
        fragments.append(("class:command.dim", "  Esc close"))
        return FormattedText(fragments)

    def _keys(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        def _exit(event) -> None:
            now = monotonic()
            if now <= self.quit_armed_until:
                event.app.exit(result=0)
                return
            self.quit_armed_until = now + QUIT_CONFIRM_SECONDS
            self._append("Press Ctrl+C again to exit. Use terminal Ctrl+Shift+C or F2 to copy Trace.")

        @kb.add("escape")
        def _focus_input(event) -> None:
            if self._slash_suggestions_visible():
                self.slash_hidden_for_text = self.input.text.strip()
                self._refresh()
                return
            if self.command_popup_lines:
                self._clear_command_popup()
                self._refresh()
                return
            event.app.layout.focus(self.input)

        @kb.add("down", filter=Condition(lambda: self._slash_suggestions_visible()))
        def _slash_down(event) -> None:
            self._move_slash_selection(1)

        @kb.add("up", filter=Condition(lambda: self._slash_suggestions_visible()))
        def _slash_up(event) -> None:
            self._move_slash_selection(-1)

        @kb.add("tab", filter=Condition(lambda: self._slash_suggestions_visible()))
        def _slash_accept(event) -> None:
            self._accept_slash_suggestion()

        @kb.add("c-v")
        def _paste(event) -> None:
            event.app.layout.focus(self.input)
            self.input.buffer.insert_text(self._read_clipboard())

        @kb.add("enter")
        def _submit(event) -> None:
            event.app.layout.focus(self.input)
            self._accept(self.input.buffer)

        @kb.add("c-j")
        def _composer_newline(event) -> None:
            event.app.layout.focus(self.input)
            self.input.buffer.insert_text("\n")

        @kb.add("escape", "enter")
        def _composer_alt_enter(event) -> None:
            event.app.layout.focus(self.input)
            self.input.buffer.insert_text("\n")

        @kb.add("f2")
        def _copy(event) -> None:
            self._copy_trace(selection_first=True)

        @kb.add("c-insert")
        def _copy_ctrl_insert(event) -> None:
            self._copy_trace(selection_first=True)

        @kb.add("f3")
        def _copy_all(event) -> None:
            self._copy_trace(selection_first=False)

        @kb.add("f4")
        def _focus_trace(event) -> None:
            event.app.layout.focus(self.trace)

        @kb.add("f5")
        def _focus_work(event) -> None:
            event.app.layout.focus(self.work)

        @kb.add("pageup")
        def _page_up(event) -> None:
            if event.app.layout.has_focus(self.work):
                self._scroll_work(-6)
            else:
                self._scroll_trace(-12)

        @kb.add("pagedown")
        def _page_down(event) -> None:
            if event.app.layout.has_focus(self.work):
                self._scroll_work(6)
            else:
                self._scroll_trace(12)

        @kb.add("c-home")
        def _trace_home(event) -> None:
            self.auto_scroll = False
            self.trace.buffer.cursor_position = 0
            event.app.layout.focus(self.trace)

        @kb.add("c-end")
        def _trace_end(event) -> None:
            self.auto_scroll = True
            self.trace.buffer.cursor_position = len(self.trace.text)
            event.app.layout.focus(self.trace)

        return kb

    def _scroll_trace(self, lines: int) -> None:
        self.auto_scroll = False
        self.app.layout.focus(self.trace)
        at_bottom = self._scroll_text_area(self.trace, lines)
        if at_bottom:
            self.auto_scroll = True

    def _scroll_work(self, lines: int) -> None:
        self.work_auto_scroll = False
        self.app.layout.focus(self.work)
        at_bottom = self._scroll_text_area(self.work, lines)
        if at_bottom:
            self.work_auto_scroll = True

    def _scroll_text_area(self, area: TextArea, lines: int) -> bool:
        area.buffer.exit_selection()
        current = int(getattr(area.window, "vertical_scroll", 0) or 0)
        target = self._set_scroll_position(area, current + lines)
        max_scroll = self._max_scroll_for_area(area)
        return target >= max_scroll

    def _line_start_offset(self, text: str, line_index: int) -> int:
        if line_index <= 0:
            return 0
        offset = 0
        for idx, line in enumerate(text.splitlines(keepends=True)):
            if idx >= line_index:
                break
            offset += len(line)
        return min(offset, len(text))

    def _visible_line_count(self, area: TextArea) -> int:
        render_info = area.window.render_info
        if render_info is None:
            return 1
        displayed = getattr(render_info, "displayed_lines", None)
        if displayed:
            return max(1, len(displayed))
        height = int(getattr(render_info, "window_height", 0) or 0)
        return max(1, height)

    def _content_line_count(self, area: TextArea) -> int:
        render_info = area.window.render_info
        content_height = int(getattr(render_info, "content_height", 0) or 0) if render_info is not None else 0
        if content_height > 0:
            return content_height
        return max(1, len(area.text.splitlines()) or 1)

    def _max_scroll_for_area(self, area: TextArea) -> int:
        return max(0, self._content_line_count(area) - self._visible_line_count(area))

    def _clamp_scroll(self, area: TextArea, value: int) -> int:
        return max(0, min(self._max_scroll_for_area(area), int(value)))

    def _set_scroll_position(self, area: TextArea, value: int) -> int:
        target = self._clamp_scroll(area, value)
        area.window.vertical_scroll = target
        area.window.vertical_scroll_2 = 0
        area.buffer.cursor_position = self._line_start_offset(area.text, target)
        try:
            self.app.invalidate()
        except Exception:
            pass
        return target

    def _scroll_area_for_target(self, target: str) -> TextArea:
        return self.trace if target == "trace" else self.work

    def _focus_scroll_target(self, target: str) -> None:
        area = self._scroll_area_for_target(target)
        self.auto_scroll = False if target == "trace" else self.auto_scroll
        self.work_auto_scroll = False if target == "work" else self.work_auto_scroll
        self.app.layout.focus(area)

    def _install_scrollbar_margin_mouse_handler(self, area: TextArea, target: str) -> None:
        window = area.window
        if getattr(window, "_lilbot_scrollbar_margin_mouse", False):
            return

        original_write = window._write_to_screen_at_index

        def write_with_scrollbar_margin_mouse(screen, mouse_handlers, write_position, parent_style, erase_bg):
            original_write(screen, mouse_handlers, write_position, parent_style, erase_bg)
            right_width = sum(window._get_margin_width(margin) for margin in window.right_margins)
            if right_width <= 0 or write_position.width <= 0 or write_position.height <= 0:
                return

            x_min = write_position.xpos + max(0, write_position.width - right_width)
            x_max = write_position.xpos + write_position.width
            y_min = write_position.ypos
            y_max = write_position.ypos + write_position.height

            def margin_mouse_handler(mouse_event):
                local_x = max(0, min(right_width - 1, mouse_event.position.x - x_min))
                local_y = max(0, min(write_position.height - 1, mouse_event.position.y - y_min))
                local_event = MouseEvent(
                    position=Point(x=local_x, y=local_y),
                    event_type=mouse_event.event_type,
                    button=mouse_event.button,
                    modifiers=mouse_event.modifiers,
                )
                return self._handle_scrollbar_mouse(target, local_event)

            mouse_handlers.set_mouse_handler_for_range(
                x_min=x_min,
                x_max=x_max,
                y_min=y_min,
                y_max=y_max,
                handler=margin_mouse_handler,
            )

        window._write_to_screen_at_index = write_with_scrollbar_margin_mouse
        window._lilbot_scrollbar_margin_mouse = True

    def _remember_scrollbar_height(self, target: str, height: int) -> None:
        if not hasattr(self, "scrollbar_render_heights"):
            self.scrollbar_render_heights = {}
        self.scrollbar_render_heights[target] = max(1, int(height or 1))

    def _scrollbar_height(self, target: str) -> int:
        height = int(getattr(self, "scrollbar_render_heights", {}).get(target, 0) or 0)
        if height > 0:
            return height
        area = self._scroll_area_for_target(target)
        render_info = area.window.render_info
        height = int(getattr(render_info, "window_height", 0) or 0) if render_info is not None else 0
        return max(1, height)

    def _scrollbar_geometry(self, target: str, height: int | None = None) -> dict[str, int]:
        area = self._scroll_area_for_target(target)
        height = max(1, int(height or self._scrollbar_height(target)))
        track_top = 0
        track_height = height
        content_height = self._content_line_count(area)
        visible_height = min(content_height, self._visible_line_count(area))
        max_scroll = self._max_scroll_for_area(area)

        if max_scroll <= 0:
            thumb_height = track_height
            thumb_top = track_top
        else:
            thumb_height = max(1, min(track_height, int(track_height * visible_height / max(1, content_height))))
            thumb_travel = max(1, track_height - thumb_height)
            current = self._clamp_scroll(area, int(getattr(area.window, "vertical_scroll", 0) or 0))
            thumb_top = track_top + round((current / max_scroll) * thumb_travel)

        return {
            "height": height,
            "track_top": track_top,
            "track_height": track_height,
            "thumb_top": thumb_top,
            "thumb_height": thumb_height,
            "max_scroll": max_scroll,
        }

    def _scrollbar_line_fragments(self, target: str, row: int, height: int | None = None):
        geometry = self._scrollbar_geometry(target, height)
        row = max(0, min(geometry["height"] - 1, int(row or 0)))
        if geometry["max_scroll"] <= 0:
            return [("class:scrollbar.background", "  ")]
        if geometry["thumb_top"] <= row < geometry["thumb_top"] + geometry["thumb_height"]:
            return [("class:scrollbar.thumb", "\u2588\u2588")]
        return [("class:scrollbar.track", "\u2502\u2502")]

    def _begin_scrollbar_drag(self, target: str, y: int) -> None:
        self.drag_target = target
        self.drag_last_y = y
        self._focus_scroll_target(target)

    def _drag_scrollbar(self, target: str, y: int) -> bool:
        if self.drag_target != target:
            return False
        if y != self.drag_last_y:
            self._jump_scrollbar_to_row(target, y)
            self.drag_last_y = y
        return True

    def _end_scrollbar_drag(self, target: str) -> bool:
        if self.drag_target != target:
            return False
        self.drag_target = None
        self.drag_last_y = 0
        return True

    def _jump_scrollbar_to_row(self, target: str, y: int) -> int:
        area = self._scroll_area_for_target(target)
        geometry = self._scrollbar_geometry(target)
        max_scroll = geometry["max_scroll"]
        if max_scroll <= 0:
            target_scroll = self._set_scroll_position(area, 0)
            if target == "trace":
                self.auto_scroll = True
            else:
                self.work_auto_scroll = True
            return target_scroll

        track_top = geometry["track_top"]
        max_row = max(0, geometry["track_height"] - 1)
        relative_row = max(0, min(max_row, int(y or 0) - track_top))
        if max_row <= 0:
            next_scroll = 0
        else:
            next_scroll = (relative_row * max_scroll + max_row // 2) // max_row
        target_scroll = self._set_scroll_position(area, next_scroll)
        at_bottom = target_scroll >= max_scroll
        if target == "trace":
            self.auto_scroll = at_bottom
        else:
            self.work_auto_scroll = at_bottom
        return target_scroll

    def _visible_scrollbar_y_from_area_event(self, target: str, mouse_event) -> int:
        area = self._scroll_area_for_target(target)
        raw_y = int(getattr(getattr(mouse_event, "position", None), "y", 0) or 0)
        height = self._scrollbar_height(target)
        visible_y = None
        render_info = area.window.render_info
        if render_info is not None:
            mapping = getattr(render_info, "input_line_to_visible_line", None)
            try:
                visible_y = mapping.get(raw_y) if mapping is not None else None
            except Exception:
                visible_y = None
            if visible_y is None:
                displayed = getattr(render_info, "displayed_lines", None)
                try:
                    displayed_list = list(displayed or [])
                    if raw_y in displayed_list:
                        visible_y = displayed_list.index(raw_y)
                except Exception:
                    visible_y = None
        if visible_y is None:
            if 0 <= raw_y < height:
                visible_y = raw_y
            else:
                current = int(getattr(area.window, "vertical_scroll", 0) or 0)
                visible_y = raw_y - current
        return max(0, min(height - 1, int(visible_y or 0)))

    def _handle_scrollbar_mouse(self, target: str, mouse_event) -> object:
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._scroll_trace(-3) if target == "trace" else self._scroll_work(-2)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._scroll_trace(3) if target == "trace" else self._scroll_work(2)
            return None

        y = int(getattr(mouse_event.position, "y", 0) or 0)
        if mouse_event.button == MouseButton.LEFT and mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            self._begin_scrollbar_drag(target, y)
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_MOVE and self._drag_scrollbar(target, y):
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP and self._end_scrollbar_drag(target):
            return None
        return None

    def _copy_trace(self, selection_first: bool = True) -> None:
        text = self._selected_trace_text() if selection_first else ""
        label = "selection" if text else "Trace"
        text = text or self._trace_text()
        ok = self._write_clipboard(text)
        self._append(f"{label} copied to clipboard." if ok else "Clipboard copy failed.")

    def _selected_trace_text(self) -> str:
        if self.trace.buffer.selection_state is None:
            return ""
        try:
            data = self.trace.buffer.copy_selection()
            return data.text
        except Exception:
            return ""

    def _install_mouse_handlers(self) -> None:
        input_mouse_handler = self.input.control.mouse_handler
        trace_mouse_handler = self.trace.control.mouse_handler
        work_mouse_handler = self.work.control.mouse_handler

        def composer_mouse(mouse_event):
            if mouse_event.event_type in {MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_UP}:
                self.app.layout.focus(self.input)
            if (
                mouse_event.button == MouseButton.RIGHT
                and mouse_event.event_type == MouseEventType.MOUSE_UP
            ):
                self.app.layout.focus(self.input)
                text = self._read_clipboard()
                if text:
                    self.input.buffer.insert_text(text)
                return None
            return input_mouse_handler(mouse_event)

        def trace_mouse(mouse_event):
            if mouse_event.event_type == MouseEventType.SCROLL_UP:
                self._scroll_trace(-3)
                return None
            if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                self._scroll_trace(3)
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_MOVE and self.drag_target == "trace":
                y = self._visible_scrollbar_y_from_area_event("trace", mouse_event)
                self._drag_scrollbar("trace", y)
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._end_scrollbar_drag("trace"):
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                self.auto_scroll = False
            if mouse_event.button == MouseButton.RIGHT and mouse_event.event_type == MouseEventType.MOUSE_UP:
                self._copy_trace(selection_first=True)
                return None
            return trace_mouse_handler(mouse_event)

        def work_mouse(mouse_event):
            if mouse_event.event_type == MouseEventType.SCROLL_UP:
                self._scroll_work(-2)
                return None
            if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                self._scroll_work(2)
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_MOVE and self.drag_target == "work":
                y = self._visible_scrollbar_y_from_area_event("work", mouse_event)
                self._drag_scrollbar("work", y)
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._end_scrollbar_drag("work"):
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                self.work_auto_scroll = False
            return work_mouse_handler(mouse_event)

        self.input.control.mouse_handler = composer_mouse
        self.trace.control.mouse_handler = trace_mouse
        self.work.control.mouse_handler = work_mouse

    def _write_clipboard(self, text: str) -> bool:
        try:
            if os.name == "nt":
                return _write_windows_unicode_clipboard(text)
            if shutil.which("pbcopy"):
                subprocess.run(["pbcopy"], input=text, text=True, check=True)
                return True
            if shutil.which("xclip"):
                subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, check=True)
                return True
        except Exception:
            return False
        return False

    def _read_clipboard(self) -> str:
        try:
            import tkinter

            root = tkinter.Tk()
            root.withdraw()
            text = root.clipboard_get()
            root.destroy()
            return text
        except Exception:
            pass
        if os.name == "nt":
            try:
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                    text=True,
                    capture_output=True,
                    timeout=3,
                )
                return proc.stdout or ""
            except Exception:
                return ""
        return ""

    def _root_container(self):
        left_column = HSplit(
            [
                Frame(
                    Box(Window(FormattedTextControl(self._agent_panel), wrap_lines=False), padding=1),
                    title="  LilBot Agent  ",
                    style="class:frame",
                    height=Dimension(min=26, preferred=34, max=38, weight=3),
                ),
                Frame(
                    self.work,
                    title="  Work  ",
                    style="class:frame",
                    height=Dimension(min=14, preferred=22, weight=3),
                ),
            ],
            width=Dimension(weight=17),
        )

        main_area = VSplit(
            [
                left_column,
                Frame(
                    self.trace,
                    title="  Trace  ",
                    style="class:frame",
                    width=Dimension(weight=23),
                ),
            ],
            height=Dimension(weight=1),
        )

        base = HSplit(
            [
                Window(FormattedTextControl(self._topbar), height=1, style="class:topbar"),
                main_area,
                Frame(self.input, title="  Composer  ", style="class:frame", height=7),
                Window(FormattedTextControl(self._status_strip), height=1, style="class:toolbar"),
                Window(FormattedTextControl(self._toolbar), height=1, style="class:toolbar"),
            ],
            style="class:root",
        )
        return FloatContainer(
            content=base,
            floats=[
                Float(
                    content=ConditionalContainer(
                        Frame(
                            Box(
                                Window(
                                    FormattedTextControl(self._permission_popup),
                                    height=10,
                                    wrap_lines=True,
                                    style="class:permission.text",
                                ),
                                padding=1,
                            ),
                            title="  Permission Gate  ",
                            style="class:permission.frame",
                        ),
                        filter=Condition(lambda: self.pending_permission is not None),
                    ),
                    left=8,
                    right=8,
                    top=3,
                    z_index=20,
                ),
                Float(
                    content=ConditionalContainer(
                        Frame(
                            Box(
                                Window(
                                    FormattedTextControl(self._slash_suggestions_popup),
                                    height=9,
                                    wrap_lines=False,
                                    style="class:slash.frame",
                                ),
                                padding=0,
                            ),
                            title="  Slash Commands  ",
                            style="class:slash.frame",
                        ),
                        filter=Condition(lambda: self._slash_suggestions_visible()),
                    ),
                    left=0,
                    right=0,
                    bottom=9,
                    z_index=12,
                ),
                Float(
                    content=ConditionalContainer(
                        Frame(
                            Box(
                                Window(
                                    FormattedTextControl(self._command_popup),
                                    height=12,
                                    wrap_lines=True,
                                    style="class:command.text",
                                ),
                                padding=1,
                            ),
                            title="  Command Output  ",
                            style="class:command.frame",
                        ),
                        filter=Condition(lambda: self._command_popup_visible()),
                    ),
                    left=6,
                    right=6,
                    bottom=9,
                    z_index=11,
                )
            ],
        )

    def _width(self) -> int:
        try:
            return get_app().output.get_size().columns
        except Exception:
            return 120

    def _topbar(self):
        cfg = self.ctx.config
        context_pct = self._context_percent()
        text = f" Agent  LilBot-agent-code  |  {cfg.model}  |  {cfg.provider} "
        right = f" ctx {context_pct:02d}%  permissions {self.ctx.permissions.mode}  |  v0.1 "
        width = self._width()
        gap = max(1, width - len(text) - len(right))
        return FormattedText(
            [
                ("class:topbar", text),
                ("class:topbar.dim", " " * gap),
                ("class:topbar", right),
            ]
        )

    def _context_percent(self) -> int:
        serialized = json.dumps(self.agent.messages, ensure_ascii=False, default=str)
        estimated_tokens = max(1, len(serialized) // 4)
        usage_tokens = self.agent.usage.get("prompt_tokens") or self.agent.usage.get("input_tokens") or 0
        estimated_tokens = max(estimated_tokens, int(usage_tokens))
        limit = self._model_context_limit()
        return min(99, int((estimated_tokens / limit) * 100))

    def _model_context_limit(self) -> int:
        model = self.ctx.config.model.lower()
        if "deepseek" in model:
            return 64000
        if "gpt-4o" in model:
            return 128000
        if "claude" in model:
            return 200000
        if "gemini" in model:
            return 1000000
        return 32000

    def _agent_panel(self):
        width = self._width()
        left_width = max(44, int(width * 0.425) - 6)
        fragments = []
        logo_rows = LILBOT_AGENT_LOGO_ROWS if left_width >= 90 else LILBOT_AGENT_LOGO_COMPACT_ROWS
        for idx, row in enumerate(logo_rows):
            style = LILBOT_LOGO_STYLES[min(idx, len(LILBOT_LOGO_STYLES) - 1)]
            fragments.append((style, _clip_line(row, left_width) + "\n"))

        fragments.extend(
            [
                ("class:signature", "Terrence Shen  //  China  //  Deeplearningman0723@gmail.com\n"),
                ("class:accent", ">_ clean-room local agent deck\n"),
                ("class:muted", "model: "),
                ("class:deep", self.ctx.config.model),
                ("class:muted", "   provider: "),
                ("class:deep", self.ctx.config.provider),
                ("class:muted", "   permissions: "),
                ("class:ok", self.ctx.permissions.mode),
                ("class:muted", "\nworkspace: "),
                ("class:accent", self._shorten_path(str(self.ctx.config.workspace), 78)),
                ("class:panel.title", "\n\nAvailable Tools "),
                ("class:panel.count", f"{len(self.registry.list())}"),
                ("class:muted", "\n"),
            ]
        )
        for label, names in self._tool_groups():
            fragments.extend(
                [
                    ("class:panel.label", f"{label}: "),
                    ("class:panel.value", self._compact_names(names, 6)),
                    ("class:muted", "\n"),
                ]
            )
        skills = self.ctx.skills.list()
        fragments.extend(
            [
                ("class:panel.title", "\nAvailable Skills "),
                ("class:panel.count", f"{len(skills)}"),
                ("class:muted", "\n"),
            ]
        )
        skill_names = [skill.name for skill in skills]
        fragments.extend(
            [
                ("class:panel.label", "bundled: "),
                ("class:panel.value", self._compact_names(skill_names, 8)),
                ("class:muted", "\n\n"),
                ("class:panel.title", "╭─ flow "),
                ("class:muted", "memory -> skills -> subagents -> mcp\n"),
                ("class:panel.title", "╰─ "),
                ("class:panel.value", "Trace on right, Work below this card.\n"),
            ]
        )
        return FormattedText(fragments)

    def _tool_groups(self) -> list[tuple[str, list[str]]]:
        groups: list[tuple[str, list[str]]] = [
            ("workspace", []),
            ("search", []),
            ("web", []),
            ("shell", []),
            ("memory", []),
            ("skills", []),
            ("agents", []),
            ("mcp", []),
        ]
        by_name = {label: names for label, names in groups}
        for tool in self.registry.list():
            name = tool.name
            if name in {"list_dir", "read_file", "write_file", "edit_file"}:
                by_name["workspace"].append(name)
            elif name in {"glob", "grep"}:
                by_name["search"].append(name)
            elif name in {"web_search", "fetch_url", "web_fetch"}:
                by_name["web"].append(name)
            elif name == "bash":
                by_name["shell"].append(name)
            elif name.startswith("memory_"):
                by_name["memory"].append(name.removeprefix("memory_"))
            elif name.startswith("skill_"):
                by_name["skills"].append(name.removeprefix("skill_"))
            elif name.startswith("agent_"):
                by_name["agents"].append(name.removeprefix("agent_"))
            elif name.startswith("mcp_"):
                by_name["mcp"].append(name.removeprefix("mcp_"))
        return [(label, names) for label, names in groups if names]

    def _compact_names(self, names: list[str], limit: int) -> str:
        if not names:
            return "(none)"
        shown = names[:limit]
        suffix = f", +{len(names) - limit}" if len(names) > limit else ""
        return ", ".join(shown) + suffix

    def _shorten_path(self, value: str, limit: int) -> str:
        return value if len(value) <= limit else "..." + value[-(limit - 3):]

    def _truncate(self, value: str, limit: int) -> str:
        return value if len(value) <= limit else value[: max(0, limit - 3)] + "..."

    def _work_text(self) -> str:
        ctx = getattr(self, "ctx", None)
        config = getattr(ctx, "config", None)
        permissions = getattr(ctx, "permissions", None)
        registry = getattr(self, "registry", None)
        try:
            tool_count = len(registry.list()) if registry is not None else 0
        except Exception:
            tool_count = 0
        rows = [
            "### Runtime",
            f"model: {getattr(config, 'model', 'unknown')}",
            f"provider: {getattr(config, 'provider', 'unknown')}",
            f"permissions: {getattr(permissions, 'mode', 'unknown')}",
            f"tools: {tool_count}",
            "",
            "### Active tool",
            *(self.work_items or ["No active work."]),
            "",
            "### Subagents",
            *self._subagent_work_lines(),
            "",
            "### Flow",
            *SYSTEM_MAP.splitlines(),
            "",
            "---",
            "F5 focuses Work. Mouse wheel or PageUp/PageDown scrolls the focused pane.",
            "Esc returns to Composer.",
        ]
        return "\n".join(rows)

    def _subagent_work_lines(self) -> list[str]:
        manager = getattr(getattr(self, "ctx", None), "subagents", None)
        if manager is None or not hasattr(manager, "runtime_status"):
            return ["status: unavailable"]
        try:
            status = manager.runtime_status()
        except Exception as exc:
            return [f"status: unavailable ({type(exc).__name__}: {exc})"]
        rows = [
            (
                f"concurrency: {status.get('running', 0)}/{status.get('max_concurrent', '?')} running"
                f" | queued {status.get('queued', 0)} | total {status.get('total', 0)}"
            )
        ]
        recent = status.get("recent") or []
        if not recent:
            rows.append("recent: none")
            return rows
        for item in recent[:5]:
            name = str(item.get("name") or item.get("agent_id") or "subagent")
            task_status = str(item.get("status") or "?")
            agent_type = str(item.get("agent_type") or "?")
            duration = int(item.get("duration_ms") or 0)
            rows.append(f"- {name} [{task_status}] {agent_type} {duration}ms")
            progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
            if progress:
                rows.append(
                    f"  progress: {progress.get('last_event') or 'none'}"
                    f" | events {progress.get('events', 0)}"
                    f" | resumes {progress.get('resume_count', 0)}"
                )
            handle = item.get("transcript_handle")
            if handle:
                rows.append(f"  transcript: {handle}")
            worktree = item.get("worktree") if isinstance(item.get("worktree"), dict) else {}
            worktree_status = worktree.get("status") if worktree else None
            if worktree_status and worktree_status != "none":
                branch = f" branch {worktree.get('branch')}" if worktree.get("branch") else ""
                rows.append(f"  worktree: {worktree_status}{branch} {worktree.get('path') or ''}".rstrip())
        return rows

    def _toolbar(self):
        return FormattedText(
            [
                ("class:hotkey", "/help"),
                ("class:toolbar", " commands   "),
                ("class:hotkey", "F2"),
                ("class:toolbar", " copy   "),
                ("class:hotkey", "F4"),
                ("class:toolbar", " trace   "),
                ("class:hotkey", "F5"),
                ("class:toolbar", " work   "),
                ("class:hotkey", "PgUp/PgDn"),
                ("class:toolbar", " scroll   "),
                ("class:hotkey", "Ctrl+V"),
                ("class:toolbar", " paste   "),
                ("class:hotkey", "Ctrl+C x2"),
                ("class:toolbar", " exit "),
            ]
        )

    def _status_strip(self):
        if self.busy:
            return self._busy_status_strip()
        model = self._model_badge()
        context_pct = self._context_percent()
        return FormattedText(
            [
                ("class:toolbar", " "),
                ("class:hotkey", model),
                ("class:toolbar", f" ready   ctx {context_pct:02d}%   "),
                ("class:wave", "▁▁▁▁▁▁▁▁▁▁"),
            ]
        )

    def _busy_status_strip(self):
        frame = WAVE_FRAMES[self.wave_index % len(WAVE_FRAMES)]
        self.wave_index += 1
        model = self._model_badge()
        left = f" {model}  thinking "
        right = f" ctx {self._context_percent():02d}% "
        width = self._width()
        if width < len(left) + len(right) + 8:
            right = ""
        if width < len(left) + len(right) + 4:
            right = _clip_line(right, max(0, width - len(left)))
        wave_width = max(0, width - len(left) - len(right))
        wave = (frame * ((wave_width // len(frame)) + 2))[:wave_width]
        return FormattedText(
            [
                ("class:wave", left),
                ("class:wave", wave),
                ("class:toolbar", right),
            ]
        )

    def _model_badge(self) -> str:
        model = self.ctx.config.model
        lower = model.lower()
        if "deepseek" in lower and "pro" in lower:
            return "deepseek-pro"
        if "deepseek" in lower and "flash" in lower:
            return "deepseek-flash"
        if len(model) > 28:
            return model[:25] + "..."
        return model
