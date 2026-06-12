from __future__ import annotations

import argparse
import shutil
import shlex
import sys
from pathlib import Path
from typing import Iterable

from .config import LilBotConfig, load_config, save_config
from .core.agent import Agent
from .llm.providers import choose_provider
from .mcp import MCPManager
from .memory import MemoryStore
from .sandbox import PermissionManager, Sandbox
from .skills import SkillRegistry
from .subagents import SubAgentManager
from .tools import ToolContext, ToolRegistry, register_builtins
from .tui.classic import LilBotUI
from .tui.windows_console import configure_windows_console, console_font_status


SUPPORTED_MODELS: dict[str, dict[str, str | tuple[str, ...]]] = {
    "deepseek-v4-flash": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "aliases": ("flash", "deepseek-flash", "v4-flash"),
    },
    "deepseek-v4-pro": {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "aliases": ("pro", "deepseek-pro", "v4-pro"),
    },
}

MODEL_ALIASES = {
    alias: model
    for model, spec in SUPPORTED_MODELS.items()
    for alias in (model, *spec["aliases"])  # type: ignore[misc]
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LilBot local agent")
    parser.add_argument("prompt", nargs="*", help="Prompt for non-interactive use.")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--provider", choices=["auto", "openai", "deepseek", "mock"], default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--permission-mode", choices=["ask", "accept-all", "deny-all"], default=None)
    parser.add_argument("--font-size", type=int, default=None, help="Request a Windows console font size for the TUI.")
    parser.add_argument("--print", action="store_true", dest="print_mode", help="Run one prompt and exit.")
    parser.add_argument("--classic", action="store_true", help="Use the legacy printed Rich interface.")
    parser.add_argument("--no-rich", action="store_true")
    return parser


def build_runtime(cfg: LilBotConfig, ui: LilBotUI, interactive: bool = True) -> tuple[Agent, ToolRegistry, ToolContext]:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    sandbox = Sandbox(cfg.workspace)
    permissions = PermissionManager(
        cfg.state_dir,
        cfg.permission_mode,
        prompt=lambda label: input(label),
        interactive=interactive,
    )
    memory = MemoryStore(cfg.state_dir)
    skills = SkillRegistry(cfg.state_dir)
    provider = choose_provider(cfg)
    subagents = SubAgentManager(lambda messages, tools: provider.complete(messages, tools))
    mcp = MCPManager(cfg.state_dir, cfg.workspace)
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolContext(sandbox, permissions, memory, skills, subagents, mcp, cfg)
    return Agent(cfg, provider, registry, ctx), registry, ctx


def normalize_model_name(value: str) -> str | None:
    key = value.strip().lower()
    return MODEL_ALIASES.get(key)


def model_rows(current_model: str) -> list[tuple[str, str, str]]:
    rows = []
    for model, spec in SUPPORTED_MODELS.items():
        aliases = ", ".join(spec["aliases"])  # type: ignore[arg-type]
        status = "current" if model == current_model else ""
        rows.append((model, aliases, status))
    return rows


def switch_runtime_model(agent: Agent, ctx: ToolContext, requested: str) -> str:
    model = normalize_model_name(requested)
    if not model:
        choices = ", ".join(SUPPORTED_MODELS)
        raise ValueError(f"Unknown model `{requested}`. Available: {choices}")
    spec = SUPPORTED_MODELS[model]
    ctx.config.provider = str(spec["provider"])
    ctx.config.model = model
    ctx.config.base_url = str(spec["base_url"]).rstrip("/")
    save_config(ctx.config)
    agent.config = ctx.config
    agent.provider = choose_provider(ctx.config)
    ctx.subagents.provider = lambda messages, tools: agent.provider.complete(messages, tools)
    return model


