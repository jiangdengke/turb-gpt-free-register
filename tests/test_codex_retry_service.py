# -*- coding: utf-8 -*-
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import codex_retry_service


class CodexRetryServiceTests(unittest.TestCase):
    def tearDown(self):
        for email in ("one@example.com", "two@example.com"):
            codex_retry_service.release(email)

    def test_parallel_workers_serialize_runtime_reload_and_log_startup(self):
        active_reloads = 0
        max_active_reloads = 0
        reload_guard = threading.Lock()

        def fake_reload_all():
            nonlocal active_reloads, max_active_reloads
            with reload_guard:
                active_reloads += 1
                max_active_reloads = max(max_active_reloads, active_reloads)
            time.sleep(0.03)
            with reload_guard:
                active_reloads -= 1
            return []

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = {
                "one@example.com": Path(tmpdir) / "one.log",
                "two@example.com": Path(tmpdir) / "two.log",
            }

            def run(email):
                codex_retry_service.reserve(email)
                codex_retry_service.run_worker(
                    email,
                    target_log_path=paths[email],
                )

            with patch("config.reload_all", side_effect=fake_reload_all), patch(
                "core.codex_oauth.run_codex_oauth",
                return_value={"ok": True, "status": "success"},
            ), patch.object(
                codex_retry_service.db, "update_account_codex_status"
            ):
                threads = [
                    threading.Thread(target=run, args=(email,))
                    for email in paths
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(max_active_reloads, 1)
            for path in paths.values():
                text = path.read_text(encoding="utf-8")
                self.assertIn("线程已启动，正在加载运行时配置", text)
                self.assertIn("CODEX_AUTH_URL_SOURCE=", text)
                self.assertIn("CPA_MANAGEMENT_URL=", text)
                self.assertIn("SMS_PROVIDER=", text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
