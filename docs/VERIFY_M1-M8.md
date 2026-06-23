# How to Experience & Verify M1–M8

A practical guide: for each improvement, **what changed**, **how to feel it in
normal use**, and **a quick command to verify it yourself** (works even without
a live LLM session). Run everything from the repo root.

> Reminder: this machine encrypts `.md` files at rest — read these docs on
> GitHub or with `git show HEAD:docs/VERIFY_M1-M8.md`, not by double-clicking.

Quick全量自检:
```bash
python -m pytest tests/ -q          # expect: 257 passed
```

---

## M1 — Secret redaction

**What changed.** API keys / tokens / private keys never appear in the trace,
TUI, or logs anymore (they're masked at display time; the model still works with
real values internally).

**Feel it.** Ask LilBot to read or write `.env`. Where it used to print
`sk-44d1…397`, you now see `DEEPSEEK_API_KEY=[REDACTED]…<last4>`.

**Verify.**
```bash
# the fake key is assembled at runtime so this doc contains no key-shaped literal
python -c "from lilbot.security import redact_secrets as r; \
k='sk-'+'0123456789abcdef'*2; \
print(r('DEEPSEEK_API_KEY='+k)); \
print(r('LILBOT_BASE_URL=https://api.deepseek.com')); \
print(r('MAX_TOKENS=128000'))"
# -> DEEPSEEK_API_KEY=[REDACTED]…cdef
# -> LILBOT_BASE_URL=https://api.deepseek.com   (untouched)
# -> MAX_TOKENS=128000                          (numeric config untouched)
```

---

## M2 — Auto diagnostics after edits

**What changed.** After it edits a code file, LilBot auto-runs diagnostics and
feeds errors back to itself on the next step — it fixes its own syntax/type
mistakes without you pointing them out.

**Feel it.** Ask it to write a Python file; if it produces a syntax error, watch
it get a "Diagnostics for files you just edited…" reminder and correct it.

**Verify.**
```bash
python -m pytest tests/test_auto_diagnostics.py -q   # 6 passed
```
Toggle off with `auto_diagnostics: false` in `.lilbot/config.json`.

---

## M3 — Command-safety engine

**What changed.** Catastrophic commands are hard-blocked; safe read-only
commands run without nagging you for approval.

**Feel it.** Ask it to `git status` / `ls -la` — runs immediately, no prompt.
Ask for something destructive — blocked with a reason.

**Verify.**
```bash
python -c "from lilbot.sandbox.execpolicy import classify as c; \
print(c('rm -rf /')); print(c('rm -rf build/')); \
print(c('git status -s')); print(c('git push'))"
# -> ('deny', 'delete of a root/home/cwd/glob-everything path')
# -> ('ask', '')          rm of a normal subdir is allowed to ask
# -> ('allow', 'known safe read-only command')
# -> ('ask', '')
```
Toggle auto-allow with `auto_allow_safe_commands: false`.

---

## M4 — Cycle memory + recall_archive

**What changed.** When a long session compacts, the summary is archived to
`.lilbot/archives/`, and the model can search it later with `recall_archive`.

**Feel it.** In a long session, after a `/compact`, ask "what did we decide
earlier about X?" — it can recall the archived briefing.

**Verify.**
```bash
python -m pytest tests/test_cycles.py -q     # 5 passed
# After a real compaction you'll see files appear:
ls .lilbot/archives/    # cycle-YYYYmmdd-HHMMSS-xxxx.md
```

---

## M5 — Tool-catalog prefix-cache stability

**What changed.** The tool list sent to the model is byte-stable across turns,
keeping DeepSeek's prefix cache warm (cheaper turns). `/tokens` now shows a
`tool_catalog_fp` fingerprint and `tools_visible` count.

**Feel it.** In a session run `/tokens` — note `cache_read_tokens` rising and
`tool_catalog_fp` staying constant while you work.

**Verify.**
```bash
python -c "from lilbot.tools import ToolRegistry, register_builtins; \
r=ToolRegistry(); register_builtins(r); \
print('fp stable:', r.catalog_fingerprint()==r.catalog_fingerprint()); \
print('schemas identical object:', r.schemas() is r.schemas())"
# -> fp stable: True
# -> schemas identical object: True
```

---

## M7 — Real MCP client (use any MCP server)

**What changed.** LilBot connects to MCP servers at startup, **auto-discovers**
their tools, and registers each as a first-class tool `mcp__<server>__<tool>`
(deferred, so it doesn't bloat each turn). No more manual `mcp_call`.

**Feel it.** Configure a server in `.lilbot/mcp.json`:
```json
{ "servers": {
    "fs": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] }
}}
```
Restart LilBot, run `/tools` — you'll see `mcp__fs__*`. The model can ToolSearch
and call them like built-ins.

**Verify (no external server needed — uses a built-in fake MCP server in tests).**
```bash
python -m pytest tests/test_mcp_client.py -q   # 4 passed (handshake, discover, call, register)
```

---

## M8 — MCP server mode (let others use LilBot)

**What changed.** LilBot can expose its own tools to other MCP clients (editors,
other agents) over stdio. Read-only tools only by default.

**Feel it.** Point any MCP client at:
```
command: python   args: ["-m", "lilbot", "--mcp-server"]
```
It will see LilBot's read-only tools (`read_file`, `git_status`, `grep`, …).
Widen/narrow with `.lilbot/mcp_server.json`:
```json
{ "expose_tools": ["read_file", "grep", "git_status", "project_map"] }
```

**Verify (LilBot's own M7 client drives LilBot's M8 server end-to-end).**
```bash
python -m pytest tests/test_mcp_server.py -q   # 8 passed, incl. the M7<->M8 roundtrip
```

---

## One-shot: see several at once in a real session

```bash
python -m lilbot          # start the TUI (uses DeepSeek from .env)
# then:
/tools                    # M5/M7: see tools_visible; mcp__* if configured
/tokens                   # M5: tool_catalog_fp, cache_read_tokens
往 .env 加一行 FOO=bar     # M1: key shown masked; M3 may gate the write
写一个有语法错误的 python 文件然后修好它   # M2: it self-corrects from diagnostics
/sessions  /history  /rewind             # (batch-2) resume / undo edits
```

## Where the proof lives
- Per-step history: GitHub commit log (`commits/main`) — each milestone is one
  commit with a detailed message.
- Human-readable summary: `CHANGELOG.md` (batches 1–3).
- Deep dive with pseudocode: `docs/TECH_REPORT_M1-M8.md`.
- Tests: 257 passing; per-feature files `tests/test_{secrets,auto_diagnostics,
  execpolicy,cycles,catalog_cache,mcp_client,mcp_server}.py`.
