from __future__ import annotations

import difflib
import html as html_lib
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
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


WEB_USER_AGENT = "LilBot/0.1 (+https://github.com/terrense/LilBot-agent)"
MAX_WEB_RESULTS = 10
MAX_FETCH_CHARS = 40000


def _permission(ctx: ToolContext, action: str, description: str) -> bool:
    return ctx.permissions.check(action, description).allowed


def _clean_html_fragment(value: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", value)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([.,!?;:])", r"\1", text)


def _decode_duckduckgo_url(url: str) -> str:
    url = html_lib.unescape(url)
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return query["uddg"][0]
    return url


def _parse_duckduckgo_results(body: str, max_results: int) -> list[dict[str, str]]:
    link_re = re.compile(
        r'(?is)<a[^>]*class=["\'][^"\']*result__a[^"\']*["\'][^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
    )
    snippet_re = re.compile(
        r'(?is)<(?:a|div)[^>]*class=["\'][^"\']*result__snippet[^"\']*["\'][^>]*>(.*?)</(?:a|div)>'
    )
    snippets = [_clean_html_fragment(match.group(1)) for match in snippet_re.finditer(body)]
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, match in enumerate(link_re.finditer(body)):
        title = _clean_html_fragment(match.group(2))
        url = _decode_duckduckgo_url(match.group(1))
        if not title or not url or url in seen:
            continue
        seen.add(url)
        item = {"title": title, "url": url}
        if index < len(snippets) and snippets[index]:
            item["snippet"] = snippets[index]
        results.append(item)
        if len(results) >= max_results:
            break
    return results


def _decode_bing_url(url: str) -> str:
    url = html_lib.unescape(url)
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "u" in query and query["u"]:
        encoded = query["u"][0]
        if encoded.startswith("a1"):
            try:
                import base64

                padded = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
                return base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
            except Exception:
                return url
    return url


def _parse_bing_results(body: str, max_results: int) -> list[dict[str, str]]:
    result_re = re.compile(r'(?is)<li[^>]*class=["\'][^"\']*\bb_algo\b[^"\']*["\'][^>]*>(.*?)</li>')
    title_re = re.compile(r'(?is)<h2[^>]*>.*?<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>')
    snippet_re = re.compile(r'(?is)<p[^>]*>(.*?)</p>')
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in result_re.findall(body):
        title_match = title_re.search(block)
        if not title_match:
            continue
        url = _decode_bing_url(title_match.group(1))
        title = _clean_html_fragment(title_match.group(2))
        if not title or not url or url in seen:
            continue
        seen.add(url)
        item = {"title": title, "url": url}
        snippet_match = snippet_re.search(block)
        if snippet_match:
            item["snippet"] = _clean_html_fragment(snippet_match.group(1))
        results.append(item)
        if len(results) >= max_results:
            break
    return results


def _http_get(url: str, timeout: int = 15) -> tuple[str, int, str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": WEB_USER_AGENT,
            "Accept": "text/html,text/plain,application/json,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final_url = response.geturl()
        status = getattr(response, "status", 200)
        content_type = response.headers.get("Content-Type", "")
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read()
    text = raw.decode(charset, errors="replace")
    return final_url, int(status), content_type, text


def _is_proxy_fake_ip(ip: ipaddress._BaseAddress) -> bool:
    return isinstance(ip, ipaddress.IPv4Address) and ip.packed[0] == 198 and ip.packed[1] in {18, 19}


def _is_restricted_ip(ip: ipaddress._BaseAddress, allow_proxy_fake_ip: bool = False) -> bool:
    if allow_proxy_fake_ip and _is_proxy_fake_ip(ip):
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_public_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "only http:// and https:// URLs are supported"
    if not parsed.hostname:
        return "URL must include a host"
    host = parsed.hostname.strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"}:
        return "requests to localhost are not allowed"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            for info in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80)):
                ip = ipaddress.ip_address(info[4][0])
                if _is_restricted_ip(ip, allow_proxy_fake_ip=True):
                    return f"resolved IP {ip} is restricted"
        except OSError:
            return None
    else:
        if _is_restricted_ip(ip):
            return f"IP {ip} is restricted"
    return None


