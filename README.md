# LilBot Agent

LilBot is a clean-room local coding-agent playground. It is written in Python so we can move fast on the agent kernel, tools, permissions, memory, skills, subagents, MCP-style adapters, and a polished terminal UI.

The code is original. It copies no proprietary implementation.

## Windows Quick Start

Python 3.10 is OK. The project is tested with Python 3.10.20 on Windows.

```powershell
cd F:\Experiment_laborotory\collection-claude-code-source-code-main\LilBot-agent-code
conda activate LilBot
pip install -r requirements.txt
python -m lilbot
```

If pip says `uvicorn ... requires click`, reinstall requirements after this update:

```powershell
pip install -r requirements.txt
pip check
```

If box lines or Chinese text look like `鈺...`, your PowerShell tab is not using UTF-8. LilBot now tries to enable UTF-8 automatically, but this manual setup is still useful:

```powershell
chcp 65001
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
python -m lilbot
```

Recommended terminal: Windows Terminal + Cascadia Mono or JetBrains Mono.

## DeepSeek

Do not commit API keys. Set the key only in your shell or in Windows user environment variables.

Temporary current-shell use:

```powershell
$env:DEEPSEEK_API_KEY="sk-..."
python -m lilbot --provider deepseek --model deepseek-v4-flash
```

One-shot real API smoke test:

```powershell
$env:DEEPSEEK_API_KEY="sk-..."
python -m lilbot --provider deepseek --model deepseek-v4-flash --print "Reply exactly: LilBot OK"
```

LilBot uses DeepSeek's OpenAI-compatible endpoint:

```text
https://api.deepseek.com
```

## GitHub Login On Windows

This repo already has the remote configured:

```powershell
git remote -v
```

To push from your own Windows terminal, log in using one of these methods.

### Option A: Git Credential Manager

```powershell
git push -u origin main
```

Git will open a GitHub browser login or ask for credentials. For password prompts, GitHub requires a Personal Access Token instead of your account password.

### Option B: GitHub CLI

Install GitHub CLI, then:

```powershell
gh auth login
git push -u origin main
```

Use:

```powershell
gh auth status
```

to confirm login.

## CLI Commands

- `/help` show commands
- `/theme` show theme preview
- `/tools` list tools
- `/skills` list skills
- `/skill review <target>` run a skill template
- `/memory list|search|save|delete` manage memory
- `/agents` list subagent types and tasks
- `/mcp` list MCP-style server config
- `/permissions ask|accept-all|deny-all` switch permission mode
- `/exit` quit

## Architecture

```text
User / TUI
  -> AgentLoop
      -> Provider(OpenAI-compatible, DeepSeek, or local rule model)
      -> ToolRegistry
          -> Sandbox + PermissionManager
          -> File/Bash/Search tools
          -> Memory tools
          -> Skill tools
          -> Subagent tools
          -> MCP adapter tools
      -> Session transcript + compaction
```

Python can absolutely build a CLI/TUI as polished as TypeScript tools. The terminal only receives ANSI escape sequences, text, mouse events, and keyboard events. Python libraries such as Rich, Textual, prompt_toolkit, and curses/blessed can drive those just as well as Node libraries. Rich gives us beautiful rendering now; Textual can give us full-screen reactive panels later.
