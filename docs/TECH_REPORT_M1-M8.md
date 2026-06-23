# LilBot Technical Report — Milestones M1–M8 (Surpassing CodeWhale & mewcode)

> Scope: the engine/security/protocol work that closed LilBot's gaps versus the
> two reference agents — **mewcode** (Python) and **CodeWhale** (Rust). Each
> milestone below lists the problem, the design, partial pseudocode, the files,
> and the gap it closed. Tests grew 184 → 257, zero regressions.

---

## 0. Where these sit in the architecture

```
                      ┌──────────────────────────── Agent.run_turn ───────────────────────────┐
 user text ──▶ recall(M-mem) ─▶ hooks.turn_start ─▶  while steps < max:                        │
                                                       provider.complete(_provider_messages())  │
                                                          tools = registry.schemas()  ◀── M5 cache
                                                       partition(tool_calls)                    │
                                                          read-only batch ─▶ ThreadPool         │
                                                          each call:                            │
                                                             pre_tool hook (block?)             │
                                                             snapshot (rewind)                  │
                                                             command-safety  ◀── M3             │
                                                             registry.execute ─▶ offload        │
                                                             record edited path                 │
                                                       run_post_edit_diagnostics()  ◀── M2      │
                                                    ...                                          │
                                       compact() ─▶ cycles.archive()  ◀── M4                     │
 display ◀── redact_secrets()  ◀── M1 ── (TUI presentation layer)                                │
                                                                                                │
 MCP:  build_runtime ─▶ mcp.connect_and_register(registry)  ◀── M7 (client)                     │
       `--mcp-server` ─▶ LilBotMCPServer(registry).serve()  ◀── M8 (server)                      │
```

---

## M1 — Secret redaction

**Problem.** A tool result printed a live DeepSeek API key into the visible
trace, and a `.env` diff exposed it again. Nothing in LilBot masked secrets.

**Design.** A presentation-layer redactor. The model's own context keeps raw
values (functionality unaffected); only what reaches the **screen / trace /
logs** is masked. Applied in both TUIs (`classic.py`, `dashboard.py`).

**Files.** `lilbot/security/secrets.py`, wired in `lilbot/tui/{classic,dashboard}.py`.

**Pseudocode.**
```python
TOKEN_PATTERNS = [sk-…, ghp_…, AKIA…, AIza…, xox[baprs]-…, JWT, Bearer …]
ASSIGNMENT = /(KEY|TOKEN|SECRET|PASSWORD|…)\s*[=:]\s*(value{6,})/

def redact_secrets(text):
    text = PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", text)
    text = ASSIGNMENT.sub(lambda m: m.key + op + mask(m.value) if not value.isdigit() else m, text)
    for p in TOKEN_PATTERNS: text = p.sub(lambda m: mask(m.group()), text)
    return text

def mask(s):  # keep a 4-char tail so the user can recognise *which* key
    return "[REDACTED]" if len(s) <= 8 else "[REDACTED]…" + s[-4:]
```
False-positive guards: numeric config (`MAX_TOKENS=128000`) and `AUTHOR=` are
left untouched; `AUTH` only matches `AUTHORIZATION`/`AUTH_TOKEN`.

**Gap closed.** Neither reference project leaked into LilBot's trace; this is a
defense-in-depth layer prompted by a real incident.

---

## M2 — Auto diagnostics injection after edits

**Problem.** After the model edited code, type/syntax errors were only found if
the model happened to re-read or run something. CodeWhale runs a language server
after every edit and feeds diagnostics back ("self-correction loop").

**Design.** After `write_file`/`edit_file`/`fim_edit` on a *code* file, run the
existing `lsp_diagnostics` tool (LSP when available, Python-syntax fallback) on
the edited files; stash any errors as a **one-shot** system reminder injected
into the *next* LLM call.

**Files.** `lilbot/core/agent.py` (`_run_post_edit_diagnostics`, `_provider_messages`).

**Pseudocode.**
```python
# inside run_turn, after a tool batch:
def _run_post_edit_diagnostics():
    files = [p for p in edited_this_turn if ext(p) in DIAGNOSABLE_EXTS][:5]
    lines = []
    for path in files:
        result = registry.execute("lsp_diagnostics", {"path": path}, ctx)
        for d in result.metadata["diagnostics"]:
            if d.severity in ("error", "warning"):
                lines.append(f"{path}:L{d.line} [{d.severity}] {d.message}")
    if lines:
        self._pending_diagnostics = "Fix these before continuing:\n" + "\n".join(lines)

# in _provider_messages():
if self._pending_diagnostics:
    extras.append({"role":"system","content": self._pending_diagnostics})
    self._pending_diagnostics = ""   # one-shot
```