def apply_args(cfg: LilBotConfig, args: argparse.Namespace) -> LilBotConfig:
    if args.provider:
        cfg.provider = "auto" if args.provider == "mock" else args.provider
        if args.provider == "mock":
            cfg.api_key = ""
        if args.provider == "deepseek":
            cfg.base_url = "https://api.deepseek.com"
            if cfg.model == "lilbot-rule-model":
                cfg.model = "deepseek-v4-flash"
    if args.model:
        cfg.model = args.model
    if args.base_url:
        cfg.base_url = args.base_url.rstrip("/")
    if args.api_key:
        cfg.api_key = args.api_key
    if args.permission_mode:
        cfg.permission_mode = args.permission_mode
    if args.font_size is not None:
        cfg.font_size = max(0, args.font_size)
    return cfg


def run_prompt(agent: Agent, ui: LilBotUI, prompt: str) -> None:
    for event in agent.run_turn(prompt):
        ui.event(event)


def handle_slash(line: str, agent: Agent, registry: ToolRegistry, ctx: ToolContext, ui: LilBotUI) -> bool:
    if not line.startswith("/"):
        return False
    head, _, tail = line[1:].partition(" ")
    cmd = head.strip().lower()
    args = tail.strip()

    if cmd in {"exit", "quit", "q"}:
        raise KeyboardInterrupt
    if cmd in {"help", "h", ""}:
        ui.help()
        return True
    if cmd == "theme":
        ui.theme_demo()
        return True
    if cmd == "model":
        if not args:
            ui.table("Models", ["Model", "Aliases", "Status"], model_rows(ctx.config.model))
            return True
        try:
            model = switch_runtime_model(agent, ctx, args)
        except ValueError as exc:
            ui.error(str(exc))
            return True
        ui.print(f"Model switched to {model}", "green")
        return True
    if cmd == "tools":
        ui.table("Tools", ["Name", "Description"], [(t.name, t.description) for t in registry.list()])
        return True
    if cmd == "skills":
        ctx.skills.reload()
        ui.table("Skills", ["Name", "Mode", "Description"], [(s.name, s.mode, s.description) for s in ctx.skills.list()])
        return True
    if cmd == "skill":
        if not args:
            ui.error("Usage: /skill NAME ARGS")
            return True
        name, _, skill_args = args.partition(" ")
        try:
            rendered = ctx.skills.render(name, skill_args)
        except KeyError as exc:
            ui.error(str(exc))
            return True
        run_prompt(agent, ui, rendered)
        return True
    if cmd == "memory":
        handle_memory(args, ctx, ui)
        return True
    if cmd == "agents":
        rows = [(d.name, d.description) for d in ctx.subagents.list_types()]
        rows += [(t.id, f"{t.status} {t.agent_type}: {t.prompt[:80]}") for t in ctx.subagents.list_tasks()]
        ui.table("Subagents", ["Name/Task", "Description"], rows)
        return True
    if cmd == "agent":
        agent_type, _, prompt = args.partition(" ")
        if not prompt:
            ui.error("Usage: /agent TYPE PROMPT")
            return True
        task = ctx.subagents.spawn(agent_type, prompt)
        ui.print(f"{task.id} [{task.status}]\n{task.result or task.error}")
        return True
    if cmd == "mcp":
        servers = ctx.mcp.list_servers()
        if not servers:
            path = ctx.mcp.write_example_config()
            ui.print(f"No MCP servers configured. Example config: {path}")
        else:
            ui.table("MCP Servers", ["Name", "Command"], [(s.name, f"{s.command} {' '.join(s.args)}") for s in servers])
        return True
    if cmd == "permissions":
        if not args:
            ui.print(f"Permission mode: {ctx.permissions.mode}")
            return True
        try:
            ctx.permissions.set_mode(args)
            ctx.config.permission_mode = args
            save_config(ctx.config)
            ui.print(f"Permission mode set to {args}", "green")
        except ValueError as exc:
            ui.error(str(exc))
        return True
    if cmd == "compact":
        ui.print(agent.compact(), "green")
        return True
    if cmd == "status":
        ui.table("Status", ["Key", "Value"], [
            ("workspace", str(ctx.config.workspace)),
            ("provider", ctx.config.provider),
            ("model", ctx.config.model),
            ("messages", str(len(agent.messages))),
            ("permission", ctx.permissions.mode),
            ("font_size", str(ctx.config.font_size)),
        ])
        return True
    if cmd == "display":
        font = console_font_status()
        terminal = shutil.get_terminal_size(fallback=(0, 0))
        ui.table("Display", ["Key", "Value"], [
            ("terminal_columns", str(terminal.columns)),
            ("terminal_rows", str(terminal.lines)),
            ("font_requested", str(font.requested_size or ctx.config.font_size)),
            ("font_current", str(font.current_size or "unknown")),
            ("font_face", font.face_name or "unknown"),
            ("font_applied", str(font.applied)),
            ("font_message", font.message),
        ])
        return True
    ui.error(f"Unknown command: /{cmd}. Try /help")
    return True


