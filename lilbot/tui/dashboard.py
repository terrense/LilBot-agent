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
from ..cli import handle_slash, run_prompt
from ..core.events import TextDelta, ToolFinished, ToolStarted, TurnFinished
from ..tools import ToolContext, ToolRegistry

try:
    from prompt_toolkit import Application
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.cursor_shapes import CursorShape
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, HSplit, Layout, VSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.lexers import Lexer
    from prompt_toolkit.mouse_events import MouseButton, MouseEventType
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
        "scrollbar.background": "bg:#1a1230",
        "scrollbar.button": "bg:#d8b4fe",
        "scrollbar.arrow": "#f9a8d4 bold",
        "permission.frame": "bg:#1b0f2e #ffe4f1 bold",
        "permission.title": "bg:#1b0f2e #f9a8d4 bold",
        "permission.text": "bg:#1b0f2e #fff5fb bold",
        "permission.dim": "bg:#1b0f2e #c4b5fd bold",
        "permission.option": "bg:#3b1b55 #fde68a bold",
        "permission.alert": "bg:#1b0f2e #93c5fd bold",
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


def _highlight_trace_line(line: str):
    stripped = line.strip()
    if not line:
        return [("class:trace", "")]
    if line.startswith("> "):
        return [("class:trace.user", "> "), *_inline_fragments(line[2:], "class:trace.user")]
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
        self.ctx.permissions.quiet = True
        self.ctx.permissions.prompt = self.permission_prompt

        self.trace = TextArea(
            text=self._trace_text(),
            read_only=True,
            focusable=True,
            focus_on_click=True,
            scrollbar=True,
            lexer=TraceLexer(),
            wrap_lines=True,
            style="class:trace",
        )
        self.work = TextArea(
            text=self._work_text(),
            read_only=True,
            focusable=True,
            focus_on_click=True,
            scrollbar=True,
            lexer=TraceLexer(),
            wrap_lines=True,
            style="class:trace",
        )
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
        self._append(value)

    def error(self, message: str) -> None:
        self._append(f"ERROR: {message}")

    def table(self, title: str, columns: list[str], rows: Iterable[Iterable[str]]) -> None:
        self._append(title)
        self._append(" | ".join(columns))
        self._append("-" * min(96, max(8, len(title) + 20)))
        for row in rows:
            self._append(" | ".join(str(item) for item in row))

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
            ("/exit", "quit"),
        ]
        self.table("Command deck", ["Command", "Purpose"], rows)

    def theme_demo(self) -> None:
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
        self.trace.buffer.cursor_position = len(text)
        render_info = self.trace.window.render_info
        height = int(getattr(render_info, "window_height", 0) or 0) if render_info is not None else 0
        if height > 0:
            self.trace.window.vertical_scroll = max(0, len(text.splitlines()) - height)
        self.trace.window.vertical_scroll_2 = 0

    def _refresh(self) -> None:
        text = self._trace_text()
        self.trace.text = text
        if self.auto_scroll:
            self._scroll_trace_to_bottom(text)
        work_text = self._work_text()
        if self.work.text != work_text:
            self.work.text = work_text
            if self.work_auto_scroll:
                self.work.buffer.cursor_position = len(work_text)
        try:
            self.app.invalidate()
        except Exception:
            pass

    def _trace_text(self) -> str:
        return "\n".join(self.lines)

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
            event.app.layout.focus(self.input)

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
        logical_lines = area.text.splitlines() or [""]
        max_scroll = max(0, len(logical_lines) - 1)
        current = int(getattr(area.window, "vertical_scroll", 0) or 0)
        target = max(0, min(max_scroll, current + lines))
        area.window.vertical_scroll = target
        area.window.vertical_scroll_2 = 0
        area.buffer.cursor_position = self._line_start_offset(area.text, target)
        try:
            self.app.invalidate()
        except Exception:
            pass
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

    def _begin_drag_scroll(self, target: str, y: int) -> None:
        self.drag_target = target
        self.drag_last_y = y
        self.auto_scroll = False if target == "trace" else self.auto_scroll
        self.work_auto_scroll = False if target == "work" else self.work_auto_scroll
        self.app.layout.focus(self.trace if target == "trace" else self.work)

    def _drag_scroll(self, target: str, y: int) -> bool:
        if self.drag_target != target:
            return False
        delta = y - self.drag_last_y
        if delta:
            if target == "trace":
                self._scroll_trace(delta)
            else:
                self._scroll_work(delta)
            self.drag_last_y = y
        return True

    def _end_drag_scroll(self, target: str) -> bool:
        if self.drag_target != target:
            return False
        self.drag_target = None
        return True

    def _is_scrollbar_zone(self, area: TextArea, x: int) -> bool:
        render_info = area.window.render_info
        width = getattr(render_info, "window_width", 0) if render_info is not None else 0
        if width <= 0:
            return False
        return x >= max(0, width - 2)

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
            if (
                mouse_event.button == MouseButton.LEFT
                and mouse_event.event_type == MouseEventType.MOUSE_DOWN
                and self._is_scrollbar_zone(self.trace, mouse_event.position.x)
            ):
                self._begin_drag_scroll("trace", mouse_event.position.y)
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_MOVE and self._drag_scroll("trace", mouse_event.position.y):
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._end_drag_scroll("trace"):
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
            if (
                mouse_event.button == MouseButton.LEFT
                and mouse_event.event_type == MouseEventType.MOUSE_DOWN
                and self._is_scrollbar_zone(self.work, mouse_event.position.x)
            ):
                self._begin_drag_scroll("work", mouse_event.position.y)
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_MOVE and self._drag_scroll("work", mouse_event.position.y):
                return None
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self._end_drag_scroll("work"):
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
                subprocess.run(["clip"], input=text, text=True, check=True)
                return True
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
        rows = [
            "### Nebula workplane",
            "",
            *SYSTEM_MAP.splitlines(),
            "",
            "### Active work",
            *(self.work_items or ["No active work."]),
            "",
            "---",
            "F5 focuses Work. Mouse wheel or PageUp/PageDown scrolls the focused pane.",
            "Esc returns to Composer.",
        ]
        return "\n".join(rows)

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
