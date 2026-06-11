from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from typing import Iterable

from ..core.agent import Agent
from ..cli import handle_slash, run_prompt
from ..core.events import TextDelta, ToolFinished, ToolStarted, TurnFinished
from ..tools import ToolContext, ToolRegistry

try:
    from prompt_toolkit import Application
    from prompt_toolkit.application import run_in_terminal
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.lexers import Lexer
    from prompt_toolkit.mouse_events import MouseButton, MouseEventType
    from prompt_toolkit.styles import Style
    from prompt_toolkit.widgets import Box, Frame, TextArea
except ImportError:  # pragma: no cover - optional dependency fallback
    Application = None


PIXEL_FONT = {
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
}

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


STYLE = Style.from_dict(
    {
        "root": "bg:#12091f #f8d8ec",
        "topbar": "bg:#211234 #ffd6ea bold",
        "topbar.dim": "bg:#211234 #bda4df",
        "frame": "bg:#170d29 #f8d8ec",
        "frame.border": "#d8b4fe",
        "frame.label": "#f9a8d4 bold",
        "logo": "#f9a8d4 bold",
        "logo.shadow": "#8b5cf6 bold",
        "signature": "#f0abfc bold",
        "accent": "#d8b4fe bold",
        "muted": "#b8a6d9",
        "deep": "#93c5fd",
        "ok": "#86efac bold",
        "warn": "#fde68a bold",
        "error": "#fca5a5 bold",
        "composer": "bg:#211234 #ffe4f1",
        "composer.prompt": "bg:#211234 #f9a8d4 bold",
        "toolbar": "bg:#211234 #c4b5fd",
        "hotkey": "bg:#211234 #f9a8d4 bold",
        "wave": "bg:#211234 #f9a8d4 bold",
        "trace": "bg:#0f1226 #f8d8ec",
        "trace.user": "bg:#0f1226 #f9a8d4 bold",
        "trace.agent": "bg:#0f1226 #f8d8ec",
        "trace.agent.label": "bg:#0f1226 #93c5fd bold",
        "trace.heading": "bg:#0f1226 #93c5fd bold",
        "trace.bold": "bg:#0f1226 #ffffff bold",
        "trace.dim": "bg:#0f1226 #8d7aa8",
        "trace.code": "bg:#1e1530 #c4b5fd italic",
        "trace.code.inline": "bg:#26304a #fde68a",
        "trace.bullet": "bg:#0f1226 #93c5fd",
        "trace.table": "bg:#0f1226 #c4b5fd",
        "trace.tool": "bg:#0f1226 #d8b4fe",
        "trace.tool.rail": "bg:#0f1226 #8b5cf6 bold",
        "trace.tool.ok": "bg:#0f1226 #86efac bold",
        "trace.tool.error": "bg:#0f1226 #fca5a5 bold",
        "trace.separator": "bg:#0f1226 #6d5c85",
        "selected": "bg:#5b2c78 #fff5fb",
        "scrollbar.background": "bg:#1a1230",
        "scrollbar.button": "bg:#d8b4fe",
        "scrollbar.arrow": "#f9a8d4 bold",
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
    if stripped.startswith("|") or " | " in line:
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
            "Trace owns selection and scrolling: drag inside Trace, Ctrl+C/F2 to copy, PageUp/PageDown to scroll.",
        ]
        self.work_items: list[str] = ["No active work."]
        self.tool_count = 0
        self.busy = False
        self.wave_index = 0
        self.auto_scroll = True
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
        self.input = TextArea(
            height=3,
            multiline=False,
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
            self._append("")
            self._append("LILBOT")
            self._append(event.text)
        elif isinstance(event, ToolStarted):
            self.tool_count += 1
            stamp = datetime.now().strftime("%H%M%S")
            args = json.dumps(event.arguments, ensure_ascii=False)
            self.work_items = [
                f"step {self.tool_count}",
                f"tool  {event.name}",
                f"args  {event.arguments}",
            ]
            self._append("")
            self._append(f"╭─ ▷ run {stamp}-{self.tool_count:02d}  {event.name}")
            self._append(f"│ args {args}")
        elif isinstance(event, ToolFinished):
            mark = "done" if event.ok else "error"
            self._append(f"╰─ {mark} {event.name} {event.elapsed_ms}ms")
            if event.output:
                self._append(event.output)
            self.work_items = [f"{mark} {event.name}", f"{event.elapsed_ms}ms"]
        elif isinstance(event, TurnFinished):
            self._append(f"completed {event.steps} steps")
            self.work_items = ["No active work."]

    def permission_prompt(self, label: str) -> str:
        holder: list[str] = []

        def ask() -> None:
            print()
            print("Permission required inside LilBot dashboard.")
            print("y = allow once, a = always allow, n = deny once, d = always deny")
            holder.append(input(label))

        run_in_terminal(ask)
        return holder[0] if holder else "n"

    def _accept(self, buffer) -> bool:
        line = buffer.text.strip()
        buffer.text = ""
        if not line:
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
        self._append(f"> {line}")
        threading.Thread(target=self._process_line, args=(line,), daemon=True).start()
        return False

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
        self.lines = self.lines[-700:]
        self._refresh()

    def _refresh(self) -> None:
        text = self._trace_text()
        self.trace.text = text
        if self.auto_scroll:
            self.trace.buffer.cursor_position = len(text)
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
            if event.app.layout.has_focus(self.trace) and self.trace.buffer.selection_state is not None:
                self._copy_trace(selection_first=True)
                return
            event.app.exit(result=0)

        @kb.add("escape")
        def _focus_input(event) -> None:
            event.app.layout.focus(self.input)

        @kb.add("c-v")
        def _paste(event) -> None:
            event.app.layout.focus(self.input)
            self.input.buffer.insert_text(self._read_clipboard())

        @kb.add("f2")
        def _copy(event) -> None:
            self._copy_trace(selection_first=True)

        @kb.add("f3")
        def _copy_all(event) -> None:
            self._copy_trace(selection_first=False)

        @kb.add("f4")
        def _focus_trace(event) -> None:
            event.app.layout.focus(self.trace)

        @kb.add("pageup")
        def _page_up(event) -> None:
            self._scroll_trace(-18)

        @kb.add("pagedown")
        def _page_down(event) -> None:
            self._scroll_trace(18)

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
        if lines < 0:
            self.trace.buffer.cursor_up(count=abs(lines))
        else:
            self.trace.buffer.cursor_down(count=lines)
            if self.trace.buffer.cursor_position >= len(self.trace.text):
                self.auto_scroll = True

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

        def composer_mouse(mouse_event):
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
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                self.auto_scroll = False
            if mouse_event.button == MouseButton.RIGHT and mouse_event.event_type == MouseEventType.MOUSE_UP:
                self._copy_trace(selection_first=True)
                return None
            return trace_mouse_handler(mouse_event)

        self.input.control.mouse_handler = composer_mouse
        self.trace.control.mouse_handler = trace_mouse

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
                    Box(Window(FormattedTextControl(self._logo), wrap_lines=False), padding=1),
                    title="  >_ lilbot  ",
                    style="class:frame",
                    height=Dimension(min=18, preferred=22, max=26),
                ),
                Frame(
                    Box(Window(FormattedTextControl(self._work), wrap_lines=True), padding=1),
                    title="  Work  ",
                    style="class:frame",
                    height=Dimension(weight=1),
                ),
            ],
            width=Dimension(weight=2),
        )

        main_area = VSplit(
            [
                left_column,
                Frame(
                    self.trace,
                    title="  Trace  ",
                    style="class:frame",
                    width=Dimension(weight=5),
                ),
            ],
            height=Dimension(weight=1),
        )

        return HSplit(
            [
                Window(FormattedTextControl(self._topbar), height=1, style="class:topbar"),
                main_area,
                Frame(self.input, title="  Composer  ", style="class:frame", height=5),
                Window(FormattedTextControl(self._toolbar), height=1, style="class:toolbar"),
            ],
            style="class:root",
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

    def _logo(self):
        width = self._width()
        logo = self._pixel_logo("LILBOT", scale_x=2, scale_y=2)
        if width < 120:
            logo = self._pixel_logo("LILBOT", scale_x=1, scale_y=1)
        return FormattedText(
            [
                ("class:logo.shadow", logo.replace("\u2588", "\u2593")),
                ("class:signature", "\n\nTerrence Shen  //  China  //  Deeplearningman0723@gmail.com\n"),
                ("class:accent", "\n>_ clean-room local coding agent\n"),
                ("class:muted", "model: "),
                ("class:deep", self.ctx.config.model),
                ("class:muted", "    provider: "),
                ("class:deep", self.ctx.config.provider),
                ("class:muted", "    permissions: "),
                ("class:ok", self.ctx.permissions.mode),
                ("class:muted", "\ndirectory: "),
                ("class:accent", str(self.ctx.config.workspace)),
            ]
        )

    def _pixel_logo(self, text: str, scale_x: int, scale_y: int) -> str:
        filled = "\u2588" * scale_x
        empty = " " * scale_x
        gap = " " * max(1, scale_x)
        rows: list[str] = []
        for row_idx in range(7):
            pieces = []
            for char in text:
                pattern = PIXEL_FONT.get(char.upper())
                if not pattern:
                    pieces.append(empty * 3)
                    continue
                pieces.append("".join(filled if bit == "1" else empty for bit in pattern[row_idx]))
            line = gap.join(pieces).rstrip()
            for _ in range(scale_y):
                rows.append(line)
        return "\n".join(rows)

    def _work(self):
        work = "\n".join(self.work_items)
        return FormattedText(
            [
                ("class:accent", "Nebula workplane\n\n"),
                ("class:muted", SYSTEM_MAP),
                ("class:deep", "\nActive work\n"),
                ("class:ok", work),
            ]
        )

    def _toolbar(self):
        if self.busy:
            frame = WAVE_FRAMES[self.wave_index % len(WAVE_FRAMES)]
            self.wave_index += 1
            return FormattedText(
                [
                    ("class:wave", f" thinking {frame} "),
                    ("class:toolbar", " DeepSeek turn running   "),
                    ("class:hotkey", " F2 "),
                    ("class:toolbar", " copy selection/trace   "),
                    ("class:hotkey", " F4 "),
                    ("class:toolbar", " focus trace   "),
                    ("class:hotkey", " Ctrl+C "),
                    ("class:toolbar", " exit "),
                ]
            )
        return FormattedText(
            [
                ("class:hotkey", " /help "),
                ("class:toolbar", " commands   "),
                ("class:hotkey", " /copy/F2 "),
                ("class:toolbar", " copy trace   "),
                ("class:hotkey", " F4/PageUp/PageDown "),
                ("class:toolbar", " trace nav   "),
                ("class:hotkey", " right-click "),
                ("class:toolbar", " paste in composer/copy trace selection   "),
                ("class:hotkey", " /theme "),
                ("class:toolbar", " blush/violet   "),
                ("class:hotkey", " Ctrl+C "),
                ("class:toolbar", " exit "),
            ]
        )