**Gap closed.** CodeWhale had the LSP-injection loop; LilBot had the diagnostics
*tool* but never auto-ran it. Now the loop is closed (toggle `auto_diagnostics`).

---

## M3 — Command-safety engine (execpolicy)

**Problem.** Every shell command went straight to the approval prompt; there was
no structured deny-list for catastrophic commands and no smart auto-allow.

**Design.** Port of CodeWhale's execpolicy + `bash_arity` idea:
- **DENY** catastrophic commands outright.
- **AUTO-ALLOW** known read-only commands using *arity-aware prefix matching*
  (flags ignored): `git status` allows `git status -s` but not `git push`.
- Everything else → normal `ask`.

**Files.** `lilbot/sandbox/execpolicy.py`, wired in `_shell_permission` (builtin).

**Pseudocode.**
```python
def matches_allow_rule(cmd, allow):           # arity-aware
    if has_shell_operators(cmd): return False  # ; && | > ` $() → never auto-allow
    positional = [t for t in shlex.split(cmd) if not t.startswith("-")]
    return any(positional[:len(rule.split())] == rule.split() for rule in allow)

def classify(cmd):
    if rm_targets_root_home_cwd_glob(cmd) or DANGEROUS.match(cmd): return "deny"
    if matches_allow_rule(cmd, SAFE_READONLY): return "allow"
    return "ask"

# _shell_permission:
decision = classify(command)
if decision == "deny":  return blocked(reason)
if decision == "allow" and config.auto_allow_safe_commands: return allowed   # no prompt
... else normal permission prompt
```
`rm -rf /tmp/foo` is NOT denied (normal subdir delete); `rm -rf /` / `~` / `*` /
`.` are. Compound commands are never auto-allowed.

**Gap closed.** Brought CodeWhale's structured exec policy to LilBot's
(previously PowerShell-centric) safety layer.

---

## M4 — Cycle memory + recall_archive

**Problem.** On compaction the summarized prefix was discarded forever. The
`recall_archive` tool existed but nothing ever wrote archives for it to read.

**Design.** Each compaction archives a dated **briefing** to
`.lilbot/archives/cycle-<ts>.md`. `recall_archive` now finds them (newest-first,
keyword filter).

**Files.** `lilbot/core/cycles.py`; `agent.compact()`; `_recall_archive` (builtin).

**Pseudocode.**
```python
# agent.compact():
result = auto_compact(messages, ...)
if result and len(result.messages) > 1:
    briefing = result.messages[1]["content"]            # the LLM summary
    cycles.archive(briefing, result.summarized, result.before_tokens)
messages = result.messages

# CycleArchive.archive():  write .lilbot/archives/cycle-<ts>-<uuid4>.md
# recall_archive(query):   read *.md, filter by query, sort by mtime desc
```

**Gap closed.** CodeWhale's cycle_manager/recall_archive; LilBot now retains
long-session knowledge across compactions.

---

## M5 — Tool-catalog prefix-cache stability

**Problem.** `registry.schemas()` rebuilt the tool list each turn. For DeepSeek
prefix caching, the `tools` payload bytes should be **stable** across turns.

**Design.** Cache the serialized visible-tool catalog (CodeWhale `OnceLock`
pattern), rebuilt only when the visible set changes. Render-context (dynamic
agent descriptions) mutates a *copy*, never the cache. Expose
`catalog_fingerprint()` (shown in `/tokens`).

**Files.** `lilbot/tools/registry.py`.

**Pseudocode.**
```python
def _base_catalog():
    sig = frozenset(visible tool names)
    if cache is None or cache_sig != sig:           # rebuild only on change
        cache = [schema_of(t) for t in tools if visible(t)]; cache_sig = sig
    return cache                                    # same object => same bytes

def schemas(render_ctx):
    base = _base_catalog()
    if render_ctx is None: return base              # cached, byte-stable
    schemas = [dict(s) for s in base]               # copy before mutation
    render_agent_descriptions(schemas, render_ctx); return schemas
