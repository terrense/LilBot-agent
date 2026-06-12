from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lilbot.cli import normalize_model_name, switch_runtime_model
from lilbot.config import load_config
from lilbot.config import LilBotConfig


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

    def test_model_aliases_normalize(self):
        self.assertEqual(normalize_model_name("pro"), "deepseek-v4-pro")
        self.assertEqual(normalize_model_name("flash"), "deepseek-v4-flash")
        self.assertEqual(normalize_model_name("deepseek-v4-pro"), "deepseek-v4-pro")
        self.assertIsNone(normalize_model_name("unknown-model"))

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
