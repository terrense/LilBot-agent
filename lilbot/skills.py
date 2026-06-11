from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    name: str
    description: str
    body: str
    source: Path
    mode: str = "inline"

    def render(self, args: str = "") -> str:
        return self.body.replace("{{args}}", args.strip())


def _parse_skill(path: Path) -> Skill:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            _, head, body = parts
            for line in head.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                meta[key.strip().lower()] = value.strip().strip('"')
    return Skill(
        name=meta.get("name", path.stem),
        description=meta.get("description", ""),
        body=body.strip(),
        source=path,
        mode=meta.get("mode", "inline"),
    )


class SkillRegistry:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.project_dir = state_dir / "skills"
        self.bundled_dir = Path(__file__).parent / "skills" / "bundled"
        self._skills: dict[str, Skill] = {}
        self.reload()

    def reload(self) -> None:
        self._skills.clear()
        for directory in [self.bundled_dir, self.project_dir]:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                skill = _parse_skill(path)
                self._skills[skill.name] = skill

    def list(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def render(self, name: str, args: str = "") -> str:
        skill = self.get(name)
        if not skill:
            known = ", ".join(s.name for s in self.list()) or "none"
            raise KeyError(f"unknown skill '{name}'. Known skills: {known}")
        return skill.render(args)

    def ensure_project_dir(self) -> Path:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        return self.project_dir

