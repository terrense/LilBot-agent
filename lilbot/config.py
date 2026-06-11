from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class LilBotConfig:
    workspace: Path
    provider: str = "auto"
    model: str = "lilbot-rule-model"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    permission_mode: str = "ask"
    max_steps: int = 8
    compact_after_messages: int = 28
    verbose: bool = False

    @property
    def state_dir(self) -> Path:
        return self.workspace / ".lilbot"

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.json"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def default_config(workspace: Path | None = None) -> LilBotConfig:
    root = (workspace or Path.cwd()).resolve()
    provider = _env("LILBOT_PROVIDER", "auto")
    deepseek_key = _env("DEEPSEEK_API_KEY", "")
    api_key = _env("LILBOT_API_KEY", _env("OPENAI_API_KEY", deepseek_key))
    if provider == "auto" and deepseek_key and not _env("LILBOT_API_KEY") and not _env("OPENAI_API_KEY"):
        provider = "deepseek"
    default_model = "deepseek-v4-flash" if provider == "deepseek" else "lilbot-rule-model"
    default_base = "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1"
    model = _env("LILBOT_MODEL", default_model)
    base_url = _env("LILBOT_BASE_URL", default_base)
    return LilBotConfig(
        workspace=root,
        provider=provider,
        model=model,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        permission_mode=_env("LILBOT_PERMISSION_MODE", "ask"),
    )


def load_config(workspace: Path | None = None) -> LilBotConfig:
    cfg = default_config(workspace)
    path = cfg.config_path
    if not path.exists():
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cfg
    for key, value in data.items():
        if key == "workspace":
            continue
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg


def save_config(cfg: LilBotConfig) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = asdict(cfg)
    data["workspace"] = str(cfg.workspace)
    cfg.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
