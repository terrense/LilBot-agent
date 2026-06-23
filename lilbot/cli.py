from __future__ import annotations

import argparse
import shutil
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import LilBotConfig, load_config, save_config
from .core.agent import Agent
from .llm.providers import choose_provider
from .mcp import MCPManager
from .memory import FileMemoryStore, MemoryStore
from .sandbox import PermissionManager, Sandbox
from .skills import SkillRegistry
from .subagents import SubAgentManager
from .teams.manager import TeamManager
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


@dataclass(frozen=True)
class SlashCommandInfo:
    name: str
    usage: str
    description: str
    aliases: tuple[str, ...] = ()
    type: str = "local"
    hidden: bool = False

    @property
    def palette_text(self) -> str:
        return f"/{self.name} " if any(token in self.usage for token in ("[", "<")) else f"/{self.name}"


SLASH_COMMANDS: tuple[SlashCommandInfo, ...] = (
    SlashCommandInfo("help", "/help [command]", "Show commands and short help.", ("?", "h")),
    SlashCommandInfo("clear", "/clear", "Clear Trace and start a fresh local conversation.", ("cls",), "local-ui"),
    SlashCommandInfo("theme", "/theme", "Show the current visual theme preview.", type="local-ui"),
    SlashCommandInfo("model", "/model [flash|pro]", "Switch or view the current DeepSeek model.", ("moxing",)),
    SlashCommandInfo("models", "/models", "List available models.", ("moxingliebiao",)),
    SlashCommandInfo("tools", "/tools", "List registered tools grouped by capability."),
    SlashCommandInfo("skills", "/skills", "List bundled and installed skills."),
    SlashCommandInfo("skill", "/skill NAME ARGS", "Render and run a skill prompt.", type="prompt"),
    SlashCommandInfo("memory", "/memory list|search|save|delete", "Manage project memory."),
    SlashCommandInfo("agents", "/agents", "List sub-agent types and tasks."),
    SlashCommandInfo("agent", "/agent TYPE PROMPT", "Run a sub-agent task."),
    SlashCommandInfo("team", "/team list|new NAME|msg NAME TEXT|rm NAME", "Inspect or drive agent teams.", ("teams",)),
    SlashCommandInfo("mcp", "/mcp", "List MCP-style external servers."),
    SlashCommandInfo("permissions", "/permissions ask|accept-all|deny-all", "Change permission mode."),
    SlashCommandInfo("compact", "/compact", "Compact conversation context."),
    SlashCommandInfo("sessions", "/sessions", "List saved sessions you can resume."),
    SlashCommandInfo("resume", "/resume [id]", "Resume a saved session (latest if no id)."),
    SlashCommandInfo("history", "/history", "List recent file edits the agent made."),
    SlashCommandInfo("rewind", "/rewind [n]", "Undo the last n file edits (default 1)."),
    SlashCommandInfo("status", "/status", "Show session status."),
    SlashCommandInfo("tokens", "/tokens", "Show local token and context usage.", ("usage", "token")),
    SlashCommandInfo("plan", "/plan [task]", "Enter Plan Mode locally; optional task is sent to Agent.", ("p",), "local-ui"),
    SlashCommandInfo("do", "/do [approved|rejected]", "Exit Plan Mode and record the approval state.", ("execute",), "local-ui"),
    SlashCommandInfo("review", "/review [focus]", "Ask Agent to review the current git diff.", type="prompt"),
    SlashCommandInfo("display", "/display", "Show terminal and font diagnostics."),
    SlashCommandInfo("copy", "/copy", "Copy Trace to clipboard.", type="local-ui"),
    SlashCommandInfo("exit", "/exit", "Quit LilBot.", ("quit", "q"), "local-ui"),
)


def slash_commands_matching(prefix: str) -> list[SlashCommandInfo]:
    query = prefix.strip().lstrip("/").lower()
    matches = [
        command
        for command in SLASH_COMMANDS
        if not command.hidden
        and (
            not query
            or command.name.startswith(query)
            or any(alias.startswith(query) for alias in command.aliases)
        )
    ]
    return sorted(matches, key=lambda command: (not command.name.startswith(query), command.name))


def resolve_slash_command(name: str) -> SlashCommandInfo | None:
    query = name.strip().lstrip("/").lower()
    if not query:
        return next(command for command in SLASH_COMMANDS if command.name == "help")
    for command in SLASH_COMMANDS:
        if command.name == query or query in command.aliases:
            return command
    return None


