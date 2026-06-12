from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lilbot.cli import normalize_model_name, slash_commands_matching, switch_runtime_model
from lilbot.config import DEFAULT_TUI_FONT_SIZE, load_config
from lilbot.config import LilBotConfig
from lilbot.tui.windows_console import set_windows_console_font_size


class ConfigTests(unittest.TestCase):
    def test_loads_project_dotenv_for_deepseek(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "\n".join(
                    [
                        "DEEPSEEK_API_KEY=test-key",
                        "LILBOT_PROVIDER=deepseek",
                        "LILBOT_MODEL=deepseek-v4-flash",
                        "LILBOT_BASE_URL=https://api.deepseek.com",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                cfg = load_config(root)
        self.assertEqual(cfg.provider, "deepseek")
        self.assertEqual(cfg.model, "deepseek-v4-flash")
        self.assertEqual(cfg.base_url, "https://api.deepseek.com")
        self.assertEqual(cfg.api_key, "test-key")

    def test_font_size_defaults_and_dotenv_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {}, clear=True):
                cfg = load_config(root)
        self.assertEqual(cfg.font_size, DEFAULT_TUI_FONT_SIZE)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("LILBOT_FONT_SIZE=21\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                cfg = load_config(root)
        self.assertEqual(cfg.font_size, 21)

    def test_console_font_request_is_safe_off_windows(self):
        status = set_windows_console_font_size(18)
        if os.name != "nt":
            self.assertFalse(status.supported)
            self.assertEqual(status.message, "not windows")

    def test_model_aliases_normalize(self):
        self.assertEqual(normalize_model_name("pro"), "deepseek-v4-pro")
        self.assertEqual(normalize_model_name("flash"), "deepseek-v4-flash")
        self.assertEqual(normalize_model_name("deepseek-v4-pro"), "deepseek-v4-pro")
        self.assertIsNone(normalize_model_name("unknown-model"))

    def test_slash_command_registry_matches_prefix_and_alias(self):
        names = [command.name for command in slash_commands_matching("/mo")]
        self.assertIn("model", names)
        self.assertIn("models", names)

        alias_names = [command.name for command in slash_commands_matching("moxing")]
        self.assertIn("model", alias_names)

    def test_switch_runtime_model_updates_config_and_subagents(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = LilBotConfig(
                workspace=Path(tmp),
                provider="deepseek",
                model="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
            )
            agent = SimpleNamespace(config=cfg, provider=None)
            ctx = SimpleNamespace(config=cfg, subagents=SimpleNamespace(provider=None))
            model = switch_runtime_model(agent, ctx, "pro")
        self.assertEqual(model, "deepseek-v4-pro")
        self.assertEqual(cfg.provider, "deepseek")
        self.assertEqual(cfg.base_url, "https://api.deepseek.com")
        self.assertTrue(callable(ctx.subagents.provider))


if __name__ == "__main__":
    unittest.main()
