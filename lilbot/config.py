from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_MAX_STEPS = 20
DEFAULT_TUI_FONT_SIZE = 22
DEFAULT_SUBAGENT_MAX_CONCURRENT = 8


@dataclass
class LilBotConfig:
    workspace: Path
    provider: str = "auto"
    model: str = "lilbot-rule-model"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    permission_mode: str = "ask"
    max_steps: int = DEFAULT_MAX_STEPS
    font_size: int = DEFAULT_TUI_FONT_SIZE
    subagent_max_concurrent: int = DEFAULT_SUBAGENT_MAX_CONCURRENT
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


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def load_dotenv(workspace: Path) -> None:
    path = workspace / ".env"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def default_config(workspace: Path | None = None) -> LilBotConfig:
    root = (workspace or Path.cwd()).resolve()
    load_dotenv(root)
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
        font_size=max(0, _env_int("LILBOT_FONT_SIZE", DEFAULT_TUI_FONT_SIZE)),
        subagent_max_concurrent=max(1, _env_int("LILBOT_SUBAGENT_MAX_CONCURRENT", DEFAULT_SUBAGENT_MAX_CONCURRENT)),
    )


def apply_env_overrides(cfg: LilBotConfig) -> LilBotConfig:
    deepseek_key = _env("DEEPSEEK_API_KEY", "")
    if _env("LILBOT_PROVIDER"):
        cfg.provider = _env("LILBOT_PROVIDER")
    elif deepseek_key and cfg.provider == "auto":
        cfg.provider = "deepseek"

    if _env("LILBOT_MODEL"):
        cfg.model = _env("LILBOT_MODEL")
    elif cfg.provider == "deepseek" and cfg.model == "lilbot-rule-model":
        cfg.model = "deepseek-v4-flash"

    if _env("LILBOT_BASE_URL"):
        cfg.base_url = _env("LILBOT_BASE_URL").rstrip("/")
    elif cfg.provider == "deepseek":
        cfg.base_url = "https://api.deepseek.com"

    cfg.api_key = _env("LILBOT_API_KEY", _env("OPENAI_API_KEY", deepseek_key or cfg.api_key))

    if _env("LILBOT_PERMISSION_MODE"):
        cfg.permission_mode = _env("LILBOT_PERMISSION_MODE")
    if _env("LILBOT_FONT_SIZE"):
        cfg.font_size = max(0, _env_int("LILBOT_FONT_SIZE", cfg.font_size))
    if _env("LILBOT_MAX_STEPS"):
        cfg.max_steps = max(1, _env_int("LILBOT_MAX_STEPS", cfg.max_steps))
    elif cfg.max_steps == 8:
        cfg.max_steps = DEFAULT_MAX_STEPS
    if _env("LILBOT_SUBAGENT_MAX_CONCURRENT"):
        cfg.subagent_max_concurrent = max(1, _env_int("LILBOT_SUBAGENT_MAX_CONCURRENT", cfg.subagent_max_concurrent))
    return cfg


def load_config(workspace: Path | None = None) -> LilBotConfig:
    cfg = default_config(workspace)
    path = cfg.config_path
    if not path.exists():
        return apply_env_overrides(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cfg
    for key, value in data.items():
        if key == "workspace":
            continue
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return apply_env_overrides(cfg)


def save_config(cfg: LilBotConfig) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = asdict(cfg)
    data["workspace"] = str(cfg.workspace)
    cfg.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
