from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .agent import Agent
from .cli import handle_slash, run_prompt
from .events import TextDelta, ToolFinished, ToolStarted, TurnFinished
from .tools import ToolContext, ToolRegistry

try:
    from prompt_toolkit import Application
    from prompt_toolkit.application import run_in_terminal
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.styles import Style
    from prompt_toolkit.widgets import Box, Frame, TextArea
except ImportError:  # pragma: no cover - optional dependency fallback
    Application = None


BIG_LOGO = r"""
██╗     ██╗██╗     ██████╗  ██████╗ ████████╗
██║     ██║██║     ██╔══██╗██╔═══██╗╚══██╔══╝
██║     ██║██║     ██████╔╝██║   ██║   ██║
██║     ██║██║     ██╔══██╗██║   ██║   ██║
███████╗██║███████╗██████╔╝╚██████╔╝   ██║
╚══════╝╚═╝╚══════╝╚═════╝  ╚═════╝    ╚═╝
"""

SMALL_LOGO = r"""
 _     _ _ ____        _
| |   (_) | __ )  ___ | |_
| |   | | |  _ \ / _ \| __|
| |___| | | |_) | (_) | |_
|_____|_|_|____/ \___/ \__|
"""

SYSTEM_MAP = """\
memory core      skill deck       subagent bay       mcp dock
    │                │                 │                │
    └────── tool registry ─────────────┴──── permission gate
                         │
                    workspace sandbox
"""


STYLE = Style.from_dict(
    {
        "root": "bg:#130a22 #f7d6e8",
        "topbar": "bg:#25143f #ffd1e8 bold",
        "topbar.dim": "bg:#25143f #b8a6d9",
        "frame": "bg:#170d29 #f7d6e8",
        "frame.border": "#d8b4fe",
        "frame.label": "#f9a8d4 bold",
        "frame.shadow": "bg:#0f172a",
        "logo": "#f9a8d4 bold",
        "logo.shadow": "#a78bfa bold",
        "accent": "#c4b5fd bold",
        "muted": "#9ca3af",
        "deep": "#60a5fa",
        "ok": "#86efac bold",
        "warn": "#fde68a bold",
        "error": "#fca5a5 bold",
        "composer": "bg:#211234 #ffe4f1",
        "composer.prompt": "bg:#211234 #f9a8d4 bold",
        "toolbar": "bg:#25143f #c4b5fd",
        "hotkey": "bg:#25143f #f9a8d4 bold",
        "trace": "bg:#0f172a #f7d6e8",
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
            "Use /help, /theme, /tools, /skills, or type a task.",
        ]
        self.work_items: list[str] = ["No active work."]
        self.tool_count = 0
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
            mouse_support=True,
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
        self._append("3  midnight blue     soon")
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
        self._append(f"> {line}")
        try:
            if line in {"/exit", "/quit", "/q"}:
                self.app.exit(result=0)
                return False
            if not handle_slash(line, self.agent, self.registry, self.ctx, self):
                run_prompt(self.agent, self, line)
        except KeyboardInterrupt:
            self.app.exit(result=0)
        except Exception as exc:  # pragma: no cover - interactive guard
            self.error(f"{type(exc).__name__}: {exc}")
        self._refresh()
        return False

    def _append(self, text: str) -> None:
        for line in str(text).splitlines() or [""]:
            self.lines.append(line)
        self.lines = self.lines[-500:]
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

        return kb

    def _root_container(self):
        return HSplit(
            [
                Window(FormattedTextControl(self._topbar), height=1, style="class:topbar"),
                VSplit(
                    [
                        Frame(
                            Box(Window(FormattedTextControl(self._logo), wrap_lines=False), padding=1),
                            title="  >_ lilbot  ",
                            style="class:frame",
                            width=Dimension(weight=3),
                        ),
                        Frame(
                            Box(Window(FormattedTextControl(self._work), wrap_lines=True), padding=1),
                            title="  Work  ",
                            style="class:frame",
                            width=Dimension(weight=2),
                        ),
                    ],
                    height=Dimension(weight=3),
                ),
                Frame(self.trace, title="  Trace  ", style="class:frame", height=Dimension(weight=2)),
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
        text = f" Agent  LilBot-agent-code  ·  {cfg.model}  ·  {cfg.provider} "
        right = f" permissions {self.ctx.permissions.mode}  ·  v0.1 "
        width = self._width()
        gap = max(1, width - len(text) - len(right))
        return FormattedText(
            [
                ("class:topbar", text),
                ("class:topbar.dim", " " * gap),
                ("class:topbar", right),
            ]
        )

    def _logo(self):
        width = self._width()
        logo = SMALL_LOGO if width < 110 else BIG_LOGO
        return FormattedText(
            [
                ("class:logo.shadow", logo.replace("█", "▓")),
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
        return FormattedText(
            [
                ("class:hotkey", " /help "),
                ("class:toolbar", " commands   "),
                ("class:hotkey", " /theme "),
                ("class:toolbar", " blush/violet deck   "),
                ("class:hotkey", " /tools "),
                ("class:toolbar", " tool bus   "),
                ("class:hotkey", " /skills "),
                ("class:toolbar", " skill deck   "),
                ("class:hotkey", " Ctrl+C "),
                ("class:toolbar", " exit "),
            ]
        )

