"""TeamManager: lifecycle + persistence + notification routing for teams.

py. Differences vs a generic design:
- in-process backend only (no tmux/iterm2 pane spawning/killing);
- persistence root is workspace-level ``<state_dir>/teams`` (see models.py);
- worktree cleanup shells out via the provided sandbox-less subprocess fallback.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .mailbox import Mailbox, create_message
from .models import (
    AgentTeam,
    TeammateInfo,
    resolve_team_dir,
    unique_team_name,
)
from .progress import TeammateProgress
from .registry import AgentNameRegistry
from .shared_task import SharedTaskStore

if TYPE_CHECKING:
    from .spawn_inprocess import InProcessTeammateHandle

log = logging.getLogger(__name__)

LEAD_NAME = "lead"


class TeamError(Exception):
    pass


class TeamManager:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self._teams: dict[str, AgentTeam] = {}
        self._task_stores: dict[str, SharedTaskStore] = {}
        self._mailboxes: dict[str, Mailbox] = {}
        self._inprocess_handles: dict[str, "InProcessTeammateHandle"] = {}
        self._teammate_team_map: dict[str, str] = {}  # agent_id -> team_name

    # ── creation / lookup ────────────────────────────────────────

    def create_team(self, name: str, lead_agent_id: str = LEAD_NAME, description: str = "") -> AgentTeam:
        slug = unique_team_name(self.state_dir, name)
        team_dir = resolve_team_dir(self.state_dir, slug)
        team_dir.mkdir(parents=True, exist_ok=True)

        config_path = str(team_dir / "config.json")
        team = AgentTeam(
            name=slug,
            lead_agent_id=lead_agent_id,
            config_path=config_path,
            description=description,
        )
        team.save()

        task_store = SharedTaskStore(team_dir / "tasks.json")
        task_store.init_empty()

        mailbox = Mailbox(team_dir / "mailbox")

        self._teams[slug] = team
        self._task_stores[slug] = task_store
        self._mailboxes[slug] = mailbox

        log.info("Created team '%s' at %s", slug, team_dir)
        return team

    def list_teams(self) -> list[AgentTeam]:
        # include teams persisted on disk that aren't cached yet
        base = resolve_team_dir(self.state_dir, "x").parent
        if base.exists():
            for entry in base.iterdir():
                if entry.is_dir() and entry.name not in self._teams:
                    cfg = entry / "config.json"
                    if cfg.exists():
                        try:
                            self._teams[entry.name] = AgentTeam.load(str(cfg))
                        except Exception:
                            pass
        return list(self._teams.values())

    def get_team(self, name: str) -> AgentTeam | None:
        if name in self._teams:
            return self._teams[name]
        config_path = resolve_team_dir(self.state_dir, name) / "config.json"
        if config_path.exists():
            team = AgentTeam.load(str(config_path))
            self._teams[name] = team
            return team
        return None

    def get_task_store(self, team_name: str) -> SharedTaskStore | None:
        if team_name in self._task_stores:
            return self._task_stores[team_name]
        tasks_path = resolve_team_dir(self.state_dir, team_name) / "tasks.json"
        if tasks_path.exists():
            store = SharedTaskStore(tasks_path)
            self._task_stores[team_name] = store
            return store
        return None

    def get_mailbox(self, team_name: str) -> Mailbox | None:
        if team_name in self._mailboxes:
            return self._mailboxes[team_name]
        mailbox_dir = resolve_team_dir(self.state_dir, team_name) / "mailbox"
        if mailbox_dir.exists():
            mailbox = Mailbox(mailbox_dir)
            self._mailboxes[team_name] = mailbox
            return mailbox
        return None

    # ── membership ───────────────────────────────────────────────

    def register_member(self, team_name: str, member: TeammateInfo) -> None:
        team = self.get_team(team_name)
        if team is None:
            raise TeamError(f"Team '{team_name}' not found")
        team.add_member(member)
        team.save()
        AgentNameRegistry.instance().register(member.name, member.agent_id)
        self._teammate_team_map[member.agent_id] = team_name
        log.info("Registered member '%s' (agent=%s) in team '%s'", member.name, member.agent_id, team_name)

    def set_member_idle(self, team_name: str, member_name: str) -> None:
        team = self.get_team(team_name)
        if team is None:
            return
        team.set_member_active(member_name, False)
        team.save()

    def register_inprocess_handle(self, agent_id: str, handle: "InProcessTeammateHandle") -> None:
        self._inprocess_handles[agent_id] = handle

    def get_team_for_teammate(self, agent_id: str) -> str | None:
        if agent_id in self._teammate_team_map:
            return self._teammate_team_map[agent_id]
        for name, team in self._teams.items():
            for m in team.members:
                if m.agent_id == agent_id:
                    return name
        return None

    def on_teammate_completed(self, agent_id: str) -> None:
        team_name = self.get_team_for_teammate(agent_id)
        if team_name is None:
            return
        team = self.get_team(team_name)
        if team is None:
            return
        member = next((m for m in team.members if m.agent_id == agent_id), None)
        if member:
            self.set_member_idle(team_name, member.name)

    # ── deletion ─────────────────────────────────────────────────

    def delete_team(self, team_name: str) -> None:
        team = self.get_team(team_name)
        if team is None:
            raise TeamError(f"Team '{team_name}' not found")

        for member in list(team.members):
            AgentNameRegistry.instance().unregister(member.name)
            handle = self._inprocess_handles.pop(member.agent_id, None)
            if handle is not None and not handle.done:
                handle.cancel()
            if member.worktree_path:
                self._cleanup_worktree(member.worktree_path)

        mailbox = self.get_mailbox(team_name)
        if mailbox:
            mailbox.cleanup_all()

        self._remove_dir(resolve_team_dir(self.state_dir, team_name))
        self._teams.pop(team_name, None)
        self._task_stores.pop(team_name, None)
        self._mailboxes.pop(team_name, None)
        log.info("Deleted team '%s'", team_name)

    # ── notification回流 ─────────────────────────────────────────

    def drain_lead_mailbox(self) -> list[str]:
        """Consume all messages addressed to the lead, formatted for injection."""
        notes: list[str] = []
        for team_name in list(self._teams.keys()):
            team = self.get_team(team_name)
            if team is None:
                continue
            mailbox = self.get_mailbox(team_name)
            if mailbox is None:
                continue
            msgs = mailbox.consume(team.lead_agent_id)
            if not msgs:
                continue
            parts = [f'<team-notification team="{team_name}">']
            for m in msgs:
                parts.append(f"from={m.from_agent}: {m.content}")
            parts.append("</team-notification>")
            notes.append("\n".join(parts))
        return notes

    def get_all_teammate_progress(self) -> list[TeammateProgress]:
        results: list[TeammateProgress] = []
        for team in self._teams.values():
            for member in team.members:
                if getattr(member, "progress", None) is not None:
                    results.append(member.progress)
        return results

    def notify_lead(self, team_name: str, from_agent: str, content: str, summary: str = "") -> None:
        team = self.get_team(team_name)
        mailbox = self.get_mailbox(team_name)
        if team is None or mailbox is None:
            return
        mailbox.write(team.lead_agent_id, create_message(from_agent, team.lead_agent_id, content, summary or content[:40]))

    # ── helpers ──────────────────────────────────────────────────

    def _cleanup_worktree(self, worktree_path: str) -> None:
        import subprocess
        # Run inside the workspace repo (state_dir is <workspace>/.lilbot), not the
        # caller's cwd, so `git worktree remove` targets the right repository.
        workspace = self.state_dir.parent
        try:
            subprocess.run(
                ["git", "worktree", "remove", worktree_path, "--force"],
                capture_output=True, timeout=15, cwd=str(workspace),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("git worktree remove failed for %s: %s", worktree_path, e)
            import shutil
            try:
                if Path(worktree_path).exists():
                    shutil.rmtree(worktree_path, ignore_errors=True)
            except Exception:
                pass

    def _remove_dir(self, path: Path) -> None:
        import shutil
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to remove directory %s: %s", path, e)
