# -*- coding: utf-8 -*-
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from core import registration_service as service


class RegistrationTaskRegressionTests(unittest.TestCase):
    def test_job_log_context_captures_info_and_restores_root_level(self):
        root = logging.getLogger()
        previous_level = root.level
        try:
            root.setLevel(logging.WARNING)
            with tempfile.TemporaryDirectory() as tmp:
                log_path = Path(tmp) / "job.log"
                with service._JobLogContext(str(log_path)):
                    self.assertEqual(root.level, logging.INFO)
                    logging.getLogger("registration-test").info("captured info")
                self.assertEqual(root.level, logging.WARNING)
                self.assertIn("captured info", log_path.read_text(encoding="utf-8"))
        finally:
            root.setLevel(previous_level)

    def test_nested_job_log_context_keeps_info_enabled_until_last_exit(self):
        root = logging.getLogger()
        previous_level = root.level
        try:
            root.setLevel(logging.WARNING)
            with tempfile.TemporaryDirectory() as tmp:
                first = Path(tmp) / "first.log"
                second = Path(tmp) / "second.log"
                with service._JobLogContext(str(first)):
                    with service._JobLogContext(str(second)):
                        logging.getLogger("registration-test").info("nested captured")
                    self.assertEqual(root.level, logging.INFO)
                self.assertEqual(root.level, logging.WARNING)
                self.assertIn("nested captured", first.read_text(encoding="utf-8"))
                self.assertIn("nested captured", second.read_text(encoding="utf-8"))
        finally:
            root.setLevel(previous_level)

    def test_blank_job_log_returns_a_task_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "empty.log"
            log_path.touch()
            job = {
                "id": 102,
                "status": "failed",
                "log_file": str(log_path),
                "email": "registered@example.com",
                "account_id": 9,
                "started_at": "2026-09-27T10:00:00",
                "completed_at": "2026-09-27T10:01:00",
            }
            with patch.object(service.db, "get_job", return_value=job):
                content = service.read_job_log(102)

        self.assertIn("status=success (account saved)", content)
        self.assertIn("account_id=9", content)

    def test_registered_registration_job_is_success_and_not_codex_retryable(self):
        job = {
            "id": 101,
            "job_type": "registration",
            "status": "failed",
            "account_id": 9,
            "error_message": "Codex 未完成",
        }
        account = {
            "id": 9,
            "email": "registered@example.com",
            "codex_status": "failed",
        }
        with patch.object(service, "_account_for_job", return_value=account):
            info = service.get_retry_info(job)

        self.assertEqual(info["display_status"], "success")
        self.assertFalse(info["retryable"])
        self.assertIsNone(info["retry_action"])
        self.assertIsNone(info["error_message"])

    def test_protocol_registration_does_not_start_codex_when_disabled(self):
        with patch.object(main._codex_cfg, "ENABLE_CODEX_AUTO", False), patch(
            "core.codex_oauth.run_codex_oauth"
        ) as run_codex:
            result = main._run_optional_codex_oauth("registered@example.com")

        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["ok"])
        run_codex.assert_not_called()

    def test_protocol_registration_can_start_codex_when_explicitly_enabled(self):
        expected = {"status": "success", "ok": True}
        with patch.object(main._codex_cfg, "ENABLE_CODEX_AUTO", True), patch(
            "core.codex_oauth.run_codex_oauth", return_value=expected
        ) as run_codex:
            result = main._run_optional_codex_oauth("registered@example.com")

        self.assertIs(result, expected)
        run_codex.assert_called_once_with("registered@example.com")


if __name__ == "__main__":
    unittest.main()
