# Windows guide

LilBot is Windows-first, but it also runs on Linux and macOS. This page covers
the parts that behave differently on Windows.

## Install and activate

```powershell
git clone https://github.com/terrense/LilBot-agent.git
cd LilBot-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

If PowerShell refuses to run the activation script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

`-Scope Process` changes the policy only for the current shell. Do not lower the
machine-wide policy just to run LilBot.

## Terminal and encoding

- Use Windows Terminal. The classic console host works, but the full-screen
  dashboard is designed for a modern VT-capable terminal.
- LilBot sets the console code page to UTF-8 (`65001`) at startup and reconfigures
  `stdout`/`stderr` to UTF-8. If Chinese text still looks wrong, check the
  terminal profile's font and the active code page.
- The dashboard can request a larger font with `--font-size 18` or
  `LILBOT_FONT_SIZE=18`. Windows Terminal owns its own profile font and may
  ignore the request; `/display` reports what the host accepted.
- `F2` / `/copy` writes Unicode clipboard data directly, so Chinese text survives
  the copy. Terminal `Ctrl+Shift+C` also works.

## Shell behavior

Commands run through PowerShell:

```powershell
powershell -NoProfile -NonInteractive -Command "<command>"
```

Before execution, LilBot classifies the command in two stages:

1. `execpolicy` rejects catastrophic commands and auto-allows a small set of
   known read-only commands using arity-aware prefix matching.
2. `powershell_safety` performs a token-level risk pass over separators,
   redirection, subprocess boundaries, background launches, and destructive
   delete/move targets.

Anything not explicitly safe goes through the permission gate. `--permission-mode
deny-all` denies every operation that needs approval; it is not a claim that no
command can ever run.

## Paths and workspace state

- `--workspace PATH` sets the workspace. Paths are resolved and must stay inside
  it; `..` escapes are rejected.
- Runtime state lives in `<workspace>/.lilbot/`: sessions, memory, event log,
  hook config, MCP config, subagent transcripts, and test artifacts.
- Long paths can still hit the classic 260-character limit in third-party tools.
  Keep deep worktrees short, or enable long-path support in Windows.
- Antivirus real-time scanning can make a large repository walk noticeably
  slower. The search tools prune noisy directories, but they cannot outrun a
  filter driver on every file.

## Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `No module named lilbot` | Running outside the repo or the venv is not active | Activate `.venv`, or run from the repository root |
| PowerShell activation is blocked | Execution policy | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| Chinese output is garbled | Terminal code page or font | Use Windows Terminal, check UTF-8, try a CJK font |
| Permission prompt never appears | Non-interactive `--print` run | Use `--permission-mode accept-all` or run interactively |
| Startup is slow | A configured MCP server is unreachable | Check `.lilbot/mcp.json`; a failed server is reported and skipped |
| `dashboard unavailable` | Terminal is not a TTY or prompt_toolkit failed | LilBot falls back to the classic Rich renderer |

## Tests on Windows

```powershell
python -m pytest tests -q
python -m compileall -q lilbot
```

Some tests skip when they need symlink privileges or a POSIX-only feature. The CI
matrix runs the suite on Windows and Ubuntu for Python 3.10-3.13.
