from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Any


@dataclass
class Skill:
    name: str
    description: str
    body: str
    source: Path
    mode: str = "inline"
    aliases: list[str] | None = None
    when_to_use: str = ""
    argument_hint: str = ""
    argument_names: list[str] | None = None
    allowed_tools: list[str] | None = None
    model: str | None = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    agent: str | None = None
    effort: str | None = None
    paths: list[str] | None = None
    shell: str | None = None
    companion_files: list[Path] | None = None

    @property
    def skill_dir(self) -> Path:
        return self.source.parent if self.source.name == "SKILL.md" else self.source.parent

    def render(self, args: str | dict[str, Any] = "") -> str:
        args_text = args.get("args", "") if isinstance(args, dict) else str(args)
        args_text = args_text.strip()
        result = self.body
        replacements = {
            "{{args}}": args_text,
            "$ARGUMENTS": args_text,
            "${LILBOT_SKILL_DIR}": str(self.skill_dir),
            "${CLAUDE_SKILL_DIR}": str(self.skill_dir),
        }
        for key, value in replacements.items():
            result = result.replace(key, value)

        names = self.argument_names or []
        if names:
            try:
                values = shlex.split(args_text)
            except ValueError:
                values = args_text.split()
            for index, name in enumerate(names):
                value = values[index] if index < len(values) else ""
                result = result.replace("{{" + name + "}}", value)
                result = result.replace("${" + name + "}", value)
        return result


def _normalize_key(key: str) -> str:
    return key.strip().lower().replace("_", "-")


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    return value


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) != 3:
        return {}, raw
    _, head, body = parts
    meta: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in head.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace() and stripped.startswith("-") and current_key:
            value = _parse_scalar(stripped[1:].strip())
            existing = meta.setdefault(current_key, [])
            if isinstance(existing, list):
                existing.append(value)
            continue
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        current_key = _normalize_key(key)
        meta[current_key] = [] if not value.strip() else _parse_scalar(value)
    return meta, body


def _meta_get(meta: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        normalized = _normalize_key(key)
        if normalized in meta:
            return meta[normalized]
    return default


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            if isinstance(item, str) and "," in item:
                items.extend(_as_list(item))
            elif item is not None:
                items.append(str(item).strip())
        return [item for item in items if item]
    if isinstance(value, str):
        return [item.strip().strip('"').strip("'") for item in value.split(",") if item.strip()]
    return [str(value).strip()]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_skill(path: Path) -> Skill:
    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(raw)
    mode = str(_meta_get(meta, "context", "mode", default="inline") or "inline")
    return Skill(
        name=meta.get("name", path.stem),
        description=meta.get("description", ""),
        body=body.strip(),
        source=path,
        mode=mode,
        aliases=_as_list(_meta_get(meta, "aliases")),
        when_to_use=str(_meta_get(meta, "when_to_use", "when-to-use")),
        argument_hint=str(_meta_get(meta, "argument_hint", "argument-hint")),
        argument_names=_as_list(_meta_get(meta, "arguments", "arg-names")),
        allowed_tools=_as_list(_meta_get(meta, "allowed_tools", "allowed-tools")),
        model=str(_meta_get(meta, "model")) or None,
        disable_model_invocation=_as_bool(_meta_get(meta, "disable_model_invocation", "disable-model-invocation")),
        user_invocable=_as_bool(_meta_get(meta, "user_invocable", "user-invocable"), True),
        agent=str(_meta_get(meta, "agent")) or None,
        effort=str(_meta_get(meta, "effort")) or None,
        paths=_as_list(_meta_get(meta, "paths")),
        shell=str(_meta_get(meta, "shell")) or None,
        companion_files=_companion_files(path),
    )


def _companion_files(path: Path) -> list[Path]:
    if path.name != "SKILL.md":
        return []
    return sorted(
        item
        for item in path.parent.rglob("*")
        if item.is_file() and item.name != "SKILL.md"
    )


class SkillRegistry:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.project_dir = state_dir / "skills"
        self.bundled_dir = Path(__file__).parent / "bundled"
        self._skills: dict[str, Skill] = {}
        self._aliases: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self._skills.clear()
        self._aliases.clear()
        for directory in [self.bundled_dir, self.project_dir]:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                skill = _parse_skill(path)
                self._add(skill)
            for path in sorted(directory.glob("*/SKILL.md")):
                skill = _parse_skill(path)
                self._add(skill)

    def _add(self, skill: Skill) -> None:
        self._skills[skill.name] = skill
        for alias in skill.aliases or []:
            self._aliases[alias] = skill.name

    def list(self, include_hidden: bool = False) -> list[Skill]:
        skills = self._skills.values() if include_hidden else (s for s in self._skills.values() if s.user_invocable)
        return sorted(skills, key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name) or self._skills.get(self._aliases.get(name, ""))

    def render(self, name: str, args: str | dict[str, Any] = "") -> str:
        skill = self.get(name)
        if not skill:
            known = ", ".join(s.name for s in self.list()) or "none"
            raise KeyError(f"unknown skill '{name}'. Known skills: {known}")
        if skill.disable_model_invocation:
            raise ValueError(f"skill '{skill.name}' cannot be invoked by the model")
        return skill.render(args)

    def ensure_project_dir(self) -> Path:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        return self.project_dir
