"""LilBot Teams — 30-second live demo.

Shows the whole team mechanism step by step:
  team_create -> spawn teammates -> teammate runs a turn -> reports to lead
  -> lead drains the mailbox (async PUSH) -> wake a teammate -> shared board.

Usage:
  python experiment/teams_demo.py          # deterministic stub provider (no network, great for live talks)
  python experiment/teams_demo.py --real   # real DeepSeek provider (needs the repo .env key + a git workspace)

The stub mode is the one to use on a livestream: every step prints, nothing
depends on the network, and it finishes in ~2 seconds.
"""

from __future__ import annotations

import sys
import tempfile
import time
import subprocess
from pathlib import Path

# Run from the repo root so `import lilbot` works.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lilbot.config import LilBotConfig, load_config
from lilbot import cli
from lilbot.tui.classic import LilBotUI
from lilbot.core.events import ProviderTurn
from lilbot.teams import AgentNameRegistry


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def banner(step: str, text: str) -> None:
    print("\n" + _c("96", f">> {step}") + f" {text}")


def wait_until(predicate, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


class StubProvider:
    """Deterministic provider: a teammate 'completes' its turn with a contract report."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        # Echo a hint of what the teammate was told, so the demo feels alive.
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        snippet = " ".join(str(last_user).split())[:50]
        return ProviderTurn(
            content=(
                f"SUMMARY: handled the task ({snippet}...).\n"
                "CHANGES: None.\nEVIDENCE: stub run.\nRISKS: None.\nBLOCKERS: None."
            )
        )


def build(workspace: Path, real: bool):
    if real:
        load_config(REPO)  # inject repo .env (DeepSeek key) into os.environ
        cfg = load_config(workspace)
    else:
        cfg = LilBotConfig(workspace=workspace)
    AgentNameRegistry.reset()
    agent, registry, ctx = cli.build_runtime(cfg, LilBotUI(enabled=False), interactive=False)
    if not real:
        stub = StubProvider()
        agent.provider = stub
        ctx.subagents.provider = lambda m, t: stub.complete(m, t)
    print(f"   provider={cfg.provider} model={cfg.model}")
    return agent, registry, ctx


def main() -> int:
    try:  # robust against GBK/legacy consoles on Windows
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    real = "--real" in sys.argv
    ws = Path(tempfile.mkdtemp(prefix="lilbot-team-demo-"))

    # A real run wants a git repo so teammates can use worktree isolation.
    if real:
        for c in (["git", "init", "-q"], ["git", "config", "user.email", "d@d"], ["git", "config", "user.name", "demo"]):
            subprocess.run(c, cwd=ws, capture_output=True)
        (ws / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        subprocess.run(["git", "add", "-A"], cwd=ws, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=ws, capture_output=True)

    print(f"\n=== LilBot Teams demo  ({'REAL DeepSeek' if real else 'stub provider'}) ===")
    print(f"    workspace: {ws}")
    agent, registry, ctx = build(ws, real)

    # 1) Create the team
    banner("1. team_create", "create a coordination container")
    r, _ = registry.execute("team_create", {"team_name": "shipit", "description": "demo team"}, ctx)
    print("   ->", "ok" if r.ok else r.output)

    # 2) Spawn two long-running teammates (NOT one-shot subagents — note team_name)
    banner("2. Agent(team_name=...)", "spawn two long-running teammates")
    impl_prompt = (
        "calc.py add() returns a - b but should return a + b. Use read_file then edit_file to fix it, then report."
        if real else "implement the feature"
    )
    registry.execute("agent_open", {
        "team_name": "shipit", "name": "impl", "subagent_type": "implementer",
        "isolation": "worktree" if real else None, "prompt": impl_prompt,
    }, ctx)
    registry.execute("agent_open", {
        "team_name": "shipit", "name": "rev", "subagent_type": "review",
        "prompt": "stand by to review impl's work",
    }, ctx)
    print("   -> spawned: impl (implementer), rev (review)")

    # 3) Watch teammates report back — the PUSH path
    banner("3. async PUSH", "teammates run a turn, then message 'lead' and go idle")
    wait_until(lambda: bool(ctx.teams.get_mailbox("shipit").read("lead")), timeout=60 if real else 8)
    notes = ctx.teams.drain_lead_mailbox()   # this is what core/agent.py does every loop turn
    print(f"   lead drained {len(notes)} notification(s):")
    for n in notes:
        print("   " + " | ".join(n.splitlines()))

    # 4) Wake a teammate by name — proves it's long-lived, not one-shot
    banner("4. send_message", "wake the idle teammate 'rev' by name")
    r, _ = registry.execute("send_message", {
        "to": "rev", "message": "review impl's change for correctness", "summary": "review request",
    }, ctx)
    print("   ->", r.output)
    wait_until(lambda: bool(ctx.teams.get_mailbox("shipit").read("lead")), timeout=60 if real else 8)
    for n in ctx.teams.drain_lead_mailbox():
        print("   woke -> " + " | ".join(n.splitlines()))

    # 5) Shared task board with dependencies
    banner("5. shared board", "tasks with assignee + blocked_by dependency")
    registry.execute("team_task_create", {"title": "implement fix", "assignee": "impl"}, ctx)
    registry.execute("team_task_create", {"title": "review fix", "assignee": "rev", "blocked_by": ["1"]}, ctx)
    registry.execute("team_task_update", {"task_id": "1", "status": "completed"}, ctx)
    r, _ = registry.execute("team_task_list", {}, ctx)
    import json
    for t in json.loads(r.output):
        dep = f" (blocked_by {t['blocked_by']})" if t["blocked_by"] else ""
        print(f"   - #{t['id']} {t['title']} [{t['status']}] @{t['assignee']}{dep}")

    # 6) Roster + live progress, then clean up
    banner("6. roster + progress", "who is on the team and what they did")
    for p in ctx.teams.get_all_teammate_progress():
        print(f"   - {p.name} [{p.status}] tools={p.tool_use_count} tok={p.format_tokens(p.token_count)}")

    if real:
        wt = next((m.worktree_path for m in ctx.teams.get_team("shipit").members if m.name == "impl"), "")
        if wt and (Path(wt) / "calc.py").exists():
            print("\n   impl's worktree calc.py now reads:")
            print("   " + (Path(wt) / "calc.py").read_text().strip().replace("\n", "\n   "))

    ctx.teams.delete_team("shipit")
    print("\n" + _c("92", "[OK] done - team deleted, worktrees cleaned"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