def slash_command_runs_agent(line: str) -> bool:
    """Return True when a slash command intentionally enters the Agent Loop."""
    if not line.startswith("/"):
        return False
    head, _, tail = line[1:].partition(" ")
    command = resolve_slash_command(head)
    if command is None:
        return False
    return command.type == "prompt" or (command.name == "plan" and bool(tail.strip()))


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
    parser.add_argument("--resume", nargs="?", const="__latest__", default=None,
                        help="Resume a saved session. Bare --resume resumes the most recent; "
                             "pass a session id to resume a specific one.")
    parser.add_argument("--mcp-server", action="store_true", dest="mcp_server",
                        help="Run as an MCP server over stdio, exposing LilBot's tools to other "
                             "MCP clients (read-only tools by default).")
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
    memory = FileMemoryStore(cfg.state_dir)
    # One-time migration: import any legacy JSONL memories into the file store.
    legacy = MemoryStore(cfg.state_dir)
    if (cfg.state_dir / "memory.jsonl").exists():
        try:
            memory.import_from(legacy)
            (cfg.state_dir / "memory.jsonl").rename(cfg.state_dir / "memory.jsonl.migrated")
        except OSError:
            pass
    skills = SkillRegistry(cfg.state_dir)
    provider = choose_provider(cfg)
    subagents = SubAgentManager(
        lambda messages, tools: provider.complete(messages, tools),
        cfg.state_dir / "agents",
        max_concurrent=cfg.subagent_max_concurrent,
    )
    mcp = MCPManager(cfg.state_dir, cfg.workspace)
    teams = TeamManager(cfg.state_dir)
    registry = ToolRegistry()
    register_builtins(registry)
    ctx = ToolContext(sandbox, permissions, memory, skills, subagents, mcp, cfg, teams)
    subagents.configure_tools(registry, ctx)
    # M7: connect configured MCP servers and register their tools as first-class
    # deferred tools (mcp__<server>__<tool>). Best-effort — never blocks startup.
    try:
        registered = mcp.connect_and_register(registry)
        if registered and getattr(cfg, "verbose", False):
            ui.print(f"MCP: registered {registered} tool(s) from configured servers.", "dim")
        for err in getattr(mcp, "connect_errors", []):
            if getattr(cfg, "verbose", False):
                ui.print(f"MCP: {err}", "yellow")
    except Exception:
        pass
    agent = Agent(cfg, provider, registry, ctx)
    agent.agent_id = "lead"
    return agent, registry, ctx


def maybe_resume(agent: Agent, ui: LilBotUI, resume_arg: str | None) -> None:
    if not resume_arg:
        return
    session_id = None if resume_arg == "__latest__" else resume_arg
    ui.print(agent.resume(session_id), "green")


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


REVIEW_PROMPT = """Review the current workspace changes like a senior code reviewer.

Prioritize bugs, behavioral regressions, security/safety risks, and missing tests.
Inspect the relevant git diff and files before concluding. Lead with findings ordered
by severity and include file/line references where possible. If there are no findings,
say that clearly and mention any residual test risk.
"""


def _show_slash_help(args: str, ui: LilBotUI) -> None:
    if args:
        command = resolve_slash_command(args)
        if command is None:
            ui.error(f"Unknown command: /{args.lstrip('/')}")
            return
        aliases = ", ".join(f"/{alias}" for alias in command.aliases) or "-"
        ui.table(
            f"/{command.name}",
            ["Key", "Value"],
            [
                ("usage", command.usage),
                ("type", command.type),
                ("aliases", aliases),
                ("description", command.description),
            ],
        )
        return

    rows = [
        (command.usage, command.type, command.description)
        for command in SLASH_COMMANDS
        if not command.hidden
    ]
    ui.table("Slash Commands", ["Command", "Type", "Purpose"], rows)


