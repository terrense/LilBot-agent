"""Team / teammate coordination layer (in-process backend).

Ported and adapted from the ``teams`` package. Provides long-running
teammates, file-based mailboxes, a shared task board, and name addressing on top
of LilBot's existing subagent runtime.
"""

from __future__ import annotations

from .mailbox import Mailbox, MailboxMessage, create_message
from .models import AgentTeam, BackendType, TeammateInfo, resolve_team_dir, teams_root, unique_team_name
from .progress import TeammateProgress, random_verb
from .registry import AgentNameRegistry
from .shared_task import SharedTask, SharedTaskStore

__all__ = [
    "AgentNameRegistry",
    "AgentTeam",
    "BackendType",
    "Mailbox",
    "MailboxMessage",
    "SharedTask",
    "SharedTaskStore",
    "TeammateInfo",
    "TeammateProgress",
    "create_message",
    "random_verb",
    "resolve_team_dir",
    "teams_root",
    "unique_team_name",
]
