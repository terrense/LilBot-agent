from __future__ import annotations

import sys
from typing import Iterable

from .events import TextDelta, ToolFinished, ToolStarted, TurnFinished

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:  # pragma: no cover - fallback for bare Python
    Console = None
    Panel = None
    Table = None
    Text = None


LOGO = r"""
 _     _ _ ____        _
| |   (_) | __ )  ___ | |_
| |   | | |  _ \ / _ \| __|
| |___| | | |_) | (_) | |_
|_____|_|_|____/ \___/ \__|
"""


class LilBotUI:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled and Console is not None
        self.console = Console() if self.enabled else None

    def banner(self, workspace: str, provider: str, model: str, permission_mode: str) -> None:
        if self.enabled:
            text = Text(LOGO, style="bold cyan")
            text.append(f"\nworkspace  {workspace}\n", style="dim")
            text.append(f"provider   {provider}   model {model}   permissions {permission_mode}", style="green")
            self.console.print(Panel(text, title="LilBot", border_style="bright_cyan"))
        else:
            print(LOGO)
            print(f"workspace {workspace}")
            print(f"provider {provider} model {model} permissions {permission_mode}")

    def prompt(self) -> str:
        if self.enabled:
            return self.console.input("[bold yellow]lilbot> [/]")
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
            table = Table(title=title, show_lines=False)
            for column in columns:
                table.add_column(column)
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
            self.print(event.text)
        elif isinstance(event, ToolStarted):
            self.print(f"tool {event.name} {event.arguments}", "cyan")
        elif isinstance(event, ToolFinished):
            mark = "ok" if event.ok else "error"
            style = "green" if event.ok else "red"
            self.print(f"{mark} {event.name} {event.elapsed_ms}ms", style)
            if event.output:
                self.print(event.output)
        elif isinstance(event, TurnFinished):
            self.print(f"completed {event.steps} steps", "green")

    def error(self, message: str) -> None:
        if self.enabled:
            self.console.print(message, style="bold red")
        else:
            print(message, file=sys.stderr)

    def help(self) -> None:
        rows = [
            ("/help", "show commands"),
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
            ("! command", "run shell command through permission gate"),
            ("/exit", "quit"),
        ]
        self.table("Commands", ["Command", "Meaning"], rows)