def _estimate_message_tokens(messages: list[dict[str, object]]) -> int:
    total_chars = 0
    for message in messages:
        total_chars += len(str(message.get("role", "")))
        content = message.get("content", "")
        if isinstance(content, list):
            total_chars += sum(len(str(item)) for item in content)
        else:
            total_chars += len(str(content))
    return max(0, total_chars // 4)


def _token_rows(agent: Agent, ctx: ToolContext) -> list[tuple[str, str]]:
    messages = getattr(agent, "messages", []) or []
    usage = getattr(agent, "usage", {}) or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
    cache_read = int(usage.get("cache_read_tokens", 0) or 0)
    cache_rate = f"{(cache_read / prompt_tokens * 100):.0f}%" if prompt_tokens else "0%"
    registry = getattr(agent, "registry", None)
    try:
        catalog_fp = registry.catalog_fingerprint() if registry else "n/a"
        visible_tools = len(registry.schemas()) if registry else 0
    except Exception:
        catalog_fp, visible_tools = "n/a", 0
    return [
        ("tools_visible", str(visible_tools)),
        ("tool_catalog_fp", catalog_fp),
        ("messages", str(len(messages))),
        ("approx_context_tokens", str(_estimate_message_tokens(messages))),
        ("context_window", str(getattr(ctx.config, "context_window", 128_000))),
        ("prompt_tokens", str(prompt_tokens)),
        ("cache_read_tokens", f"{cache_read} ({cache_rate} of prompt)"),
        ("completion_tokens", str(completion_tokens)),
        ("total_tokens", str(total_tokens)),
        ("compact_after_messages", str(getattr(ctx.config, "compact_after_messages", 28))),
        ("max_steps", str(getattr(ctx.config, "max_steps", 20))),
    ]


def _reset_conversation(agent: Agent) -> str:
    reset = getattr(agent, "reset_conversation", None)
    if callable(reset):
        return str(reset())
    messages = getattr(agent, "messages", None)
    if isinstance(messages, list):
        del messages[1:]
    usage = getattr(agent, "usage", None)
    if isinstance(usage, dict):
        usage.clear()
    return "Conversation reset. Messages now: 1"


def _clear_trace(ui: LilBotUI) -> None:
    clear_trace = getattr(ui, "clear_trace", None)
    if callable(clear_trace):
        clear_trace()


def _copy_trace(ui: LilBotUI) -> bool:
    copy_trace = getattr(ui, "copy_trace", None)
    if callable(copy_trace):
        copy_trace()
        return True
    private_copy_trace = getattr(ui, "_copy_trace", None)
    if callable(private_copy_trace):
        private_copy_trace(selection_first=False)
        return True
    return False


def _run_local_tool(registry: ToolRegistry, ctx: ToolContext, name: str, args: dict[str, object], ui: LilBotUI, success: str) -> bool:
    result, _elapsed_ms = registry.execute(name, args, ctx)
    if result.ok:
        ui.print(success, "green")
        return True
    ui.error(result.output)
    return False


def handle_slash(line: str, agent: Agent, registry: ToolRegistry, ctx: ToolContext, ui: LilBotUI) -> bool:
    if not line.startswith("/"):
        return False
    head, _, tail = line[1:].partition(" ")
    command = resolve_slash_command(head)
    if command is None:
        ui.error(f"Unknown command: /{head.strip().lower()}. Try /help")
        return True
    cmd = command.name
    args = tail.strip()

    if cmd == "exit":
        raise KeyboardInterrupt
    if cmd == "help":
        _show_slash_help(args, ui)
        return True
    if cmd == "clear":
        message = _reset_conversation(agent)
        _clear_trace(ui)
        ui.print(f"{message}. Trace cleared.", "green")
        return True
    if cmd == "theme":
        ui.theme_demo()
        return True
    if cmd in {"model", "models"}:
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
    if cmd == "team":
        handle_team(args, agent, ctx, ui)
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
    if cmd == "sessions":
        store = getattr(agent, "sessions", None)
        infos = store.list() if store else []
        if not infos:
            ui.print("No saved sessions yet.")
            return True
        import datetime as _dt
        rows = [
            (
                s.session_id,
                _dt.datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M"),
                str(s.message_count),
                s.preview,
            )
            for s in infos
        ]
        ui.table("Sessions", ["ID", "Updated", "Msgs", "Preview"], rows)
        return True
    if cmd == "resume":
        ui.print(agent.resume(args.strip() or None), "green")
        return True
    if cmd == "history":
        hist = getattr(agent, "file_history", None)
        entries = hist.list() if hist else []
        if not entries:
            ui.print("No file edits recorded yet.")
            return True
        import datetime as _dt
        rows = [
            (
                str(e.seq),
                e.rel_path,
                e.tool,
                ("modified" if e.existed else "created"),
                _dt.datetime.fromtimestamp(e.ts).strftime("%H:%M:%S"),
            )
            for e in entries[-20:]
        ]
        ui.table("File history", ["#", "Path", "Tool", "Change", "Time"], rows)
        return True
    if cmd == "rewind":
        hist = getattr(agent, "file_history", None)
        if hist is None:
            ui.print("File history is unavailable.")
            return True
        try:
            n = int(args.strip()) if args.strip() else 1
        except ValueError:
            n = 1
        results = hist.rewind(n)
        if not results:
            ui.print("Nothing to rewind.")
        else:
            ui.print("Rewound:\n" + "\n".join(f"- {r}" for r in results), "green")
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
    if cmd == "tokens":
        ui.table("Token Usage", ["Key", "Value"], _token_rows(agent, ctx))
        return True
    if cmd == "plan":
        ok = _run_local_tool(
            registry,
            ctx,
            "EnterPlanMode",
            {"reason": args or "slash /plan", "plan": args},
            ui,
            "Plan Mode enabled. Write/execute tools are gated until the plan is approved.",
        )
        if ok and args:
            run_prompt(
                agent,
                ui,
                (
                    "Plan Mode task:\n"
                    f"{args}\n\n"
                    "Create a concise implementation plan first. Do not perform write, shell, "
                    "or other execution actions until the user approves the plan."
                ),
            )
        return True
    if cmd == "do":
        requested = args.strip().lower()
        rejected = requested in {"reject", "rejected", "deny", "denied", "no", "n"}
        approval_state = "rejected" if rejected else "approved"
        _run_local_tool(
            registry,
            ctx,
            "ExitPlanMode",
            {"approved": not rejected, "approval_state": approval_state, "summary": args or "slash /do"},
            ui,
            f"Plan Mode exited with approval_state={approval_state}.",
        )
        return True
    if cmd == "review":
        focus = f"\nExtra review focus: {args}\n" if args else ""
        run_prompt(agent, ui, REVIEW_PROMPT + focus)
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
    if cmd == "copy":
        if not _copy_trace(ui):
            ui.error("Trace copy is only available in the dashboard UI.")
        return True
    return True


def handle_team(args: str, agent: Agent, ctx: ToolContext, ui: LilBotUI) -> None:
    teams = getattr(ctx, "teams", None)
    if teams is None:
        ui.error("Team system is unavailable in this runtime.")
        return
    action, _, tail = args.partition(" ")
    action = (action or "list").strip().lower()
    tail = tail.strip()

    if action == "list":
        rows: list[tuple[str, str, str, str]] = []
        for team in teams.list_teams():
            if not team.members:
                rows.append((team.name, "-", "-", "(no members)"))
            for m in team.members:
                prog = getattr(m, "progress", None)
                status = getattr(prog, "status", None) or ("idle" if m.is_active is False else "active")
                last = getattr(prog, "last_message", None) or ""
                rows.append((team.name, m.name, f"{m.agent_type}/{status}", " ".join(last.split())[:50]))
        if not rows:
            ui.print("No teams. Create one in chat (the agent calls team_create) or '/team new NAME'.")
            return
        ui.table("Teams", ["Team", "Member", "Type/Status", "Last"], rows)
        return
    if action == "new":
        if not tail:
            ui.error("Usage: /team new NAME")
            return
        team = teams.create_team(tail, "lead", "")
        ui.print(f"Created team '{team.name}'.", "green")
        return
    if action in {"msg", "send"}:
        name, _, text = tail.partition(" ")
        if not name or not text.strip():
            ui.error("Usage: /team msg NAME TEXT")
            return
        from .tools.builtin import _send_message  # reuse the tool handler
        res = _send_message({"to": name, "message": text.strip(), "summary": text.strip()[:40]}, ctx)
        ui.print(res.output, "green" if res.ok else "red")
        agent.drain_team_notifications()
        return
    if action in {"rm", "delete", "del"}:
        if not tail:
            ui.error("Usage: /team rm NAME")
            return
        try:
            teams.delete_team(tail)
            ui.print(f"Deleted team '{tail}'.", "green")
        except Exception as exc:  # noqa: BLE001
            ui.error(str(exc))
        return
    ui.error("Usage: /team list|new NAME|msg NAME TEXT|rm NAME")


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
    if getattr(args, "mcp_server", False):
        # MCP server mode (M8): expose LilBot's tools to other MCP clients over
        # stdio. No TUI; build the runtime non-interactively and serve.
        _agent, registry, ctx = build_runtime(cfg, ui, interactive=False)
        from .mcp.server import LilBotMCPServer, load_expose_config
        LilBotMCPServer(registry, ctx, load_expose_config(cfg.state_dir)).serve()
        return 0
    one_shot = args.print_mode or bool(args.prompt)
    agent, registry, ctx = build_runtime(cfg, ui, interactive=not one_shot)
    maybe_resume(agent, ui, getattr(args, "resume", None))
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
