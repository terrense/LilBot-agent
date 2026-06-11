from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from .registry import ToolContext, ToolDef, ToolRegistry, ToolResult


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _string(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _integer(description: str, default: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"type": "integer", "description": description}
    if default is not None:
        data["default"] = default
    return data


def _bool(description: str, default: bool | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"type": "boolean", "description": description}
    if default is not None:
        data["default"] = default
    return data


def _permission(ctx: ToolContext, action: str, description: str) -> bool:
    return ctx.permissions.check(action, description).allowed


def _list_dir(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = ctx.sandbox.list_dir(args.get("path", "."), int(args.get("max_depth", 1)))
    return ToolResult(True, "\n".join(rows) if rows else "(empty)")


def _read_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(args["path"])
    offset = max(0, int(args.get("offset", 0)))
    limit = max(1, min(int(args.get("limit", 4000)), 30000))
    if not path.exists():
        return ToolResult(False, f"File not found: {args['path']}")
    if not path.is_file():
        return ToolResult(False, f"Not a file: {args['path']}")
    text = path.read_text(encoding="utf-8", errors="ignore")
    return ToolResult(True, text[offset : offset + limit], {"path": str(path), "bytes": len(text)})


def _write_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rel = args["path"]
    if not _permission(ctx, f"write:{rel}", f"write file {rel}"):
        return ToolResult(False, "Permission denied.")
    path = ctx.sandbox.resolve(rel)
    old = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    new = args.get("content", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="",
        )
    )
    return ToolResult(True, diff or f"Wrote {len(new)} chars to {rel}")


def _edit_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rel = args["path"]
    if not _permission(ctx, f"edit:{rel}", f"edit file {rel}"):
        return ToolResult(False, "Permission denied.")
    path = ctx.sandbox.resolve(rel)
    if not path.exists():
        return ToolResult(False, f"File not found: {rel}")
    old_text = path.read_text(encoding="utf-8", errors="ignore")
    old = args["old"]
    new = args["new"]
    if old not in old_text:
        return ToolResult(False, "Old text was not found.")
    updated = old_text.replace(old, new) if args.get("replace_all", False) else old_text.replace(old, new, 1)
    path.write_text(updated, encoding="utf-8")
    diff = "\n".join(
        difflib.unified_diff(
            old_text.splitlines(),
            updated.splitlines(),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="",
        )
    )
    return ToolResult(True, diff or f"Edited {rel}")


def _bash(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command = args["command"]
    if not _permission(ctx, f"bash:{command}", f"run shell command: {command}"):
        return ToolResult(False, "Permission denied.")
    result = ctx.sandbox.run(command, int(args.get("timeout", 30)))
    return ToolResult(result.ok, result.output or f"Process exited with {result.returncode}", {"returncode": result.returncode})


def _glob(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = ctx.sandbox.glob(args["pattern"], args.get("path", "."))
    return ToolResult(True, "\n".join(rows) if rows else "(no matches)")


def _grep(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = ctx.sandbox.grep(
        args["pattern"],
        args.get("path", "."),
        args.get("glob"),
        int(args.get("max_results", 80)),
    )
    return ToolResult(True, "\n".join(rows) if rows else "(no matches)")


def _memory_save(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    entry = ctx.memory.add(
        args["name"],
        args["text"],
        args.get("kind", "note"),
        args.get("scope", "project"),
    )
    return ToolResult(True, f"Saved memory {entry.id}: {entry.name}")


def _memory_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = [f"{e.id} [{e.kind}/{e.scope}] {e.name}: {e.preview()}" for e in ctx.memory.list()]
    return ToolResult(True, "\n".join(rows) if rows else "(no memories)")


def _memory_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = [f"{e.id} [{e.kind}/{e.scope}] {e.name}: {e.preview()}" for e in ctx.memory.search(args["query"])]
    return ToolResult(True, "\n".join(rows) if rows else "(no matches)")


def _memory_delete(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    ok = ctx.memory.delete(args["id_or_name"])
    return ToolResult(ok, "Deleted." if ok else "Memory not found.")


def _skill_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = [f"{s.name} [{s.mode}] - {s.description}" for s in ctx.skills.list()]
    return ToolResult(True, "\n".join(rows) if rows else "(no skills)")


def _skill_run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rendered = ctx.skills.render(args["name"], args.get("args", ""))
    return ToolResult(True, rendered)


def _agent_spawn(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task = ctx.subagents.spawn(
        args.get("agent_type", "planner"),
        args["prompt"],
        bool(args.get("background", False)),
    )
    if task.status == "done":
        return ToolResult(True, f"{task.id} done:\n{task.result}", {"task_id": task.id})
    return ToolResult(True, f"{task.id} {task.status}", {"task_id": task.id})


def _agent_status(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task = ctx.subagents.get(args["task_id"])
    if not task:
        return ToolResult(False, "Task not found.")
    body = task.result if task.status == "done" else task.error or task.status
    return ToolResult(task.status != "error", f"{task.id} [{task.status}] {task.agent_type}\n{body}")


def _agent_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = [f"{d.name}: {d.description}" for d in ctx.subagents.list_types()]
    rows += [f"{t.id} [{t.status}] {t.agent_type}: {t.prompt[:80]}" for t in ctx.subagents.list_tasks()]
    return ToolResult(True, "\n".join(rows) if rows else "(no agent data)")


def _mcp_servers(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    servers = ctx.mcp.list_servers()
    if not servers:
        path = ctx.mcp.write_example_config()
        return ToolResult(True, f"No MCP servers configured. Example config created at {path}")
    return ToolResult(True, "\n".join(f"{s.name}: {s.command} {' '.join(s.args)}" for s in servers))


def _mcp_call(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    output = ctx.mcp.call_tool(args["server"], args["tool"], args.get("arguments", {}))
    return ToolResult(True, output)


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(ToolDef("list_dir", "List files under a workspace path.", _schema({
        "path": _string("Directory path relative to workspace."),
        "max_depth": _integer("Recursive depth, 0-8.", 1),
    }), _list_dir))
    registry.register(ToolDef("read_file", "Read a UTF-8 text file inside the workspace.", _schema({
        "path": _string("File path relative to workspace."),
        "offset": _integer("Character offset.", 0),
        "limit": _integer("Maximum characters to return.", 4000),
    }, ["path"]), _read_file))
    registry.register(ToolDef("write_file", "Write a UTF-8 file inside the workspace.", _schema({
        "path": _string("File path relative to workspace."),
        "content": _string("Full file content."),
    }, ["path", "content"]), _write_file))
    registry.register(ToolDef("edit_file", "Replace text in a workspace file.", _schema({
        "path": _string("File path relative to workspace."),
        "old": _string("Exact text to replace."),
        "new": _string("Replacement text."),
        "replace_all": _bool("Replace every occurrence.", False),
    }, ["path", "old", "new"]), _edit_file))
    registry.register(ToolDef("bash", "Run a shell command in the workspace after permission approval.", _schema({
        "command": _string("Shell command."),
        "timeout": _integer("Timeout in seconds.", 30),
    }, ["command"]), _bash))
    registry.register(ToolDef("glob", "Find files by glob pattern.", _schema({
        "pattern": _string("Glob pattern, for example **/*.py."),
        "path": _string("Base path relative to workspace."),
    }, ["pattern"]), _glob))
    registry.register(ToolDef("grep", "Search text in workspace files.", _schema({
        "pattern": _string("Text pattern to search, case-insensitive."),
        "path": _string("Base path relative to workspace."),
        "glob": _string("Optional filename glob, for example *.py."),
        "max_results": _integer("Maximum matches.", 80),
    }, ["pattern"]), _grep))
    registry.register(ToolDef("memory_save", "Save persistent project memory.", _schema({
        "name": _string("Short memory name."),
        "text": _string("Memory content."),
        "kind": _string("Memory type, such as preference/fact/task."),
        "scope": _string("project or user."),
    }, ["name", "text"]), _memory_save))
    registry.register(ToolDef("memory_list", "List memories.", _schema({}), _memory_list))
    registry.register(ToolDef("memory_search", "Search memories.", _schema({
        "query": _string("Search query."),
    }, ["query"]), _memory_search))
    registry.register(ToolDef("memory_delete", "Delete a memory by id or name.", _schema({
        "id_or_name": _string("Memory id or exact name."),
    }, ["id_or_name"]), _memory_delete))
    registry.register(ToolDef("skill_list", "List available skills.", _schema({}), _skill_list))
    registry.register(ToolDef("skill_run", "Render a skill template.", _schema({
        "name": _string("Skill name."),
        "args": _string("Arguments injected into {{args}}."),
    }, ["name"]), _skill_run))
    registry.register(ToolDef("agent_spawn", "Spawn a lightweight sub-agent.", _schema({
        "agent_type": _string("coder, reviewer, researcher, or planner."),
        "prompt": _string("Task prompt."),
        "background": _bool("Run in background.", False),
    }, ["prompt"]), _agent_spawn))
    registry.register(ToolDef("agent_status", "Check a sub-agent task.", _schema({
        "task_id": _string("Task id."),
    }, ["task_id"]), _agent_status))
    registry.register(ToolDef("agent_list", "List sub-agent types and tasks.", _schema({}), _agent_list))
    registry.register(ToolDef("mcp_servers", "List configured MCP-style servers.", _schema({}), _mcp_servers))
    registry.register(ToolDef("mcp_call", "Call a tool on an MCP-style server.", _schema({
        "server": _string("Server name."),
        "tool": _string("Tool name."),
        "arguments": {"type": "object", "description": "Tool arguments."},
    }, ["server", "tool"]), _mcp_call))