def _web_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    query = str(args.get("query") or args.get("q") or "").strip()
    if not query and isinstance(args.get("search_query"), list) and args["search_query"]:
        first = args["search_query"][0]
        if isinstance(first, dict):
            query = str(first.get("q") or first.get("query") or "").strip()
            args = {**first, **args}
    if not query:
        return ToolResult(False, "Missing required query.")
    max_results = max(1, min(int(args.get("max_results", 5)), MAX_WEB_RESULTS))
    timeout = max(3, min(int(args.get("timeout", 15)), 60))
    errors: list[str] = []

    engines = [
        (
            "duckduckgo",
            f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}",
            _parse_duckduckgo_results,
        ),
        (
            "bing",
            f"https://www.bing.com/search?q={urllib.parse.quote_plus(query)}",
            _parse_bing_results,
        ),
    ]
    for source, url, parser in engines:
        try:
            _, _, _, body = _http_get(url, timeout)
            results = parser(body, max_results)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{source}: {exc}")
            continue
        if results:
            response = {
                "query": query,
                "source": source,
                "count": len(results),
                "message": f"Found {len(results)} result(s)",
                "results": results,
            }
            return ToolResult(True, json.dumps(response, ensure_ascii=False, indent=2))
        errors.append(f"{source}: no parseable results")

    return ToolResult(False, "Web search failed. " + " | ".join(errors))


def _fetch_url(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    url = str(args.get("url") or "").strip()
    if not url:
        return ToolResult(False, "Missing required url.")
    validation_error = _validate_public_url(url)
    if validation_error:
        return ToolResult(False, validation_error)
    timeout = max(3, min(int(args.get("timeout", 15)), 60))
    max_chars = max(1000, min(int(args.get("max_chars", 12000)), MAX_FETCH_CHARS))
    fmt = str(args.get("format") or "text").lower()
    try:
        final_url, status, content_type, body = _http_get(url, timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ToolResult(False, f"Fetch failed: {exc}")
    content = body if fmt == "raw" else _clean_html_fragment(body)
    truncated = len(content) > max_chars
    response = {
        "url": final_url,
        "status": status,
        "content_type": content_type,
        "content": content[:max_chars],
        "truncated": truncated,
    }
    return ToolResult(200 <= status < 400, json.dumps(response, ensure_ascii=False, indent=2), response)


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
    registry.register(ToolDef("web_search", "Search the public web and return ranked results with URLs and snippets.", _schema({
        "query": _string("Search query."),
        "q": _string("Search query alias."),
        "search_query": {
            "type": "array",
            "description": "Compatibility array form: [{\"q\":\"...\", \"max_results\": 5}].",
            "items": {"type": "object"},
        },
        "max_results": _integer("Maximum results, 1-10.", 5),
        "timeout": _integer("Timeout in seconds.", 15),
    }), _web_search))
    registry.register(ToolDef("fetch_url", "Fetch a known public HTTP/HTTPS URL and return readable content.", _schema({
        "url": _string("Absolute public HTTP/HTTPS URL."),
        "format": _string("text or raw. Default: text."),
        "max_chars": _integer("Maximum content characters, up to 40000.", 12000),
        "timeout": _integer("Timeout in seconds.", 15),
    }, ["url"]), _fetch_url))
    registry.register(ToolDef("web_fetch", "Alias for fetch_url.", _schema({
        "url": _string("Absolute public HTTP/HTTPS URL."),
        "format": _string("text or raw. Default: text."),
        "max_chars": _integer("Maximum content characters, up to 40000.", 12000),
        "timeout": _integer("Timeout in seconds.", 15),
    }, ["url"]), _fetch_url))
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