```

**Gap closed.** Matches CodeWhale's byte-stable tool serialization → keeps the
prefix cache warm → cheaper turns.

---

## M7 — Real MCP client (discovery + first-class tools)

**Problem.** LilBot's MCP was a one-shot subprocess `mcp_call(server, tool,
args)` with **no discovery** and no persistent connection — behind *both*
mewcode and CodeWhale.

**Design.** A synchronous JSON-RPC-2.0-over-stdio client (persistent subprocess
+ reader thread, **no async dependency**): `initialize` handshake, `tools/list`
discovery, `tools/call`. Each discovered tool is registered as a **first-class
deferred** tool `mcp__<server>__<tool>` so the model uses it like a built-in.

**Files.** `lilbot/mcp/client.py`, `lilbot/mcp/manager.py`; wired in `build_runtime`.

**Pseudocode.**
```python
class StdioMCPClient:
    def start():
        proc = Popen([cmd, *args], stdin=PIPE, stdout=PIPE, text=True, env=...)
        Thread(_read_loop).start()                  # routes responses by id
        _request("initialize", {protocolVersion, capabilities, clientInfo})
        _notify("notifications/initialized")
    def _request(method, params):
        id = next_id(); pending[id] = Queue()
        write({jsonrpc, id, method, params}); msg = pending[id].get(timeout)
        return msg["result"] or raise msg["error"]
    def list_tools():  return _request("tools/list")["tools"]
    def call_tool(name, args):
        r = _request("tools/call", {name, arguments:args})
        return text_of(r["content"]), bool(r.get("isError"))

# MCPManager.connect_and_register(registry):
for server in configured:
    client = StdioMCPClient(server); client.start(); client.list_tools()
    for tool in client.tools:
        registry.register(ToolDef(f"mcp__{server}__{tool.name}",
                                  tool.description, tool.inputSchema,
                                  handler=lambda a,c: client.call_tool(tool.name, a),
                                  should_defer=True))
```

**Gap closed.** Brings LilBot to parity with mewcode (discovery + registration)
on the client side; tools become deferred so they don't bloat the per-turn
payload (composes with M5/the M-batch1 ToolSearch mechanism).

---

## M8 — MCP server mode

**Problem.** Only CodeWhale could act as an MCP *server* (expose its tools to
other clients). LilBot could not.

**Design.** `python -m lilbot --mcp-server` serves LilBot's tools over stdio
JSON-RPC. **Read-only tools only by default** (safety); widen/narrow via
`.lilbot/mcp_server.json → expose_tools`. With M7 + M8, LilBot is a
**bidirectional** MCP peer.

**Files.** `lilbot/mcp/server.py`; `--mcp-server` flag in `cli.py`.

**Pseudocode.**
```python
class LilBotMCPServer:
    def handle(msg):
        if msg.method == "initialize": return ok({protocolVersion, capabilities, serverInfo})
        if msg.method == "tools/list": return ok({tools: exposed_descriptors()})
        if msg.method == "tools/call":
            name, args = msg.params.name, msg.params.arguments
            if name not in exposed: return err("tool not exposed")
            result = registry.execute(name, args, ctx)
            return ok({content:[{type:"text", text:result.output}], isError: not result.ok})
    def exposed_names():
        return expose_tools or READ_ONLY_TOOLS      # safe default
    def serve(stdin, stdout):
        for line in stdin: write(handle(json.loads(line)))
```

**Verification.** Tested by driving the M8 server with LilBot's *own* M7 client
end-to-end (`test_roundtrip_m7_client_against_m8_server`).

**Gap closed.** LilBot is now the only one of the three with *tested
bidirectional* MCP wired into a single codebase.

---

## Summary table

| Milestone | Closed gap vs | Files | Tests |
|---|---|---|---|
| M1 Secret redaction | (incident-driven) | `security/secrets.py` | 10 |
| M2 Auto diagnostics | CodeWhale | `core/agent.py` | 6 |
| M3 Command-safety | CodeWhale execpolicy | `sandbox/execpolicy.py` | 35 |
| M4 Cycle memory | CodeWhale cycle_manager | `core/cycles.py` | 5 |
| M5 Catalog cache | CodeWhale OnceLock | `tools/registry.py` | 5 |
| M7 MCP client | mewcode + CodeWhale | `mcp/client.py` | 4 |
| M8 MCP server | CodeWhale | `mcp/server.py` | 8 |

> M6 (TUI polish) is intentionally deferred to a collaborative session.
> See `docs/VERIFY_M1-M8.md` for how to experience and verify each change.
