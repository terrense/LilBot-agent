from __future__ import annotations

import difflib
import fnmatch
import html as html_lib
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..subagents import SubAgentGateError
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
NOISY_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
}
_SHELL_JOBS: dict[str, dict[str, Any]] = {}
_SHELL_LOCK = threading.RLock()
_RLM_SESSIONS: dict[str, dict[str, Any]] = {}


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _state_path(ctx: ToolContext, name: str) -> Path:
    ctx.config.state_dir.mkdir(parents=True, exist_ok=True)
    return ctx.config.state_dir / name


def _load_state(ctx: ToolContext, name: str, default: Any) -> Any:
    path = _state_path(ctx, name)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_state(ctx: ToolContext, name: str, data: Any) -> Path:
    path = _state_path(ctx, name)
    path.write_text(_json(data), encoding="utf-8")
    return path


def _quote_ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_readonly(ctx: ToolContext, command: str, timeout: int = 30) -> ToolResult:
    result = ctx.sandbox.run(command, timeout)
    return ToolResult(result.ok, result.output or f"Process exited with {result.returncode}", {"returncode": result.returncode})


def _schema_array(description: str) -> dict[str, Any]:
    return {"type": "array", "description": description, "items": {"type": "object"}}


def _is_noisy_path(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    return any(part in NOISY_DIRS for part in rel.parts)


def _parse_line_range(value: Any, total: int) -> tuple[int, int] | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "-" in text:
        left, right = text.split("-", 1)
        start = int(left.strip() or "1")
        end = int(right.strip() or str(total))
    elif ":" in text:
        left, right = text.split(":", 1)
        start = int(left.strip() or "1")
        end = int(right.strip() or str(total))
    else:
        start = end = int(text)
    start = max(1, min(start, total or 1))
    end = max(start, min(end, total or start))
    return start, end


def _numbered_slice(lines: list[str], start: int, end: int) -> str:
    width = len(str(end))
    return "\n".join(f"{idx:>{width}} | {lines[idx - 1]}" for idx in range(start, end + 1))


def _bounded_text_projection(text: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    lines = text.splitlines()
    total_lines = len(lines)
    metadata: dict[str, Any] = {"bytes": len(text.encode("utf-8")), "lines": total_lines}

    if args.get("query"):
        query = str(args["query"])
        context = max(0, min(int(args.get("context", 2)), 20))
        matches = []
        for idx, line in enumerate(lines, 1):
            if query.lower() in line.lower():
                start = max(1, idx - context)
                end = min(total_lines, idx + context)
                matches.append({"line": idx, "start": start, "end": end, "text": _numbered_slice(lines, start, end)})
        limit = max(1, min(int(args.get("limit", 20)), 200))
        matches = matches[:limit]
        metadata.update({"mode": "query", "query": query, "match_count": len(matches)})
        return _json({"matches": matches, **metadata}), metadata

    line_range = _parse_line_range(args.get("lines") or args.get("line_range"), total_lines)
    if line_range:
        start, end = line_range
        metadata.update({"mode": "lines", "start_line": start, "end_line": end})
        return _numbered_slice(lines, start, end), metadata

    if args.get("head") is not None:
        count = max(1, min(int(args.get("head")), 5000))
        metadata.update({"mode": "head", "line_count": min(count, total_lines)})
        return "\n".join(lines[:count]), metadata

    if args.get("tail") is not None:
        count = max(1, min(int(args.get("tail")), 5000))
        metadata.update({"mode": "tail", "line_count": min(count, total_lines)})
        return "\n".join(lines[-count:]), metadata

    offset = max(0, int(args.get("offset", 0)))
    limit = max(1, min(int(args.get("limit", 4000)), 120000))
    projected = text[offset : offset + limit]
    metadata.update({"mode": "chars", "offset": offset, "limit": limit, "truncated": offset + limit < len(text)})
    return projected, metadata


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


def _web_run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if args.get("url"):
        return _fetch_url(args, ctx)
    return _web_search(args, ctx)


def _list_dir(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    base = ctx.sandbox.resolve(args.get("path", "."))
    if not base.exists():
        return ToolResult(False, f"Path not found: {args.get('path', '.')}")
    if not base.is_dir():
        return ToolResult(False, f"Not a directory: {args.get('path', '.')}")
    max_depth = max(0, min(int(args.get("max_depth", 1)), 8))
    limit = max(1, min(int(args.get("limit", 500)), 5000))
    include_hidden = bool(args.get("include_hidden", False))
    entries = []
    iterator = base.rglob("*") if max_depth else base.iterdir()
    for item in sorted(iterator):
        if _is_noisy_path(item, ctx.sandbox.root):
            continue
        rel_from_base = item.relative_to(base)
        if len(rel_from_base.parts) > max_depth + 1:
            continue
        if not include_hidden and any(part.startswith(".") for part in rel_from_base.parts):
            continue
        rel = item.relative_to(ctx.sandbox.root).as_posix()
        entries.append({
            "path": rel + ("/" if item.is_dir() else ""),
            "type": "dir" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
        })
        if len(entries) >= limit:
            break
    rows = [entry["path"] for entry in entries]
    return ToolResult(True, "\n".join(rows) if rows else "(empty)", {"entries": entries, "count": len(entries), "truncated": len(entries) >= limit})


def _read_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = ctx.sandbox.resolve(args["path"])
    if not path.exists():
        return ToolResult(False, f"File not found: {args['path']}")
    if not path.is_file():
        return ToolResult(False, f"Not a file: {args['path']}")
    if path.suffix.lower() == ".pdf":
        pdftotext = shutil.which("pdftotext")
        if not pdftotext:
            return ToolResult(False, "PDF reading requires pdftotext/poppler on PATH.", {"path": str(path)})
        proc = subprocess.run([pdftotext, str(path), "-"], text=True, capture_output=True, timeout=120)
        if proc.returncode != 0:
            return ToolResult(False, proc.stderr or "pdftotext failed.", {"returncode": proc.returncode, "path": str(path)})
        text = proc.stdout
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
    output, metadata = _bounded_text_projection(text, args)
    metadata["path"] = str(path)
    return ToolResult(True, output, metadata)


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


def _apply_patch(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    patch = str(args.get("patch") or args.get("diff") or "")
    if not patch.strip():
        return ToolResult(False, "Missing required patch.")
    if not _permission(ctx, "apply_patch", "apply a unified diff to the workspace"):
        return ToolResult(False, "Permission denied.")
    tmp_dir = ctx.sandbox.resolve(".lilbot/tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    patch_path = tmp_dir / f"patch-{uuid4().hex}.diff"
    patch_path.write_text(patch, encoding="utf-8")
    rel = patch_path.relative_to(ctx.sandbox.root).as_posix()
    result = ctx.sandbox.run(f"git apply --whitespace=nowarn {_quote_ps(rel)}", int(args.get("timeout", 30)))
    try:
        patch_path.unlink(missing_ok=True)
    except OSError:
        pass
    return ToolResult(result.ok, result.output or ("Patch applied." if result.ok else "Patch failed."), {"returncode": result.returncode})


def _file_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    query = str(args.get("query") or args.get("pattern") or args.get("name") or "").strip().lower()
    if not query:
        return ToolResult(False, "Missing required query.")
    limit = max(1, min(int(args.get("limit", args.get("max_results", 50))), 200))
    base = ctx.sandbox.resolve(args.get("path", "."))
    matches: list[tuple[int, str]] = []
    for path in base.rglob("*"):
        if _is_noisy_path(path, ctx.sandbox.root):
            continue
        rel = path.relative_to(ctx.sandbox.root).as_posix()
        name = path.name.lower()
        haystack = rel.lower()
        score = 0
        if name == query:
            score += 100
        if name.startswith(query):
            score += 60
        if query in name:
            score += 40
        if query in haystack:
            score += 20
        if score:
            matches.append((score, rel + ("/" if path.is_dir() else "")))
    matches.sort(key=lambda item: (-item[0], item[1]))
    rows = [{"path": rel, "score": score} for score, rel in matches[:limit]]
    return ToolResult(True, "\n".join(item["path"] for item in rows) if rows else "(no matches)", {"matches": rows, "count": len(rows)})


def _git_status(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    result = ctx.sandbox.run("git status --porcelain=v1 --branch", 30)
    lines = result.output.splitlines()
    branch = ""
    changes = []
    for line in lines:
        if line.startswith("## "):
            branch = line[3:]
            continue
        if len(line) >= 4:
            changes.append({"xy": line[:2], "path": line[3:]})
    data = {"branch": branch, "changes": changes, "clean": result.ok and not changes, "returncode": result.returncode}
    return ToolResult(result.ok, _json(data), data)


def _git_diff(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    staged = bool(args.get("staged", False))
    path = str(args.get("path") or "").strip()
    command = "git diff --staged" if staged else "git diff"
    if path:
        command += " -- " + _quote_ps(path)
    result = ctx.sandbox.run(command, 60)
    metadata = {
        "command": command,
        "returncode": result.returncode,
        "staged": staged,
        "path": path or None,
        "chars": len(result.output),
        "files": re.findall(r"^diff --git a/(.*?) b/", result.output, flags=re.MULTILINE),
    }
    return ToolResult(result.ok, result.output or "(no diff)", metadata)


def _git_log(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    limit = max(1, min(int(args.get("limit", 20)), 100))
    command = f"git log -n {limit} --date=iso --pretty=format:%H%x09%h%x09%an%x09%ad%x09%s"
    result = ctx.sandbox.run(command, 30)
    commits = []
    for line in result.output.splitlines():
        parts = line.split("\t", 4)
        if len(parts) == 5:
            commits.append({"hash": parts[0], "short": parts[1], "author": parts[2], "date": parts[3], "subject": parts[4]})
    return ToolResult(result.ok, _json({"commits": commits, "count": len(commits)}), {"commits": commits, "count": len(commits), "returncode": result.returncode})


def _git_show(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    revision = str(args.get("revision") or args.get("rev") or "HEAD")
    command = f"git show --stat --patch {_quote_ps(revision)}"
    result = ctx.sandbox.run(command, 60)
    return ToolResult(result.ok, result.output, {"revision": revision, "returncode": result.returncode, "chars": len(result.output)})


def _git_blame(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rel = str(args.get("path") or "")
    if not rel:
        return ToolResult(False, "Missing required path.")
    start = int(args.get("start", 1))
    end = int(args.get("end", start + 80))
    command = f"git blame -L {start},{end} -- {_quote_ps(rel)}"
    result = ctx.sandbox.run(command, 30)
    rows = []
    for line in result.output.splitlines():
        match = re.match(r"^([0-9a-f^]+)\\s+\\((.*?)\\s+(\\d{4}-\\d{2}-\\d{2}).*?\\s+(\\d+)\\)\\s?(.*)$", line)
        if match:
            rows.append({"commit": match.group(1), "author": match.group(2).strip(), "date": match.group(3), "line": int(match.group(4)), "text": match.group(5)})
    return ToolResult(result.ok, result.output, {"path": rel, "start": start, "end": end, "rows": rows, "returncode": result.returncode})


def _git_worktree_support(ctx: ToolContext) -> tuple[bool, dict[str, Any]]:
    if not shutil.which("git"):
        return False, {"supported": False, "status": "unsupported", "reason": "git is not installed or not on PATH"}
    root = ctx.sandbox.run("git rev-parse --show-toplevel", 10)
    if not root.ok:
        return False, {
            "supported": False,
            "status": "unsupported",
            "reason": "workspace is not a git repository",
            "returncode": root.returncode,
            "output": root.output,
        }
    worktree = ctx.sandbox.run("git worktree list --porcelain", 10)
    if not worktree.ok:
        return False, {
            "supported": False,
            "status": "unsupported",
            "reason": "git worktree is unavailable or failed",
            "returncode": worktree.returncode,
            "output": worktree.output,
        }
    return True, {
        "supported": True,
        "status": "supported",
        "git_root": root.output.splitlines()[0] if root.output else "",
        "worktrees": worktree.output,
    }


def _enter_worktree(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    supported, support = _git_worktree_support(ctx)
    if not supported:
        return ToolResult(False, _json(support), support)
    name = str(args.get("name") or f"worktree_{uuid4().hex[:8]}")
    rel_path = str(args.get("path") or f".lilbot/worktrees/{name}")
    branch = str(args.get("branch") or args.get("ref") or "").strip()
    try:
        target = ctx.sandbox.resolve(rel_path)
    except Exception as exc:
        data = {"supported": True, "status": "error", "reason": str(exc), "path": rel_path}
        return ToolResult(False, _json(data), data)
    create = _optional_bool(args.get("create"))
    should_create = (create is not False) and not target.exists()
    if should_create:
        command = f"git worktree add {_quote_ps(str(target))}"
        if branch:
            command += " " + _quote_ps(branch)
        if not _permission(ctx, f"worktree:add:{target}", f"create git worktree at {target}"):
            return ToolResult(False, "Permission denied.")
        result = ctx.sandbox.run(command, int(args.get("timeout", 120)))
        if not result.ok:
            data = {
                "supported": True,
                "status": "error",
                "reason": "git worktree add failed",
                "command": command,
                "returncode": result.returncode,
                "output": result.output,
            }
            return ToolResult(False, _json(data), data)
    if not target.exists() or not target.is_dir():
        data = {"supported": True, "status": "error", "reason": "worktree path does not exist", "path": str(target)}
        return ToolResult(False, _json(data), data)
    previous_root = ctx.sandbox.root
    ctx.sandbox.root = target.resolve()
    data = {
        "supported": True,
        "status": "active",
        "name": name,
        "path": str(ctx.sandbox.root),
        "previous_root": str(previous_root),
        "created": should_create,
        "branch": branch or None,
        "entered_at": time.time(),
    }
    _save_state(ctx, "worktree.json", data)
    return ToolResult(True, _json(data), data)


def _exit_worktree(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    data = _load_state(ctx, "worktree.json", {})
    if not isinstance(data, dict) or data.get("status") != "active":
        return ToolResult(False, "No active worktree.", {"status": "inactive"})
    previous_root = Path(str(data.get("previous_root") or ""))
    if not previous_root.exists():
        data.update({"status": "error", "reason": "previous root no longer exists", "updated_at": time.time()})
        _save_state(ctx, "worktree.json", data)
        return ToolResult(False, _json(data), data)
    ctx.sandbox.root = previous_root.resolve()
    data.update({
        "status": "exited",
        "path": str(data.get("path") or ""),
        "restored_root": str(ctx.sandbox.root),
        "exited_at": time.time(),
        "updated_at": time.time(),
    })
    _save_state(ctx, "worktree.json", data)
    return ToolResult(True, _json(data), data)


def _github_issue_context(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    issue = str(args.get("issue") or args.get("number") or args.get("url") or "")
    if not issue:
        return ToolResult(False, "Missing issue/number/url.")
    return _run_readonly(ctx, f"gh issue view {_quote_ps(issue)} --json number,title,state,body,author,labels,comments", 60)


def _github_pr_context(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    pr = str(args.get("pr") or args.get("number") or args.get("url") or "")
    if not pr:
        return ToolResult(False, "Missing pr/number/url.")
    result = _run_readonly(ctx, f"gh pr view {_quote_ps(pr)} --json number,title,state,body,author,baseRefName,headRefName,files,comments", 60)
    if args.get("include_diff"):
        diff = _run_readonly(ctx, f"gh pr diff {_quote_ps(pr)} --patch", 60)
        result.output += "\n\nDIFF:\n" + diff.output
        result.metadata["diff_returncode"] = diff.metadata.get("returncode")
    return result


def _github_comment(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    target = str(args.get("target") or args.get("issue") or args.get("pr") or args.get("number") or "")
    body = str(args.get("body") or args.get("comment") or "")
    if not target or not body:
        return ToolResult(False, "target and body/comment are required.")
    if not _permission(ctx, f"github_comment:{target}", f"post GitHub comment to {target}"):
        return ToolResult(False, "Permission denied.")
    tmp_dir = ctx.sandbox.resolve(".lilbot/tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    body_path = tmp_dir / f"github-comment-{uuid4().hex}.md"
    body_path.write_text(body, encoding="utf-8")
    rel = body_path.relative_to(ctx.sandbox.root).as_posix()
    result = ctx.sandbox.run(f"gh issue comment {_quote_ps(target)} --body-file {_quote_ps(rel)}", 60)
    body_path.unlink(missing_ok=True)
    return ToolResult(result.ok, result.output or f"gh exited with {result.returncode}", {"returncode": result.returncode})


def _github_close_issue(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    issue = str(args.get("issue") or args.get("number") or args.get("url") or "")
    reason = str(args.get("reason") or args.get("comment") or "")
    if not issue:
        return ToolResult(False, "Missing issue/number/url.")
    if not _permission(ctx, f"github_close_issue:{issue}", f"close GitHub issue {issue}"):
        return ToolResult(False, "Permission denied.")
    command = f"gh issue close {_quote_ps(issue)}"
    if reason:
        command += f" --comment {_quote_ps(reason)}"
    return _run_readonly(ctx, command, 60)


def _github_close_pr(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    pr = str(args.get("pr") or args.get("number") or args.get("url") or "")
    reason = str(args.get("reason") or args.get("comment") or "")
    if not pr:
        return ToolResult(False, "Missing pr/number/url.")
    if not _permission(ctx, f"github_close_pr:{pr}", f"close GitHub PR {pr}"):
        return ToolResult(False, "Permission denied.")
    command = f"gh pr close {_quote_ps(pr)}"
    if reason:
        command += f" --comment {_quote_ps(reason)}"
    return _run_readonly(ctx, command, 60)


def _diagnostics(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    git = ctx.sandbox.run("git status --short --branch", 10)
    data = {
        "workspace": str(ctx.sandbox.root),
        "state_dir": str(ctx.config.state_dir),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "provider": ctx.config.provider,
        "model": ctx.config.model,
        "permission_mode": ctx.permissions.mode,
        "git_status": git.output,
        "git_returncode": git.returncode,
        "tools": {
            "git": shutil.which("git"),
            "gh": shutil.which("gh"),
            "node": shutil.which("node"),
            "pandoc": shutil.which("pandoc"),
            "pdftotext": shutil.which("pdftotext"),
            "tesseract": shutil.which("tesseract"),
        },
    }
    return ToolResult(True, _json(data), data)


def _run_tests(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command = str(args.get("command") or "").strip()
    if not command:
        command = "python -m pytest" if (ctx.sandbox.root / "pyproject.toml").exists() else "python -m unittest discover"
    if args.get("args"):
        command = f"{command} {args['args']}"
    if not _permission(ctx, f"test:{command}", f"run test command: {command}"):
        return ToolResult(False, "Permission denied.")
    result = ctx.sandbox.run(command, int(args.get("timeout", 120)))
    return ToolResult(result.ok, result.output or f"Process exited with {result.returncode}", {"returncode": result.returncode, "command": command})


def _validate_data(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    value = args.get("data")
    if value is None and args.get("path"):
        value = ctx.sandbox.resolve(args["path"]).read_text(encoding="utf-8", errors="ignore")
    fmt = str(args.get("format") or "json").lower()
    try:
        if fmt == "json":
            parsed = json.loads(str(value))
            return ToolResult(True, _json({"valid": True, "type": type(parsed).__name__}))
        if fmt in {"csv", "tsv"}:
            delimiter = "\t" if fmt == "tsv" else ","
            lines = str(value or "").splitlines()
            widths = [len(line.split(delimiter)) for line in lines if line]
            return ToolResult(True, _json({"valid": len(set(widths)) <= 1, "rows": len(widths), "columns": widths[0] if widths else 0}))
    except Exception as exc:
        return ToolResult(False, _json({"valid": False, "error": str(exc)}))
    return ToolResult(False, f"Unsupported format: {fmt}")


def _project_map(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    max_files = max(10, min(int(args.get("max_files", 200)), 1000))
    rows = []
    for path in ctx.sandbox.root.rglob("*"):
        rel = path.relative_to(ctx.sandbox.root).as_posix()
        if any(part in {".git", "__pycache__", ".venv", "node_modules"} for part in path.parts):
            continue
        if path.is_dir() and len(path.relative_to(ctx.sandbox.root).parts) <= 2:
            rows.append(rel + "/")
        elif path.is_file() and path.suffix in {".py", ".md", ".toml", ".json", ".yaml", ".yml"}:
            rows.append(rel)
        if len(rows) >= max_files:
            break
    return ToolResult(True, "\n".join(rows) if rows else "(empty)", {"count": len(rows)})


def _retrieve_tool_result(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = args.get("path") or args.get("handle") or args.get("tool_result_id")
    if not path:
        return ToolResult(False, "LilBot stores large results inline in this phase; provide a path/handle to read.")
    read_args = dict(args)
    read_args["path"] = str(path)
    read_args.setdefault("limit", 12000)
    return _read_file(read_args, ctx)


def _handle_read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return _retrieve_tool_result(args, ctx)


def _bash(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command = args["command"]
    if not _permission(ctx, f"bash:{command}", f"run shell command: {command}"):
        return ToolResult(False, "Permission denied.")
    if bool(args.get("background", False)):
        return _task_shell_start(args, ctx)
    result = ctx.sandbox.run(command, int(args.get("timeout", 30)))
    return ToolResult(result.ok, result.output or f"Process exited with {result.returncode}", {"returncode": result.returncode})


def _start_process(command: str, ctx: ToolContext) -> subprocess.Popen:
    if os.name == "nt":
        argv: Any = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        shell = False
    else:
        argv = command
        shell = True
    return subprocess.Popen(
        argv,
        cwd=ctx.sandbox.root,
        shell=shell,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _task_shell_start(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command = str(args.get("command") or "")
    if not command:
        return ToolResult(False, "Missing required command.")
    if not _permission(ctx, f"bash:{command}", f"start background command: {command}"):
        return ToolResult(False, "Permission denied.")
    try:
        proc = _start_process(command, ctx)
    except OSError as exc:
        return ToolResult(False, f"Failed to start command: {exc}")
    task_id = f"sh_{uuid4().hex[:10]}"
    with _SHELL_LOCK:
        _SHELL_JOBS[task_id] = {
            "id": task_id,
            "command": command,
            "proc": proc,
            "started_at": time.time(),
            "output": "",
            "status": "running",
            "returncode": None,
        }
    return ToolResult(True, _json({"task_id": task_id, "status": "running", "command": command}), {"task_id": task_id, "status": "running"})


def _task_shell_wait(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task_id = str(args.get("task_id") or args.get("id") or "")
    timeout = max(0.0, min(float(args.get("timeout", args.get("timeout_secs", 1))), 120.0))
    if not task_id:
        return ToolResult(False, "Missing required task_id.")
    with _SHELL_LOCK:
        job = _SHELL_JOBS.get(task_id)
    if not job:
        return ToolResult(False, f"Unknown shell task: {task_id}")
    proc: subprocess.Popen = job["proc"]
    try:
        output, _ = proc.communicate(timeout=timeout)
        job["output"] = (job.get("output") or "") + (output or "")
        job["returncode"] = proc.returncode
        job["status"] = "completed" if proc.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        job["status"] = "running"
    duration_ms = int((time.time() - float(job["started_at"])) * 1000)
    data = {
        "task_id": task_id,
        "status": job["status"],
        "returncode": job["returncode"],
        "duration_ms": duration_ms,
        "output": (job.get("output") or "")[-12000:],
    }
    return ToolResult(job["status"] != "failed", _json(data), data)


def _exec_shell_interact(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task_id = str(args.get("task_id") or args.get("id") or "")
    text = str(args.get("input") or args.get("stdin") or args.get("data") or "")
    with _SHELL_LOCK:
        job = _SHELL_JOBS.get(task_id)
    if not job:
        return ToolResult(False, f"Unknown shell task: {task_id}")
    proc: subprocess.Popen = job["proc"]
    if proc.stdin and text:
        proc.stdin.write(text)
        if not text.endswith("\n"):
            proc.stdin.write("\n")
        proc.stdin.flush()
    if args.get("close_stdin") and proc.stdin:
        proc.stdin.close()
    return _task_shell_wait({"task_id": task_id, "timeout": args.get("timeout", 1)}, ctx)


def _exec_shell_cancel(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task_id = str(args.get("task_id") or args.get("id") or "")
    with _SHELL_LOCK:
        targets = list(_SHELL_JOBS.values()) if args.get("all") else [_SHELL_JOBS.get(task_id)]
    cancelled = []
    for job in targets:
        if not job:
            continue
        proc: subprocess.Popen = job["proc"]
        if proc.poll() is None:
            proc.terminate()
        job["status"] = "cancelled"
        job["returncode"] = proc.poll()
        cancelled.append(job["id"])
    return ToolResult(bool(cancelled), _json({"cancelled": cancelled}) if cancelled else "No matching running task.")


def _glob(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = ctx.sandbox.glob(args["pattern"], args.get("path", "."))
    return ToolResult(True, "\n".join(rows) if rows else "(no matches)")


def _grep(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    pattern_text = str(args["pattern"])
    try:
        pattern = re.compile(pattern_text, 0 if args.get("case_sensitive") else re.IGNORECASE)
    except re.error as exc:
        return ToolResult(False, f"Invalid regex pattern: {exc}")
    base = ctx.sandbox.resolve(args.get("path", "."))
    glob_pattern = args.get("glob")
    max_results = max(1, min(int(args.get("max_results", 80)), 1000))
    before = max(0, min(int(args.get("before", args.get("context", 0))), 20))
    after = max(0, min(int(args.get("after", args.get("context", 0))), 20))
    matches = []
    for file_path in base.rglob("*"):
        if len(matches) >= max_results:
            break
        if not file_path.is_file() or _is_noisy_path(file_path, ctx.sandbox.root):
            continue
        rel = file_path.relative_to(ctx.sandbox.root).as_posix()
        if glob_pattern and not fnmatch.fnmatch(rel, str(glob_pattern)) and not fnmatch.fnmatch(file_path.name, str(glob_pattern)):
            continue
        try:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines, 1):
            if pattern.search(line):
                start = max(1, idx - before)
                end = min(len(lines), idx + after)
                matches.append({
                    "path": rel,
                    "line": idx,
                    "text": line.strip(),
                    "context": _numbered_slice(lines, start, end) if before or after else None,
                })
                if len(matches) >= max_results:
                    break
    rows = [f"{item['path']}:{item['line']}: {item['text']}" for item in matches]
    return ToolResult(True, "\n".join(rows) if rows else "(no matches)", {"matches": matches, "count": len(matches), "pattern": pattern_text})


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


def _note(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    text = str(args.get("content") or args.get("text") or "")
    if not text:
        return ToolResult(False, "Missing note content.")
    entry = ctx.memory.add("note", text, "note", "project")
    return ToolResult(True, f"Saved note {entry.id}.", {"id": entry.id})


def _remember(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    text = str(args.get("memory") or args.get("text") or args.get("content") or "")
    if not text:
        return ToolResult(False, "Missing memory text.")
    entry = ctx.memory.add(str(args.get("name") or "remembered"), text, "memory", str(args.get("scope") or "project"))
    return ToolResult(True, f"Remembered {entry.id}: {entry.name}", {"id": entry.id})


def _update_plan(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    plan = args.get("plan")
    if not isinstance(plan, list):
        return ToolResult(False, "Missing required plan array.")
    data = {
        "explanation": args.get("explanation", ""),
        "plan": plan,
        "updated_at": time.time(),
    }
    path = _save_state(ctx, "plan.json", data)
    return ToolResult(True, _json(data), {"path": str(path)})


def _enter_plan_mode(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    data = {
        "active": True,
        "approval_state": "planning",
        "approved": False,
        "reason": str(args.get("reason") or args.get("description") or ""),
        "plan": args.get("plan"),
        "entered_at": time.time(),
        "updated_at": time.time(),
    }
    path = _save_state(ctx, "plan_mode.json", data)
    data["path"] = str(path)
    return ToolResult(True, _json(data), data)


def _exit_plan_mode(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    current = _load_state(ctx, "plan_mode.json", {})
    approved = _optional_bool(args.get("approved"))
    requested_state = str(args.get("approval_state") or args.get("state") or "").strip().lower()
    if requested_state in {"approved", "approve"}:
        approval_state = "approved"
    elif requested_state in {"rejected", "reject", "denied", "deny"}:
        approval_state = "rejected"
    elif approved is True:
        approval_state = "approved"
    elif approved is False:
        approval_state = "rejected"
    else:
        approval_state = "pending_approval"
    data = dict(current) if isinstance(current, dict) else {}
    data.update({
        "active": False,
        "approval_state": approval_state,
        "approved": approval_state == "approved",
        "requires_approval": approval_state == "pending_approval",
        "plan": args.get("plan", data.get("plan")),
        "summary": str(args.get("summary") or args.get("message") or ""),
        "exited_at": time.time(),
        "updated_at": time.time(),
    })
    path = _save_state(ctx, "plan_mode.json", data)
    data["path"] = str(path)
    return ToolResult(True, _json(data), data)


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y", "approved", "approve"}:
        return True
    if lowered in {"0", "false", "no", "n", "rejected", "reject", "denied", "deny"}:
        return False
    return None


def _checklist_state(ctx: ToolContext) -> list[dict[str, Any]]:
    return _load_state(ctx, "checklist.json", [])


def _save_checklist(ctx: ToolContext, items: list[dict[str, Any]]) -> None:
    _save_state(ctx, "checklist.json", items)


def _checklist_write(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    items = args.get("items") or args.get("checklist") or args.get("todos") or []
    if not isinstance(items, list):
        return ToolResult(False, "items must be an array.")
    normalized = []
    for index, item in enumerate(items, 1):
        if isinstance(item, str):
            item = {"id": str(index), "content": item, "status": "pending"}
        normalized.append({
            "id": str(item.get("id") or index),
            "content": str(item.get("content") or item.get("task") or item.get("step") or ""),
            "status": str(item.get("status") or "pending"),
        })
    _save_checklist(ctx, normalized)
    return ToolResult(True, _json(normalized), {"count": len(normalized)})


def _checklist_add(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    items = _checklist_state(ctx)
    item = {
        "id": str(args.get("id") or uuid4().hex[:8]),
        "content": str(args.get("content") or args.get("task") or ""),
        "status": str(args.get("status") or "pending"),
    }
    items.append(item)
    _save_checklist(ctx, items)
    return ToolResult(True, _json(item), item)


def _checklist_update(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    item_id = str(args.get("id") or args.get("item_id") or "")
    items = _checklist_state(ctx)
    for item in items:
        if str(item.get("id")) == item_id or str(item.get("content")) == item_id:
            if args.get("content") is not None:
                item["content"] = str(args["content"])
            if args.get("status") is not None:
                item["status"] = str(args["status"])
            _save_checklist(ctx, items)
            return ToolResult(True, _json(item), item)
    return ToolResult(False, "Checklist item not found.")


def _checklist_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    items = _checklist_state(ctx)
    return ToolResult(True, _json(items) if items else "(empty)", {"count": len(items)})


def _create_goal(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    objective = str(args.get("objective") or "")
    if not objective:
        return ToolResult(False, "Missing objective.")
    data = {
        "objective": objective,
        "status": "active",
        "token_budget": args.get("token_budget"),
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    _save_state(ctx, "goal.json", data)
    return ToolResult(True, _json(data), data)


def _get_goal(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    data = _load_state(ctx, "goal.json", {})
    return ToolResult(True, _json(data) if data else "No active goal.", data if isinstance(data, dict) else {})


def _update_goal(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    data = _load_state(ctx, "goal.json", {})
    if not data:
        return ToolResult(False, "No goal exists.")
    status = str(args.get("status") or "")
    if status not in {"active", "complete", "blocked"}:
        return ToolResult(False, "status must be active, complete, or blocked.")
    data["status"] = status
    data["updated_at"] = time.time()
    _save_state(ctx, "goal.json", data)
    return ToolResult(True, _json(data), data)


def _task_records(ctx: ToolContext) -> list[dict[str, Any]]:
    return _load_state(ctx, "tasks.json", [])


def _save_tasks(ctx: ToolContext, tasks: list[dict[str, Any]]) -> None:
    _save_state(ctx, "tasks.json", tasks)


def _task_create(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    tasks = _task_records(ctx)
    task = {
        "id": "task_" + uuid4().hex[:10],
        "prompt": str(args.get("prompt") or args.get("title") or ""),
        "status": "queued",
        "created_at": time.time(),
        "updated_at": time.time(),
        "timeline": [{"event": "created", "at": time.time()}],
        "gates": [],
        "artifacts": [],
    }
    tasks.append(task)
    _save_tasks(ctx, tasks)
    return ToolResult(True, _json(task), task)


def _task_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    tasks = _task_records(ctx)
    return ToolResult(True, _json(tasks) if tasks else "(no tasks)", {"count": len(tasks)})


def _task_read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task_id = str(args.get("task_id") or args.get("id") or "")
    for task in _task_records(ctx):
        if task.get("id") == task_id:
            return ToolResult(True, _json(task), task)
    return ToolResult(False, "Task not found.")


def _task_cancel(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task_id = str(args.get("task_id") or args.get("id") or "")
    tasks = _task_records(ctx)
    for task in tasks:
        if task.get("id") == task_id:
            task["status"] = "canceled"
            task["updated_at"] = time.time()
            task.setdefault("timeline", []).append({"event": "canceled", "at": time.time()})
            _save_tasks(ctx, tasks)
            return ToolResult(True, _json(task), task)
    return ToolResult(False, "Task not found.")


def _task_gate_run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command = str(args.get("command") or "")
    if not command:
        return ToolResult(False, "Missing command.")
    if not _permission(ctx, f"gate:{command}", f"run verification gate: {command}"):
        return ToolResult(False, "Permission denied.")
    started = time.time()
    result = ctx.sandbox.run(command, int(args.get("timeout", 120)))
    gate = {
        "command": command,
        "exit_code": result.returncode,
        "ok": result.ok,
        "duration_ms": int((time.time() - started) * 1000),
        "summary": (result.output or "")[:1000],
    }
    task_id = args.get("task_id")
    if task_id:
        tasks = _task_records(ctx)
        for task in tasks:
            if task.get("id") == task_id:
                task.setdefault("gates", []).append(gate)
                task["updated_at"] = time.time()
        _save_tasks(ctx, tasks)
    return ToolResult(result.ok, _json(gate), gate)


def _pr_attempts(ctx: ToolContext) -> list[dict[str, Any]]:
    return _load_state(ctx, "pr_attempts.json", [])


def _save_pr_attempts(ctx: ToolContext, attempts: list[dict[str, Any]]) -> None:
    _save_state(ctx, "pr_attempts.json", attempts)


def _pr_attempt_record(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    diff = ctx.sandbox.run("git diff --binary", 60)
    attempts = _pr_attempts(ctx)
    attempt = {
        "id": "attempt_" + uuid4().hex[:10],
        "task_id": args.get("task_id"),
        "message": args.get("message", ""),
        "created_at": time.time(),
        "patch": diff.output,
    }
    attempts.append(attempt)
    _save_pr_attempts(ctx, attempts)
    return ToolResult(True, _json({k: v for k, v in attempt.items() if k != "patch"}), {"id": attempt["id"], "patch_chars": len(diff.output)})


def _pr_attempt_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    attempts = _pr_attempts(ctx)
    summary = [{k: v for k, v in item.items() if k != "patch"} for item in attempts]
    return ToolResult(True, _json(summary) if summary else "(no attempts)", {"count": len(summary)})


def _pr_attempt_read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    attempt_id = str(args.get("attempt_id") or args.get("id") or "")
    for attempt in _pr_attempts(ctx):
        if attempt.get("id") == attempt_id:
            return ToolResult(True, _json(attempt), {"patch_chars": len(attempt.get("patch", ""))})
    return ToolResult(False, "Attempt not found.")


def _pr_attempt_preflight(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    attempt_id = str(args.get("attempt_id") or args.get("id") or "")
    patch = None
    for attempt in _pr_attempts(ctx):
        if attempt.get("id") == attempt_id:
            patch = attempt.get("patch", "")
            break
    if patch is None:
        return ToolResult(False, "Attempt not found.")
    tmp_dir = ctx.sandbox.resolve(".lilbot/tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    patch_path = tmp_dir / f"preflight-{uuid4().hex}.diff"
    patch_path.write_text(patch, encoding="utf-8")
    rel = patch_path.relative_to(ctx.sandbox.root).as_posix()
    result = ctx.sandbox.run(f"git apply --check {_quote_ps(rel)}", 60)
    patch_path.unlink(missing_ok=True)
    return ToolResult(result.ok, result.output or ("Patch applies cleanly." if result.ok else "Patch does not apply."), {"returncode": result.returncode})


def _automations(ctx: ToolContext) -> list[dict[str, Any]]:
    return _load_state(ctx, "automations.json", [])


def _save_automations(ctx: ToolContext, automations: list[dict[str, Any]]) -> None:
    _save_state(ctx, "automations.json", automations)


def _automation_create(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    items = _automations(ctx)
    item = {
        "id": "auto_" + uuid4().hex[:10],
        "name": str(args.get("name") or "automation"),
        "prompt": str(args.get("prompt") or ""),
        "rrule": str(args.get("rrule") or args.get("schedule") or ""),
        "status": "active",
        "created_at": time.time(),
        "updated_at": time.time(),
        "runs": [],
    }
    items.append(item)
    _save_automations(ctx, items)
    return ToolResult(True, _json(item), item)


def _automation_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    items = _automations(ctx)
    return ToolResult(True, _json(items) if items else "(no automations)", {"count": len(items)})


def _automation_read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    automation_id = str(args.get("automation_id") or args.get("id") or "")
    for item in _automations(ctx):
        if item.get("id") == automation_id:
            return ToolResult(True, _json(item), item)
    return ToolResult(False, "Automation not found.")


def _automation_update(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    automation_id = str(args.get("automation_id") or args.get("id") or "")
    items = _automations(ctx)
    for item in items:
        if item.get("id") == automation_id:
            for key in ("name", "prompt", "rrule", "status"):
                if args.get(key) is not None:
                    item[key] = args[key]
            item["updated_at"] = time.time()
            _save_automations(ctx, items)
            return ToolResult(True, _json(item), item)
    return ToolResult(False, "Automation not found.")


def _automation_lifecycle(args: dict[str, Any], ctx: ToolContext, status: str) -> ToolResult:
    args = dict(args)
    args["status"] = status
    return _automation_update(args, ctx)


def _automation_delete(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    automation_id = str(args.get("automation_id") or args.get("id") or "")
    items = _automations(ctx)
    kept = [item for item in items if item.get("id") != automation_id]
    _save_automations(ctx, kept)
    return ToolResult(len(kept) != len(items), "Deleted." if len(kept) != len(items) else "Automation not found.")


def _automation_run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    automation_id = str(args.get("automation_id") or args.get("id") or "")
    items = _automations(ctx)
    for item in items:
        if item.get("id") == automation_id:
            task = _task_create({"prompt": item.get("prompt", ""), "title": item.get("name", "")}, ctx)
            run = {"at": time.time(), "task": task.metadata}
            item.setdefault("runs", []).append(run)
            item["updated_at"] = time.time()
            _save_automations(ctx, items)
            return ToolResult(True, _json(run), run)
    return ToolResult(False, "Automation not found.")


def _skill_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    include_hidden = bool(args.get("include_hidden", False))
    rows = []
    skills = ctx.skills.list(include_hidden=include_hidden)
    for s in skills:
        when = f" | when: {s.when_to_use}" if s.when_to_use else ""
        aliases = f" | aliases: {', '.join(s.aliases or [])}" if s.aliases else ""
        rows.append(f"{s.name} [{s.mode}] - {s.description}{when}{aliases}")
    return ToolResult(True, "\n".join(rows) if rows else "(no skills)", {"count": len(skills)})


def _skill_run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    name = str(args.get("name") or args.get("skill") or "")
    skill = ctx.skills.get(name)
    if not skill:
        known = ", ".join(s.name for s in ctx.skills.list()) or "none"
        return ToolResult(False, f"Unknown skill '{name}'. Known skills: {known}")
    if skill.disable_model_invocation:
        return ToolResult(False, f"skill '{skill.name}' cannot be invoked by the model")
    rendered = skill.render(args.get("args", ""))
    metadata = {
        "name": skill.name,
        "mode": skill.mode,
        "allowed_tools": skill.allowed_tools or [],
        "agent": skill.agent,
    }
    if skill.mode.lower() != "fork":
        return ToolResult(True, rendered, metadata)
    if ctx.subagents is None:
        return ToolResult(False, "Forked skill execution requires subagent support.", metadata)
    try:
        task = ctx.subagents.open(
            agent_type=skill.agent or "custom",
            prompt=rendered,
            name=f"skill_{skill.name}_{uuid4().hex[:8]}",
            background=bool(args.get("background", False)),
            allowed_tools=skill.allowed_tools or [],
            model=skill.model,
            fork_context=True,
        )
    except SubAgentGateError as exc:
        data = exc.to_dict()
        metadata.update({"gate": "subagent_creation", "gates": exc.failures})
        return ToolResult(False, _json(data), metadata)
    projection = ctx.subagents.projection(task)
    metadata.update({
        "agent_id": task.id,
        "status": task.status,
        "transcript_handle": task.transcript_handle or None,
    })
    return ToolResult(task.status not in {"failed", "error"}, _json(projection), metadata)


def _load_skill(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    name = str(args.get("name") or args.get("skill") or "")
    skill = ctx.skills.get(name)
    if not skill:
        known = ", ".join(s.name for s in ctx.skills.list()) or "none"
        return ToolResult(False, f"Unknown skill '{name}'. Known skills: {known}")
    companions = [str(path) for path in (skill.companion_files or [])]
    data = {
        "name": skill.name,
        "description": skill.description,
        "mode": skill.mode,
        "aliases": skill.aliases or [],
        "when_to_use": skill.when_to_use,
        "argument_hint": skill.argument_hint,
        "argument_names": skill.argument_names or [],
        "allowed_tools": skill.allowed_tools or [],
        "model": skill.model,
        "disable_model_invocation": skill.disable_model_invocation,
        "user_invocable": skill.user_invocable,
        "agent": skill.agent,
        "effort": skill.effort,
        "paths": skill.paths or [],
        "shell": skill.shell,
        "source": str(skill.source),
        "companion_files": companions,
        "body": skill.body,
    }
    metadata = {
        "name": skill.name,
        "source": str(skill.source),
        "mode": skill.mode,
        "allowed_tools": skill.allowed_tools or [],
        "companion_count": len(companions),
    }
    return ToolResult(True, _json(data), metadata)


def _agent_spawn(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        task = ctx.subagents.open(
            agent_type=args.get("agent_type", "planner"),
            prompt=args["prompt"],
            background=bool(args.get("background", False)),
            allowed_tools=_tool_list_arg(args),
        )
    except SubAgentGateError as exc:
        return ToolResult(False, _json(exc.to_dict()), {"gate": "subagent_creation", "gates": exc.failures})
    if task.status in {"completed", "done"}:
        return ToolResult(True, f"{task.id} done:\n{task.result}", {"task_id": task.id})
    return ToolResult(True, f"{task.id} {task.status}", {"task_id": task.id})


def _agent_status(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task = ctx.subagents.get(args["task_id"])
    if not task:
        return ToolResult(False, "Task not found.")
    body = task.result if task.status in {"done", "completed"} else task.error or task.status
    return ToolResult(task.status != "error", f"{task.id} [{task.status}] {task.agent_type}\n{body}")


def _agent_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = [f"{d.name}: {d.description}" for d in ctx.subagents.list_types()]
    rows += [f"{t.id} [{t.status}] {t.agent_type}: {t.prompt[:80]}" for t in ctx.subagents.list_tasks()]
    return ToolResult(True, "\n".join(rows) if rows else "(no agent data)")


def _agent_open(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    prompt = str(args.get("prompt") or args.get("message") or args.get("objective") or "")
    if not prompt:
        return ToolResult(False, "Missing prompt.")
    try:
        task = ctx.subagents.open(
            args.get("type") or args.get("agent_type") or args.get("subagent_type") or args.get("role"),
            prompt,
            name=args.get("name") or args.get("session_name"),
            background=bool(args.get("background", args.get("run_in_background", True))),
            allowed_tools=_tool_list_arg(args),
            model=args.get("model"),
            fork_context=bool(args.get("fork_context", False)),
        )
    except SubAgentGateError as exc:
        return ToolResult(False, _json(exc.to_dict()), {"gate": "subagent_creation", "gates": exc.failures})
    projection = ctx.subagents.projection(task)
    return ToolResult(True, _json(projection), projection)


def _tool_agent(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    args = dict(args)
    args["type"] = "tool_agent"
    args.setdefault("background", True)
    return _agent_open(args, ctx)


def _tool_list_arg(args: dict[str, Any]) -> list[str] | None:
    value = args.get("allowed_tools")
    if value is None:
        value = args.get("allowed_tools_list")
    if value is None:
        value = args.get("tools")
    if value in (None, ""):
        return None
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _agent_eval(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    ref = str(args.get("name") or args.get("agent_id") or args.get("id") or "")
    if not ref:
        return ToolResult(False, "Missing name or agent_id.")
    task = ctx.subagents.eval(
        ref,
        message=args.get("message") or args.get("input"),
        block=bool(args.get("block", True)),
        timeout=float(args.get("timeout_ms", 30000)) / 1000.0,
    )
    if not task:
        return ToolResult(False, "Subagent not found.")
    projection = ctx.subagents.projection(task)
    return ToolResult(task.status not in {"failed", "error"}, _json(projection), projection)


def _agent_close(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    ref = str(args.get("name") or args.get("agent_id") or args.get("id") or "")
    if not ref:
        return ToolResult(False, "Missing name or agent_id.")
    task = ctx.subagents.close(ref)
    if not task:
        return ToolResult(False, "Subagent not found.")
    projection = ctx.subagents.projection(task)
    return ToolResult(True, _json(projection), projection)


def _mcp_servers(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    servers = ctx.mcp.list_servers()
    if not servers:
        path = ctx.mcp.write_example_config()
        return ToolResult(True, f"No MCP servers configured. Example config created at {path}")
    return ToolResult(True, "\n".join(f"{s.name}: {s.command} {' '.join(s.args)}" for s in servers))


def _mcp_call(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    output = ctx.mcp.call_tool(args["server"], args["tool"], args.get("arguments", {}))
    return ToolResult(True, output)


def _mcp_list_resources(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    servers = [s.name for s in ctx.mcp.list_servers()]
    data = {"servers": servers, "resources": [], "note": "Resource discovery is not implemented by LilBot's JSON-RPC-lines adapter yet."}
    return ToolResult(True, _json(data), data)


def _mcp_read_resource(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    uri = str(args.get("uri") or "")
    if uri.startswith("file:"):
        return _read_file({"path": uri.removeprefix("file:"), "limit": args.get("limit", 12000)}, ctx)
    return ToolResult(False, "Only file: resource URIs are supported in this phase.")


def _rlm_session_objects(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    data = {
        "sessions": [
            {"name": name, "source": session.get("source"), "created_at": session.get("created_at")}
            for name, session in sorted(_RLM_SESSIONS.items())
        ],
        "objects": ["session://active/transcript", "session://active/context"],
    }
    return ToolResult(True, _json(data), data)


def _rlm_open(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    name = str(args.get("name") or f"rlm_{uuid4().hex[:8]}")
    content = str(args.get("content") or "")
    source = "inline"
    if args.get("path"):
        path = ctx.sandbox.resolve(args["path"])
        content = path.read_text(encoding="utf-8", errors="ignore")
        source = str(path)
    elif args.get("url"):
        fetched = _fetch_url({"url": args["url"], "format": "text", "max_chars": args.get("max_chars", 40000)}, ctx)
        if not fetched.ok:
            return fetched
        payload = json.loads(fetched.output)
        content = payload.get("content", "")
        source = payload.get("url", args["url"])
    env: dict[str, Any] = {"_context": content, "_ctx": content, "content": content}
    _RLM_SESSIONS[name] = {"name": name, "source": source, "created_at": time.time(), "env": env, "config": {}}
    data = {"name": name, "source": source, "chars": len(content)}
    return ToolResult(True, _json(data), data)


def _rlm_eval(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    name = str(args.get("name") or args.get("session") or "")
    code = str(args.get("code") or "")
    session = _RLM_SESSIONS.get(name)
    if not session:
        return ToolResult(False, "RLM session not found.")
    if not code:
        return ToolResult(False, "Missing code.")
    env = session["env"]
    final: dict[str, Any] = {}

    def finalize(value: Any, confidence: str | None = None) -> Any:
        final["value"] = value
        if confidence is not None:
            final["confidence"] = confidence
        return value

    env["finalize"] = finalize
    old_stdout = sys.stdout
    capture = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    try:
        sys.stdout = capture
        exec(code, env, env)
    except Exception as exc:
        sys.stdout = old_stdout
        capture.seek(0)
        return ToolResult(False, _json({"error": f"{type(exc).__name__}: {exc}", "stdout": capture.read()}))
    finally:
        sys.stdout = old_stdout
    capture.seek(0)
    data = {"stdout": capture.read(), "final": final or None}
    return ToolResult(True, _json(data), data)


def _rlm_configure(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    name = str(args.get("name") or args.get("session") or "")
    session = _RLM_SESSIONS.get(name)
    if not session:
        return ToolResult(False, "RLM session not found.")
    session["config"].update({k: v for k, v in args.items() if k not in {"name", "session"}})
    return ToolResult(True, _json(session["config"]), session["config"])


def _rlm_close(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    name = str(args.get("name") or args.get("session") or "")
    session = _RLM_SESSIONS.pop(name, None)
    return ToolResult(bool(session), "Closed." if session else "RLM session not found.")


def _slop_entries(ctx: ToolContext) -> list[dict[str, Any]]:
    return _load_state(ctx, "slop_ledger.json", [])


def _save_slop(ctx: ToolContext, entries: list[dict[str, Any]]) -> None:
    _save_state(ctx, "slop_ledger.json", entries)


def _slop_ledger_append(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    entries = _slop_entries(ctx)
    raw_entries = args.get("entries") if isinstance(args.get("entries"), list) else [args]
    added = []
    for raw in raw_entries:
        item = {
            "id": "slop_" + uuid4().hex[:8],
            "bucket": raw.get("bucket", "tool_gaps"),
            "severity": raw.get("severity", "medium"),
            "confidence": raw.get("confidence", "medium"),
            "title": raw.get("title", "Untitled residue"),
            "description": raw.get("description", ""),
            "status": raw.get("status", "open"),
            "source_links": raw.get("source_links", []),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        entries.append(item)
        added.append(item)
    _save_slop(ctx, entries)
    return ToolResult(True, _json(added), {"count": len(added)})


def _slop_ledger_query(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    entries = _slop_entries(ctx)
    search = str(args.get("search") or args.get("query") or "").lower()
    status = args.get("status")
    bucket = args.get("bucket")
    results = []
    for item in entries:
        if status and item.get("status") != status:
            continue
        if bucket and item.get("bucket") != bucket:
            continue
        if search and search not in _json(item).lower():
            continue
        results.append(item)
    return ToolResult(True, _json(results) if results else "(no entries)", {"count": len(results)})


def _slop_ledger_update(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    item_id = str(args.get("id") or args.get("entry_id") or "")
    entries = _slop_entries(ctx)
    for item in entries:
        if item.get("id") == item_id or str(item.get("id", "")).startswith(item_id):
            for key in ("bucket", "severity", "confidence", "title", "description", "status", "cleanup_recommendation"):
                if args.get(key) is not None:
                    item[key] = args[key]
            item["updated_at"] = time.time()
            _save_slop(ctx, entries)
            return ToolResult(True, _json(item), item)
    return ToolResult(False, "Slop entry not found.")


def _slop_ledger_export(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    entries = _slop_entries(ctx)
    lines = ["# Slop Ledger Export", ""]
    for item in entries:
        lines.append(f"## {item.get('id')} - {item.get('title')}")
        lines.append(f"- Bucket: {item.get('bucket')}")
        lines.append(f"- Severity: {item.get('severity')}")
        lines.append(f"- Status: {item.get('status')}")
        lines.append("")
        lines.append(str(item.get("description", "")))
        lines.append("")
    text = "\n".join(lines)
    if args.get("path"):
        if not _permission(ctx, f"write:{args['path']}", f"export slop ledger to {args['path']}"):
            return ToolResult(False, "Permission denied.")
        path = ctx.sandbox.resolve(args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return ToolResult(True, text)


def _external_missing(tool: str, dependency: str) -> ToolResult:
    found = shutil.which(dependency)
    if found:
        return ToolResult(True, _json({"tool": tool, "dependency": dependency, "path": found, "available": True}))
    return ToolResult(False, _json({"tool": tool, "dependency": dependency, "available": False, "message": f"{dependency} is not installed or not on PATH."}))


def _pandoc_convert(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not shutil.which("pandoc"):
        return _external_missing("pandoc_convert", "pandoc")
    source = str(args.get("input") or args.get("path") or "")
    output = str(args.get("output") or "")
    if not source or not output:
        return ToolResult(False, "input/path and output are required.")
    if not _permission(ctx, f"pandoc:{source}->{output}", f"convert {source} to {output}"):
        return ToolResult(False, "Permission denied.")
    return _run_readonly(ctx, f"pandoc {_quote_ps(source)} -o {_quote_ps(output)}", 120)


def _image_ocr(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not shutil.which("tesseract"):
        return _external_missing("image_ocr", "tesseract")
    image = str(args.get("image_path") or args.get("path") or "")
    if not image:
        return ToolResult(False, "Missing image_path.")
    return _run_readonly(ctx, f"tesseract {_quote_ps(image)} stdout", 120)


def _image_analyze(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = str(args.get("image_path") or args.get("path") or "")
    if not path:
        return ToolResult(False, "Missing image_path.")
    resolved = ctx.sandbox.resolve(path)
    data = {"image_path": str(resolved), "bytes": resolved.stat().st_size, "message": "Vision model analysis is not configured in LilBot phase 1."}
    return ToolResult(False, _json(data), data)


def _notify(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult(True, str(args.get("message") or args.get("text") or "Notification requested."))


def _request_user_input(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult(False, "Interactive request_user_input is not available inside LilBot tool calls yet.")


def _review(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    target = str(args.get("target") or args.get("path") or ".")
    hits = ctx.sandbox.grep("TODO", target, None, 20)
    data = {"target": target, "summary": "Lightweight review complete.", "todo_hits": hits}
    return ToolResult(True, _json(data), data)


def _fim_edit(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult(False, "fim_edit requires a configured fill-in-the-middle model; use edit_file/apply_patch in this phase.")


def _finance(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    symbol = str(args.get("symbol") or args.get("ticker") or "")
    if not symbol:
        return ToolResult(False, "Missing symbol/ticker.")
    return _fetch_url({"url": f"https://stooq.com/q/l/?s={urllib.parse.quote(symbol.lower())}&f=sd2t2ohlcv&h&e=csv", "format": "raw", "max_chars": 4000}, ctx)


def _code_execution(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    code = str(args.get("code") or "")
    if not code:
        return ToolResult(False, "Missing code.")
    if not _permission(ctx, "code_execution", "execute Python code"):
        return ToolResult(False, "Permission denied.")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
        handle.write(code)
        script = handle.name
    try:
        proc = subprocess.run([sys.executable, script], cwd=ctx.sandbox.root, text=True, capture_output=True, timeout=120)
        data = {"stdout": proc.stdout, "stderr": proc.stderr, "return_code": proc.returncode}
        return ToolResult(proc.returncode == 0, _json(data), data)
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass


def _js_execution(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not shutil.which("node"):
        return _external_missing("js_execution", "node")
    code = str(args.get("code") or "")
    if not code:
        return ToolResult(False, "Missing code.")
    if not _permission(ctx, "js_execution", "execute JavaScript code"):
        return ToolResult(False, "Permission denied.")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(code)
        script = handle.name
    try:
        proc = subprocess.run(["node", script], cwd=ctx.sandbox.root, text=True, capture_output=True, timeout=120)
        data = {"stdout": proc.stdout, "stderr": proc.stderr, "return_code": proc.returncode}
        return ToolResult(proc.returncode == 0, _json(data), data)
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass


def _revert_turn(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult(False, "revert_turn needs snapshot support; use git diff/status to inspect and manually revert in this phase.")


def _recall_archive(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    archive = ctx.config.state_dir / "archives"
    if not archive.exists():
        return ToolResult(True, "(no archives)")
    query = str(args.get("query") or "").lower()
    rows = []
    for path in archive.glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not query or query in text.lower():
            rows.append(f"{path.name}: {text[:300]}")
    return ToolResult(True, "\n".join(rows) if rows else "(no matches)")


def _tool_search(args: dict[str, Any], registry: ToolRegistry, *, regex: bool) -> ToolResult:
    query = str(args.get("query") or "")
    limit = max(1, min(int(args.get("max_results", 20)), 100))
    rows = []
    if regex:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            return ToolResult(False, f"Invalid regex: {exc}")
        for tool in registry.list():
            haystack = f"{tool.name}\n{tool.description}\n{tool.input_schema}"
            if pattern.search(haystack):
                rows.append({"name": tool.name, "description": tool.description})
            if len(rows) >= limit:
                break
    else:
        terms = [term.lower() for term in query.split() if term.strip()]
        scored = []
        for tool in registry.list():
            haystack = f"{tool.name} {tool.description}".lower()
            score = sum(2 if term in tool.name.lower() else 1 for term in terms if term in haystack)
            if score:
                scored.append((score, tool.name, tool.description))
        scored.sort(key=lambda item: (-item[0], item[1]))
        rows = [{"name": name, "description": desc} for _, name, desc in scored[:limit]]
    return ToolResult(True, _json({"matches": rows, "count": len(rows)}), {"count": len(rows)})


def _parallel_tool(args: dict[str, Any], registry: ToolRegistry, ctx: ToolContext) -> ToolResult:
    uses = args.get("tool_uses") or args.get("tools") or []
    if not isinstance(uses, list):
        return ToolResult(False, "tool_uses must be an array.")
    results = []
    for item in uses:
        if not isinstance(item, dict):
            continue
        name = str(item.get("recipient_name") or item.get("tool_name") or item.get("name") or "")
        parameters = item.get("parameters") or item.get("arguments") or {}
        if name in {"multi_tool_use_parallel", "multi_tool_use.parallel"}:
            results.append({"tool": name, "ok": False, "output": "recursive parallel calls are refused"})
            continue
        result, elapsed_ms = registry.execute(name, parameters, ctx)
        results.append({"tool": name, "ok": result.ok, "elapsed_ms": elapsed_ms, "output": result.output, "metadata": result.metadata})
    return ToolResult(True, _json({"results": results, "count": len(results)}), {"count": len(results)})


def register_builtins(registry: ToolRegistry) -> None:
    registry.register(ToolDef("list_dir", "List files under a workspace path.", _schema({
        "path": _string("Directory path relative to workspace."),
        "max_depth": _integer("Recursive depth, 0-8.", 1),
        "limit": _integer("Maximum entries.", 500),
        "include_hidden": _bool("Include dotfiles and dot directories.", False),
    }), _list_dir))
    registry.register(ToolDef("read_file", "Read a UTF-8 text file inside the workspace.", _schema({
        "path": _string("File path relative to workspace."),
        "offset": _integer("Character offset.", 0),
        "limit": _integer("Maximum characters to return.", 4000),
        "lines": _string("Line range such as 10-40."),
        "head": _integer("Return first N lines."),
        "tail": _integer("Return last N lines."),
        "query": _string("Return matches for this text with context."),
        "context": _integer("Context lines for query mode.", 2),
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
        "pattern": _string("Regex pattern to search."),
        "path": _string("Base path relative to workspace."),
        "glob": _string("Optional filename glob, for example *.py."),
        "max_results": _integer("Maximum matches.", 80),
        "context": _integer("Context lines before and after each match.", 0),
        "before": _integer("Context lines before each match.", 0),
        "after": _integer("Context lines after each match.", 0),
        "case_sensitive": _bool("Use case-sensitive matching.", False),
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
    registry.register(ToolDef("skill_list", "List available skills.", _schema({
        "include_hidden": _bool("Include skills that are not user-invocable.", False),
    }), _skill_list))
    registry.register(ToolDef("skill_run", "Render a skill template.", _schema({
        "name": _string("Skill name."),
        "args": _string("Arguments injected into {{args}}."),
        "background": _bool("For forked skills, run in background and return immediately.", False),
    }, ["name"]), _skill_run))
    registry.register(ToolDef("Skill", "Execute a skill within the main conversation, Claude Code style.", _schema({
        "skill": _string("Skill name."),
        "args": _string("Optional skill arguments."),
        "background": _bool("For forked skills, run in background and return immediately.", False),
    }, ["skill"]), _skill_run))
    registry.register(ToolDef("agent_spawn", "Spawn a lightweight sub-agent.", _schema({
        "agent_type": _string("general, explore, researcher, plan, writer, critic, review, implementer, verifier, or tool_agent."),
        "prompt": _string("Task prompt."),
        "allowed_tools": {"type": "array", "items": {"type": "string"}},
        "allowed_tools_list": {"type": "array", "items": {"type": "string"}},
        "tools": {"type": "array", "items": {"type": "string"}},
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
    registry.register(ToolDef("apply_patch", "Apply a unified diff to the workspace with permission approval.", _schema({
        "patch": _string("Unified diff content."),
        "timeout": _integer("Timeout in seconds.", 30),
    }, ["patch"]), _apply_patch))
    registry.register(ToolDef("file_search", "Fuzzy-match workspace filenames.", _schema({
        "query": _string("Filename query."),
        "path": _string("Base path relative to workspace."),
        "limit": _integer("Maximum matches.", 50),
    }, ["query"]), _file_search))
    registry.register(ToolDef("grep_files", "Regex-like text search in workspace files.", _schema({
        "pattern": _string("Regex pattern to search."),
        "path": _string("Base path relative to workspace."),
        "glob": _string("Optional filename glob."),
        "max_results": _integer("Maximum matches.", 80),
        "context": _integer("Context lines before and after each match.", 0),
        "before": _integer("Context lines before each match.", 0),
        "after": _integer("Context lines after each match.", 0),
        "case_sensitive": _bool("Use case-sensitive matching.", False),
    }, ["pattern"]), _grep))
    registry.register(ToolDef("exec_shell", "Run a shell command, optionally in the background.", _schema({
        "command": _string("Shell command."),
        "timeout": _integer("Timeout in seconds.", 30),
        "background": _bool("Start in background and return task_id.", False),
    }, ["command"]), _bash))
    registry.register(ToolDef("exec_shell_wait", "Wait for a background shell task.", _schema({
        "task_id": _string("Background shell task id."),
        "timeout": _integer("Wait timeout in seconds.", 1),
    }, ["task_id"]), _task_shell_wait))
    registry.register(ToolDef("exec_wait", "Alias for exec_shell_wait.", _schema({
        "task_id": _string("Background shell task id."),
        "timeout": _integer("Wait timeout in seconds.", 1),
    }, ["task_id"]), _task_shell_wait))
    registry.register(ToolDef("exec_shell_interact", "Send stdin to a background shell task.", _schema({
        "task_id": _string("Background shell task id."),
        "input": _string("Input to send."),
        "close_stdin": _bool("Close stdin after sending.", False),
        "timeout": _integer("Wait timeout in seconds.", 1),
    }, ["task_id"]), _exec_shell_interact))
    registry.register(ToolDef("exec_interact", "Alias for exec_shell_interact.", _schema({
        "task_id": _string("Background shell task id."),
        "input": _string("Input to send."),
        "close_stdin": _bool("Close stdin after sending.", False),
        "timeout": _integer("Wait timeout in seconds.", 1),
    }, ["task_id"]), _exec_shell_interact))
    registry.register(ToolDef("exec_shell_cancel", "Cancel a background shell task.", _schema({
        "task_id": _string("Background shell task id."),
        "all": _bool("Cancel all background shell tasks.", False),
    }), _exec_shell_cancel))
    registry.register(ToolDef("task_shell_start", "Start a long-running command in the background.", _schema({
        "command": _string("Shell command."),
    }, ["command"]), _task_shell_start))
    registry.register(ToolDef("task_shell_wait", "Wait for a task_shell_start command.", _schema({
        "task_id": _string("Background shell task id."),
        "timeout": _integer("Wait timeout in seconds.", 1),
    }, ["task_id"]), _task_shell_wait))
    registry.register(ToolDef("git_status", "Inspect git status.", _schema({}), _git_status))
    registry.register(ToolDef("git_diff", "Inspect git diff.", _schema({
        "staged": _bool("Show staged diff.", False),
        "path": _string("Optional path."),
    }), _git_diff))
    registry.register(ToolDef("git_log", "Inspect recent git commits.", _schema({
        "limit": _integer("Commit count.", 20),
    }), _git_log))
    registry.register(ToolDef("git_show", "Show a git revision.", _schema({
        "revision": _string("Revision, default HEAD."),
    }), _git_show))
    registry.register(ToolDef("git_blame", "Show git blame for a line range.", _schema({
        "path": _string("File path."),
        "start": _integer("Start line.", 1),
        "end": _integer("End line.", 80),
    }, ["path"]), _git_blame))
    registry.register(ToolDef("EnterWorktree", "Enter a git worktree or report an honest unsupported state.", _schema({
        "name": _string("Worktree name used for default path."),
        "path": _string("Existing or new worktree path inside the workspace sandbox."),
        "branch": _string("Optional branch or ref for git worktree add."),
        "create": _bool("Create the worktree when path does not exist.", True),
        "timeout": _integer("Timeout in seconds.", 120),
    }), _enter_worktree))
    registry.register(ToolDef("ExitWorktree", "Exit the active worktree and restore the previous sandbox root.", _schema({}), _exit_worktree))
    registry.register(ToolDef("github_issue_context", "Read GitHub issue context through gh.", _schema({
        "issue": _string("Issue number or URL."),
        "number": _string("Issue number alias."),
        "url": _string("Issue URL alias."),
    }), _github_issue_context))
    registry.register(ToolDef("github_pr_context", "Read GitHub PR context through gh.", _schema({
        "pr": _string("PR number or URL."),
        "number": _string("PR number alias."),
        "url": _string("PR URL alias."),
        "include_diff": _bool("Include patch diff.", False),
    }), _github_pr_context))
    registry.register(ToolDef("github_comment", "Post a GitHub issue/PR comment through gh with permission approval.", _schema({
        "target": _string("Issue/PR number or URL."),
        "body": _string("Comment body."),
    }, ["target", "body"]), _github_comment))
    registry.register(ToolDef("github_close_issue", "Close a GitHub issue through gh with permission approval.", _schema({
        "issue": _string("Issue number or URL."),
        "reason": _string("Optional close comment."),
    }, ["issue"]), _github_close_issue))
    registry.register(ToolDef("github_close_pr", "Close a GitHub PR through gh with permission approval.", _schema({
        "pr": _string("PR number or URL."),
        "reason": _string("Optional close comment."),
    }, ["pr"]), _github_close_pr))
    registry.register(ToolDef("diagnostics", "Report workspace, git, Python, model, and permission diagnostics.", _schema({}), _diagnostics))
    registry.register(ToolDef("run_tests", "Run the local test command with permission approval.", _schema({
        "command": _string("Test command. Default detects pytest/unittest."),
        "args": _string("Extra arguments."),
        "timeout": _integer("Timeout in seconds.", 120),
    }), _run_tests))
    registry.register(ToolDef("validate_data", "Validate JSON/CSV/TSV data.", _schema({
        "data": _string("Data text."),
        "path": _string("Workspace file path."),
        "format": _string("json, csv, or tsv."),
    }), _validate_data))
    registry.register(ToolDef("project_map", "Summarize project directories and key source files.", _schema({
        "max_files": _integer("Maximum rows.", 200),
    }), _project_map))
    registry.register(ToolDef("retrieve_tool_result", "Read a stored path/handle for a prior large result.", _schema({
        "path": _string("Path or handle."),
        "handle": _string("Path or handle alias."),
        "limit": _integer("Maximum characters.", 12000),
        "lines": _string("Line range such as 10-40."),
        "head": _integer("Return first N lines."),
        "tail": _integer("Return last N lines."),
        "query": _string("Return matches for this text with context."),
        "context": _integer("Context lines for query mode.", 2),
    }), _retrieve_tool_result))
    registry.register(ToolDef("handle_read", "Read a bounded projection from a path-like handle.", _schema({
        "handle": _string("Handle or file path."),
        "path": _string("Handle or file path alias."),
        "limit": _integer("Maximum characters.", 12000),
        "lines": _string("Line range such as 10-40."),
        "head": _integer("Return first N lines."),
        "tail": _integer("Return last N lines."),
        "query": _string("Return matches for this text with context."),
        "context": _integer("Context lines for query mode.", 2),
    }), _handle_read))
    registry.register(ToolDef("web_run", "Compatibility web runner: search when given query, fetch when given url.", _schema({
        "query": _string("Search query."),
        "q": _string("Search query alias."),
        "url": _string("URL to fetch."),
        "max_results": _integer("Maximum search results.", 5),
        "max_chars": _integer("Maximum fetched chars.", 12000),
    }), _web_run))
    registry.register(ToolDef("load_skill", "Load a skill body and companion-file list by name.", _schema({
        "name": _string("Skill name."),
    }, ["name"]), _load_skill))
    registry.register(ToolDef("note", "Append a project note to memory.", _schema({
        "content": _string("Note content."),
    }, ["content"]), _note))
    registry.register(ToolDef("remember", "Persist a durable memory.", _schema({
        "name": _string("Memory name."),
        "text": _string("Memory text."),
        "scope": _string("project or user."),
    }, ["text"]), _remember))
    registry.register(ToolDef("update_plan", "Write high-level plan state.", _schema({
        "explanation": _string("Plan explanation."),
        "plan": _schema_array("Plan items with step/status."),
    }, ["plan"]), _update_plan))
    registry.register(ToolDef("EnterPlanMode", "Enter planning mode and persist plan lifecycle state.", _schema({
        "reason": _string("Why planning mode is being entered."),
        "plan": _string("Optional initial plan text."),
    }), _enter_plan_mode))
    registry.register(ToolDef("ExitPlanMode", "Exit planning mode and persist approval state.", _schema({
        "plan": _string("Plan text to present for approval."),
        "summary": _string("Short summary of the plan."),
        "approved": _bool("Whether the plan has been approved."),
        "approval_state": _string("pending_approval, approved, or rejected."),
    }), _exit_plan_mode))
    registry.register(ToolDef("checklist_write", "Replace the active checklist.", _schema({
        "items": _schema_array("Checklist items."),
    }, ["items"]), _checklist_write))
    registry.register(ToolDef("checklist_add", "Add one checklist item.", _schema({
        "content": _string("Item content."),
        "status": _string("pending, in_progress, completed."),
    }, ["content"]), _checklist_add))
    registry.register(ToolDef("checklist_update", "Update one checklist item.", _schema({
        "id": _string("Item id or content."),
        "content": _string("New content."),
        "status": _string("New status."),
    }, ["id"]), _checklist_update))
    registry.register(ToolDef("checklist_list", "List checklist items.", _schema({}), _checklist_list))
    registry.register(ToolDef("todo_write", "Compatibility alias for checklist_write.", _schema({
        "items": _schema_array("Todo items."),
    }, ["items"]), _checklist_write))
    registry.register(ToolDef("todo_add", "Compatibility alias for checklist_add.", _schema({
        "content": _string("Item content."),
        "status": _string("pending, in_progress, completed."),
    }, ["content"]), _checklist_add))
    registry.register(ToolDef("todo_update", "Compatibility alias for checklist_update.", _schema({
        "id": _string("Item id or content."),
        "content": _string("New content."),
        "status": _string("New status."),
    }, ["id"]), _checklist_update))
    registry.register(ToolDef("todo_list", "Compatibility alias for checklist_list.", _schema({}), _checklist_list))
    registry.register(ToolDef("create_goal", "Create the active goal.", _schema({
        "objective": _string("Concrete objective."),
        "token_budget": _integer("Optional token budget."),
    }, ["objective"]), _create_goal))
    registry.register(ToolDef("get_goal", "Read the active goal.", _schema({}), _get_goal))
    registry.register(ToolDef("update_goal", "Update active goal status.", _schema({
        "status": _string("active, complete, or blocked."),
    }, ["status"]), _update_goal))
    registry.register(ToolDef("agent_open", "Open a named CodeWhale-style subagent session.", _schema({
        "name": _string("Session name."),
        "prompt": _string("Initial task."),
        "type": _string("general/explore/researcher/plan/writer/critic/review/implementer/verifier/tool_agent/custom."),
        "agent_type": _string("Alias for type."),
        "allowed_tools": {"type": "array", "items": {"type": "string"}},
        "allowed_tools_list": {"type": "array", "items": {"type": "string"}},
        "tools": {"type": "array", "items": {"type": "string"}},
        "background": _bool("Run in background.", True),
        "fork_context": _bool("Fork parent context marker.", False),
    }, ["prompt"]), _agent_open))
    registry.register(ToolDef("Agent", "Launch a Claude-style subagent.", _schema({
        "description": _string("Short task description."),
        "prompt": _string("Task for the agent to perform."),
        "subagent_type": _string("Specialized agent type."),
        "allowed_tools": {"type": "array", "items": {"type": "string"}},
        "allowed_tools_list": {"type": "array", "items": {"type": "string"}},
        "model": _string("Optional model hint."),
        "run_in_background": _bool("Run in background.", True),
        "name": _string("Optional addressable agent name."),
    }, ["prompt"]), _agent_open))
    registry.register(ToolDef("Task", "Legacy Claude-style alias for Agent.", _schema({
        "description": _string("Short task description."),
        "prompt": _string("Task for the agent to perform."),
        "subagent_type": _string("Specialized agent type."),
        "allowed_tools": {"type": "array", "items": {"type": "string"}},
        "allowed_tools_list": {"type": "array", "items": {"type": "string"}},
        "model": _string("Optional model hint."),
        "run_in_background": _bool("Run in background.", True),
        "name": _string("Optional addressable agent name."),
    }, ["prompt"]), _agent_open))
    registry.register(ToolDef("tool_agent", "Open a fast tool-bound subagent.", _schema({
        "name": _string("Session name."),
        "prompt": _string("Initial task."),
        "allowed_tools": {"type": "array", "items": {"type": "string"}},
        "allowed_tools_list": {"type": "array", "items": {"type": "string"}},
        "tools": {"type": "array", "items": {"type": "string"}},
        "background": _bool("Run in background.", True),
    }, ["prompt"]), _tool_agent))
    registry.register(ToolDef("agent_eval", "Fetch, wait on, or message a subagent session.", _schema({
        "name": _string("Session name."),
        "agent_id": _string("Agent id."),
        "message": _string("Optional follow-up."),
        "block": _bool("Wait for terminal status.", True),
        "timeout_ms": _integer("Timeout milliseconds.", 30000),
    }), _agent_eval))
    registry.register(ToolDef("agent_close", "Cancel or close a subagent session.", _schema({
        "name": _string("Session name."),
        "agent_id": _string("Agent id."),
    }), _agent_close))
    registry.register(ToolDef("task_create", "Create a durable task record.", _schema({
        "prompt": _string("Task prompt."),
        "title": _string("Task title alias."),
    }), _task_create))
    registry.register(ToolDef("task_list", "List durable task records.", _schema({}), _task_list))
    registry.register(ToolDef("task_read", "Read a durable task record.", _schema({
        "task_id": _string("Task id."),
    }, ["task_id"]), _task_read))
    registry.register(ToolDef("task_cancel", "Cancel a durable task record.", _schema({
        "task_id": _string("Task id."),
    }, ["task_id"]), _task_cancel))
    registry.register(ToolDef("task_gate_run", "Run a verification command and attach gate evidence.", _schema({
        "command": _string("Verification command."),
        "task_id": _string("Optional task id."),
        "timeout": _integer("Timeout in seconds.", 120),
    }, ["command"]), _task_gate_run))
    registry.register(ToolDef("pr_attempt_record", "Record current git diff as a PR attempt.", _schema({
        "task_id": _string("Optional task id."),
        "message": _string("Attempt note."),
    }), _pr_attempt_record))
    registry.register(ToolDef("pr_attempt_list", "List recorded PR attempts.", _schema({}), _pr_attempt_list))
    registry.register(ToolDef("pr_attempt_read", "Read a recorded PR attempt.", _schema({
        "attempt_id": _string("Attempt id."),
    }, ["attempt_id"]), _pr_attempt_read))
    registry.register(ToolDef("pr_attempt_preflight", "Run git apply --check for a recorded attempt.", _schema({
        "attempt_id": _string("Attempt id."),
    }, ["attempt_id"]), _pr_attempt_preflight))
    registry.register(ToolDef("automation_create", "Create a durable automation record.", _schema({
        "name": _string("Automation name."),
        "prompt": _string("Prompt to run."),
        "rrule": _string("Schedule/RRULE text."),
    }, ["prompt"]), _automation_create))
    registry.register(ToolDef("automation_list", "List automation records.", _schema({}), _automation_list))
    registry.register(ToolDef("automation_read", "Read an automation record.", _schema({
        "automation_id": _string("Automation id."),
    }, ["automation_id"]), _automation_read))
    registry.register(ToolDef("automation_update", "Update an automation record.", _schema({
        "automation_id": _string("Automation id."),
        "name": _string("New name."),
        "prompt": _string("New prompt."),
        "rrule": _string("New schedule."),
        "status": _string("active or paused."),
    }, ["automation_id"]), _automation_update))
    registry.register(ToolDef("automation_pause", "Pause an automation.", _schema({
        "automation_id": _string("Automation id."),
    }, ["automation_id"]), lambda args, ctx: _automation_lifecycle(args, ctx, "paused")))
    registry.register(ToolDef("automation_resume", "Resume an automation.", _schema({
        "automation_id": _string("Automation id."),
    }, ["automation_id"]), lambda args, ctx: _automation_lifecycle(args, ctx, "active")))
    registry.register(ToolDef("automation_delete", "Delete an automation.", _schema({
        "automation_id": _string("Automation id."),
    }, ["automation_id"]), _automation_delete))
    registry.register(ToolDef("automation_run", "Run an automation now by creating a task record.", _schema({
        "automation_id": _string("Automation id."),
    }, ["automation_id"]), _automation_run))
    registry.register(ToolDef("rlm_session_objects", "List RLM sessions and symbolic objects.", _schema({}), _rlm_session_objects))
    registry.register(ToolDef("rlm_open", "Open a lightweight Python analysis session.", _schema({
        "name": _string("Session name."),
        "content": _string("Inline content."),
        "path": _string("Workspace file to load."),
        "url": _string("Public URL to fetch."),
    }), _rlm_open))
    registry.register(ToolDef("rlm_eval", "Execute Python in an RLM session.", _schema({
        "name": _string("Session name."),
        "code": _string("Python code."),
    }, ["name", "code"]), _rlm_eval))
    registry.register(ToolDef("rlm_configure", "Update RLM session config.", _schema({
        "name": _string("Session name."),
    }, ["name"]), _rlm_configure))
    registry.register(ToolDef("rlm_close", "Close an RLM session.", _schema({
        "name": _string("Session name."),
    }, ["name"]), _rlm_close))
    registry.register(ToolDef("slop_ledger_append", "Append architectural residue to the slop ledger.", _schema({
        "bucket": _string("Residue bucket."),
        "severity": _string("critical/high/medium/low/info."),
        "confidence": _string("certain/high/medium/low."),
        "title": _string("Short title."),
        "description": _string("Detailed description."),
    }, ["title"]), _slop_ledger_append))
    registry.register(ToolDef("slop_ledger_query", "Query the slop ledger.", _schema({
        "query": _string("Search text."),
        "bucket": _string("Bucket filter."),
        "status": _string("Status filter."),
    }), _slop_ledger_query))
    registry.register(ToolDef("slop_ledger_update", "Update one slop ledger entry.", _schema({
        "id": _string("Entry id."),
        "status": _string("New status."),
        "cleanup_recommendation": _string("Cleanup recommendation."),
    }, ["id"]), _slop_ledger_update))
    registry.register(ToolDef("slop_ledger_export", "Export the slop ledger as Markdown.", _schema({
        "path": _string("Optional output path."),
    }), _slop_ledger_export))
    registry.register(ToolDef("list_mcp_resources", "List MCP resources known to LilBot.", _schema({}), _mcp_list_resources))
    registry.register(ToolDef("list_mcp_resource_templates", "List MCP resource templates known to LilBot.", _schema({}), _mcp_list_resources))
    registry.register(ToolDef("read_mcp_resource", "Read a supported MCP resource.", _schema({
        "uri": _string("Resource URI."),
    }, ["uri"]), _mcp_read_resource))
    registry.register(ToolDef("mcp_read_resource", "Alias for read_mcp_resource.", _schema({
        "uri": _string("Resource URI."),
    }, ["uri"]), _mcp_read_resource))
    registry.register(ToolDef("pandoc_convert", "Convert documents through pandoc when installed.", _schema({
        "input": _string("Input path."),
        "output": _string("Output path."),
    }, ["input", "output"]), _pandoc_convert))
    registry.register(ToolDef("image_ocr", "Run OCR with tesseract when installed.", _schema({
        "image_path": _string("Image path."),
    }, ["image_path"]), _image_ocr))
    registry.register(ToolDef("image_analyze", "Report image metadata; vision API is not configured in phase 1.", _schema({
        "image_path": _string("Image path."),
        "prompt": _string("Analysis prompt."),
    }, ["image_path"]), _image_analyze))
    registry.register(ToolDef("notify", "Emit a lightweight notification message.", _schema({
        "message": _string("Message."),
    }), _notify))
    registry.register(ToolDef("request_user_input", "Request interactive user input; currently reports unavailable inside tool calls.", _schema({
        "questions": _schema_array("Questions."),
    }), _request_user_input))
    registry.register(ToolDef("review", "Run a lightweight review scan.", _schema({
        "target": _string("Target path."),
    }), _review))
    registry.register(ToolDef("fim_edit", "FIM edit placeholder with explicit unavailable result.", _schema({
        "path": _string("File path."),
        "prompt": _string("Edit prompt."),
    }), _fim_edit))
    registry.register(ToolDef("finance", "Fetch simple quote CSV data from stooq.", _schema({
        "symbol": _string("Symbol/ticker."),
        "ticker": _string("Symbol alias."),
    }), _finance))
    registry.register(ToolDef("code_execution", "Execute Python code with permission approval.", _schema({
        "code": _string("Python source code."),
    }, ["code"]), _code_execution))
    registry.register(ToolDef("js_execution", "Execute JavaScript through node when installed.", _schema({
        "code": _string("JavaScript source code."),
    }, ["code"]), _js_execution))
    registry.register(ToolDef("revert_turn", "Report snapshot-based revert support status.", _schema({}), _revert_turn))
    registry.register(ToolDef("recall_archive", "Search local LilBot archive notes.", _schema({
        "query": _string("Search query."),
    }), _recall_archive))
    registry.register(ToolDef("tool_search_tool_regex", "Search registered tool names/descriptions with a regex.", _schema({
        "query": _string("Regex query."),
        "max_results": _integer("Maximum results.", 20),
    }, ["query"]), lambda args, ctx: _tool_search(args, registry, regex=True)))
    registry.register(ToolDef("tool_search_tool_bm25", "Search registered tool names/descriptions with simple term scoring.", _schema({
        "query": _string("Natural-language query."),
        "max_results": _integer("Maximum results.", 20),
    }, ["query"]), lambda args, ctx: _tool_search(args, registry, regex=False)))
    registry.register(ToolDef("multi_tool_use_parallel", "Execute multiple LilBot tool calls and return structured results.", _schema({
        "tool_uses": _schema_array("Tool call objects with recipient_name and parameters."),
    }, ["tool_uses"]), lambda args, ctx: _parallel_tool(args, registry, ctx)))
