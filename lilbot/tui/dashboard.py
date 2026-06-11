from __future__ import annotations

import json
import os
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
    }
)


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
            "Right-click paste works in the Composer. Select Trace text to copy, or use /copy.",
        ]
        self.work_items: list[str] = ["No active work."]
        self.tool_count = 0
        self.busy = False
        self.wave_index = 0
        self.ctx.permissions.prompt = self.permission_prompt

        self.trace = TextArea(
            text=self._trace_text(),
            read_only=True,
            scrollbar=True,
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
            mouse_support=False,
            refresh_interval=0.16,
        )

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
            ("/copy", "copy Trace to clipboard"),
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
            self.work_items = [
                f"step {self.tool_count}",
                f"tool  {event.name}",
                f"args  {event.arguments}",
            ]
            self._append(f"run {stamp}-{self.tool_count:02d}  tool {event.name} {event.arguments}")
        elif isinstance(event, ToolFinished):
            mark = "done" if event.ok else "error"
            self._append(f"{mark} {event.name} {event.elapsed_ms}ms")
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
        self.trace.text = self._trace_text()
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
            event.app.exit(result=0)

        @kb.add("escape")
        def _focus_input(event) -> None:
            event.app.layout.focus(self.input)

        @kb.add("c-v")
        def _paste(event) -> None:
            event.app.current_buffer.insert_text(self._read_clipboard())

        @kb.add("f2")
        def _copy(event) -> None:
            self._copy_trace()

        return kb

    def _copy_trace(self) -> None:
        text = self._trace_text()
        ok = self._write_clipboard(text)
        self._append("Trace copied to clipboard." if ok else "Clipboard copy failed; select Trace text manually.")

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
                    ("class:toolbar", " copy trace   "),
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
                ("class:hotkey", " right-click "),
                ("class:toolbar", " paste/select   "),
                ("class:hotkey", " /theme "),
                ("class:toolbar", " blush/violet   "),
                ("class:hotkey", " Ctrl+C "),
                ("class:toolbar", " exit "),
            ]
        )
