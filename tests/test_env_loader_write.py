# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import env_loader


class EnvLoaderWriteTests(unittest.TestCase):
    def test_write_env_values_updates_symlink_target_without_writing_next_to_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            target = data_dir / ".env"
            target.write_text("EXISTING=value\n", encoding="utf-8")
            link = root / ".env"
            link.symlink_to(target)

            old_path = env_loader._ENV_PATH
            old_loaded = env_loader._LOADED
            try:
                env_loader._ENV_PATH = link
                env_loader._LOADED = False
                with patch.dict(os.environ, {}, clear=True):
                    written = env_loader.write_env_values({"PROXY_POOL": "proxy-value"})
            finally:
                env_loader._ENV_PATH = old_path
                env_loader._LOADED = old_loaded

            self.assertEqual(written, ["PROXY_POOL"])
            self.assertTrue(link.is_symlink())
            self.assertIn("PROXY_POOL=\"proxy-value\"", target.read_text(encoding="utf-8"))
            self.assertFalse((root / ".env.env.tmp").exists())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
