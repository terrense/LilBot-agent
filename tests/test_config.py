from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lilbot.config import load_config


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


if __name__ == "__main__":
    unittest.main()