def handle_memory(args: str, ctx: ToolContext, ui: LilBotUI) -> None:
    action, _, tail = args.partition(" ")
    action = action or "list"
    if action == "list":
        rows = [(e.id, e.kind, e.scope, e.name, e.preview(60)) for e in ctx.memory.list()]
        ui.table("Memory", ["ID", "Kind", "Scope", "Name", "Preview"], rows)
        return
    if action == "search":
        rows = [(e.id, e.name, e.preview(80)) for e in ctx.memory.search(tail)]
        ui.table("Memory Search", ["ID", "Name", "Preview"], rows)
        return
    if action == "save":
        parts = shlex.split(tail)
        if len(parts) < 2:
            ui.error('Usage: /memory save "name" "text"')
            return
        entry = ctx.memory.add(parts[0], " ".join(parts[1:]))
        ui.print(f"Saved {entry.id}", "green")
        return
    if action == "delete":
        ok = ctx.memory.delete(tail.strip())
        ui.print("Deleted." if ok else "Memory not found.", "green" if ok else "red")
        return
    ui.error("Usage: /memory list|search|save|delete")


def interactive_loop(agent: Agent, registry: ToolRegistry, ctx: ToolContext, ui: LilBotUI) -> int:
    ui.banner(str(ctx.config.workspace), ctx.config.provider, ctx.config.model, ctx.config.permission_mode)
    ui.help(compact=True)
    while True:
        try:
            line = ui.prompt().strip()
            if not line:
                continue
            if handle_slash(line, agent, registry, ctx, ui):
                continue
            run_prompt(agent, ui, line)
        except KeyboardInterrupt:
            ui.print("\nbye", "dim")
            return 0
        except EOFError:
            ui.print("\nbye", "dim")
            return 0
        except Exception as exc:  # pragma: no cover - interactive safety net
            ui.error(f"{type(exc).__name__}: {exc}")


def main(argv: Iterable[str] | None = None) -> int:
    configure_windows_console()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    cfg = load_config(args.workspace)
    cfg = apply_args(cfg, args)
    ui = LilBotUI(enabled=not args.no_rich)
    one_shot = args.print_mode or bool(args.prompt)
    agent, registry, ctx = build_runtime(cfg, ui, interactive=not one_shot)
    if one_shot:
        prompt = " ".join(args.prompt)
        if not prompt:
            parser.error("--print requires a prompt or positional prompt text")
        run_prompt(agent, ui, prompt)
        return 0
    can_use_dashboard = sys.stdin.isatty() and sys.stdout.isatty()
    if not args.classic and not args.no_rich and can_use_dashboard:
        try:
            configure_windows_console(font_size=cfg.font_size)
            from .tui.dashboard import DashboardUI

            return DashboardUI(agent, registry, ctx).run()
        except Exception as exc:
            ui.error(f"Dashboard unavailable ({type(exc).__name__}); falling back to classic UI.")
    return interactive_loop(agent, registry, ctx, ui)
