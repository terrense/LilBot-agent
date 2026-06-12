from __future__ import annotations

import sys
from datetime import datetime
from typing import Iterable

from ..core.events import TextDelta, ToolFinished, ToolStarted, TurnFinished

try:
    from rich import box
    from rich.align import Align
    from rich.columns import Columns
    from rich.console import Console
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
except ImportError:  # pragma: no cover - fallback for bare Python
    Align = None
    Console = None
    Panel = None
    Table = None
    Text = None
    Syntax = None
    box = None


LOGO = r"""
 __       __  __       ____    ____   ______
/ /      / / / /      / __ )  / __ \ /_  __/
/ /      / / / /      / __  | / / / /  / /
/ /___  / / / /___   / /_/ / / /_/ /  / /
/____/ /_/ /_____/  /_____/  \____/  /_/
"""

SPARK = r"""
        .             *                 .
   ____        ____              __
 _/    \__  __/    \__       __/  \__
      workspace      \______/  tool bus

        [memory] -> [skills] -> [agents] -> [mcp]
"""

DIFF_PREVIEW = """\
  1  function greet() {
- 2    console.log("Hello, World!");
+ 2    console.log("Hello, LilBot!");
  3  }
"""


class LilBotUI:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled and Console is not None
        self.console = Console(highlight=False) if self.enabled else None
        self.tool_count = 0

    @property
    def _box(self):
        if not self.enabled:
            return None
        return box.HEAVY if self.console.encoding.lower().replace("-", "") == "utf8" else box.ASCII

    def banner(self, workspace: str, provider: str, model: str, permission_mode: str) -> None:
        if self.enabled:
            self.console.clear()
            top = Table.grid(expand=True)
            top.add_column(justify="left", ratio=1)
            top.add_column(justify="right")
            top.add_row(
                "[bold bright_cyan]Agent[/] [white]LilBot-agent-code[/] "
                f"[dim]- {model}[/]",
                "[bright_cyan]ready[/] [dim]ctx 0%[/] [blue]====[/] [dim]v0.1.0[/]",
            )

            logo = Text(LOGO, style="bold bright_cyan")
            logo.append("\n>_ clean-room local coding agent", style="bold yellow")
            logo.append("\nmodel: ", style="dim")
            logo.append(model, style="white")
            logo.append("    provider: ", style="dim")
            logo.append(provider, style="white")
            logo.append("    permissions: ", style="dim")
            logo.append(permission_mode, style="green")
            logo.append("\ndirectory: ", style="dim")
            logo.append(workspace, style="white")

            orbit = Text(SPARK, style="bright_blue")
            orbit.append("\n  live tools: file | bash | memory | skill | subagent | mcp", style="dim")

            left = Panel(
                Align.left(logo),
                title="[bold yellow]>_ lilbot[/]",
                border_style="bright_cyan",
                box=self._box,
                padding=(1, 2),
            )
            right = Panel(
                orbit,
                title="[bold yellow]Work[/]",
                subtitle="[dim]No active work[/]",
                border_style="blue",
                box=self._box,
                padding=(1, 2),
            )

            syntax = Syntax(DIFF_PREVIEW, "diff", theme="monokai", word_wrap=False)
            preview = Panel(
                syntax,
                title="[bold yellow]Theme preview[/]",
                subtitle="[dim]/theme to view modes[/]",
                border_style="yellow",
                box=self._box,
            )

            composer = Panel(
                "[italic]Write a task, use /, or run ! command through the permission gate.[/]",
                title="[bold]Composer[/]",
                border_style="bright_blue",
                box=self._box,
            )

            self.console.print(Panel(top, border_style="blue", box=self._box))
            self.console.print(Columns([left, right], equal=True, expand=True))
            self.console.print(preview)
            self.console.print(composer)
        else:
            print(LOGO)
            print(f"workspace {workspace}")
            print(f"provider {provider} model {model} permissions {permission_mode}")

    def prompt(self) -> str:
        if self.enabled:
            return self.console.input("[bold bright_cyan]LilBot[/][dim] / [/][bold yellow]compose> [/]")
        return input("lilbot> ")

    def print(self, value: str = "", style: str | None = None) -> None:
        if self.enabled:
            self.console.print(value, style=style)
        else:
            print(value)

    def rule(self, title: str) -> None:
        if self.enabled:
            self.console.rule(title, style="dim")
        else:
            print(f"--- {title} ---")

    def table(self, title: str, columns: list[str], rows: Iterable[Iterable[str]]) -> None:
        if self.enabled:
            table = Table(title=title, show_lines=False, border_style="blue", box=self._box)
            for column in columns:
                table.add_column(column, style="white")
            for row in rows:
                table.add_row(*[str(item) for item in row])
            self.console.print(table)
            return
        print(title)
        print(" | ".join(columns))
        for row in rows:
            print(" | ".join(str(item) for item in row))

    def event(self, event: object) -> None:
        if isinstance(event, TextDelta):
            if self.enabled:
                self.console.print(Panel(event.text, title="[bold bright_cyan]LilBot[/]", border_style="bright_cyan", box=self._box))
            else:
                self.print(event.text)
        elif isinstance(event, ToolStarted):
            self.tool_count += 1
            if self.enabled:
                timestamp = datetime.now().strftime("%H%M%S")
                body = Text()
                body.append(f"run {timestamp}-{self.tool_count:02d}  ", style="dim")
                body.append(event.name, style="bold bright_cyan")
                body.append(f"  {event.arguments}", style="white")
                self.console.print(Panel(body, title=f"[bold]step {self.tool_count}[/]", border_style="cyan", box=self._box))
            else:
                self.print(f"tool {event.name} {event.arguments}", "cyan")
        elif isinstance(event, ToolFinished):
            mark = "ok" if event.ok else "error"
            style = "green" if event.ok else "red"
            if self.enabled:
                title = f"[bold {style}]{mark}[/] [white]{event.name}[/] [dim]{event.elapsed_ms}ms[/]"
                renderable = Syntax(event.output, "diff", theme="monokai") if event.output.startswith("---") else event.output
                self.console.print(Panel(renderable or "(no output)", title=title, border_style=style, box=self._box))
            else:
                self.print(f"{mark} {event.name} {event.elapsed_ms}ms", style)
                if event.output:
                    self.print(event.output)
        elif isinstance(event, TurnFinished):
            self.print(f"completed {event.steps} steps", "bold green")

    def error(self, message: str) -> None:
        if self.enabled:
            self.console.print(message, style="bold red")
        else:
            print(message, file=sys.stderr)

    def help(self, compact: bool = False) -> None:
        rows = [
            ("/help", "show commands"),
            ("/theme", "show theme preview"),
            ("/model [flash|pro]", "switch DeepSeek model"),
            ("/tools", "list tools"),
            ("/skills", "list skills"),
            ("/skill NAME ARGS", "render and run a skill"),
            ("/memory list|search|save|delete", "manage project memory"),
            ("/agents", "list sub-agent types and tasks"),
            ("/agent TYPE PROMPT", "run a sub-agent"),
            ("/mcp", "list MCP-style servers"),
            ("/permissions MODE", "ask, accept-all, or deny-all"),
            ("/compact", "compact conversation"),
            ("/status", "show session status"),
            ("/display", "show terminal and font diagnostics"),
            ("! command", "run shell command through permission gate"),
            ("/exit", "quit"),
        ]
        if compact and self.enabled:
            body = "  ".join(f"[bright_cyan]{cmd}[/] [dim]{meaning}[/]" for cmd, meaning in rows[:8])
            self.console.print(Panel(body, title="[bold yellow]Hot commands[/]", border_style="yellow", box=self._box))
            return
        self.table("Commands", ["Command", "Meaning"], rows)

    def theme_demo(self) -> None:
        if not self.enabled:
            print("Themes: dark, light, dark-colorblind, light-colorblind, ansi-dark, ansi-light")
            return
        modes = [
            ("1", "Dark mode", "selected"),
            ("2", "Light mode", ""),
            ("3", "Dark mode (colorblind-friendly)", ""),
            ("4", "Light mode (colorblind-friendly)", ""),
            ("5", "Dark mode (ANSI colors only)", ""),
            ("6", "Light mode (ANSI colors only)", ""),
        ]
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bright_cyan", justify="right")
        table.add_column(style="white")
        table.add_column(style="green")
        for number, label, status in modes:
            table.add_row(number, label, "OK" if status else "")
        self.console.print(Panel(table, title="[bold yellow]Choose the text style that looks best[/]", border_style="bright_cyan", box=self._box))
        self.console.print(Panel(Syntax(DIFF_PREVIEW, "diff", theme="monokai"), title="[bold]Syntax theme: Monokai Extended[/]", border_style="yellow", box=self._box))
