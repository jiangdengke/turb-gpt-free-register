# -*- coding: utf-8 -*-
import logging
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import Mock, patch

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

    def test_finalize_registration_clears_breaker_after_retryable_callback_403(self):
        class FakeSession:
            def __init__(self):
                self.reset_calls = 0

            def reset_circuit_breaker(self):
                self.reset_calls += 1

        session = FakeSession()
        callback_403 = HTTPError(
            "https://chatgpt.com/api/auth/callback/openai",
            403,
            "",
            {},
            None,
        )
        with patch.object(
            main, "follow_oauth_callback", side_effect=[callback_403, "https://chatgpt.com/"]
        ) as follow_callback, patch.object(
            main, "fetch_session", return_value={"accessToken": "synthetic-access-token"}
        ), patch.object(main, "human_delay"), patch.object(main.time, "sleep"):
            session_info, access_token = main._finalize_registration_session(
                session,
                "https://auth.openai.com/authorize/continue?synthetic=1",
                "synthetic@example.com",
            )

        self.assertEqual(session_info["accessToken"], "synthetic-access-token")
        self.assertEqual(access_token, "synthetic-access-token")
        self.assertEqual(session.reset_calls, 1)
        self.assertEqual(follow_callback.call_count, 2)

    def test_prepare_registration_args_prefers_retry_job_email(self):
        expected_email = "original@example.com"
        with patch("config.register.REGISTER_EMAIL", "configured@example.com"), patch(
            "config.email.USE_EMAIL_SERVICE", True
        ), patch.object(service, "_random_display_name", return_value="Synthetic Name"), patch(
            "core.profile_utils.generate_random_birthday", return_value="1990-01-01"
        ):
            email, name, birthday = service._prepare_registration_args(expected_email)

        self.assertEqual(email, expected_email)
        self.assertEqual(name, "Synthetic Name")
        self.assertEqual(birthday, "1990-01-01")

    def test_registration_retry_job_inherits_source_email(self):
        source = {
            "id": 91,
            "job_type": "registration",
            "status": "failed",
            "email": "original@example.com",
            "email_source": "generic_api",
            "log_file": "/tmp/retry.log",
        }
        retry = {
            "id": 92,
            "job_type": "registration",
            "status": "pending",
            "email": source["email"],
            "log_file": "/tmp/retry-child.log",
        }
        executor = Mock()
        with patch.object(service.db, "get_job", return_value=source), patch.object(
            service, "get_retry_info", return_value={"retryable": True, "retry_action": "registration"}
        ), patch.object(service.db, "create_retry_job", return_value=(retry, True)) as create_retry, patch.object(
            service, "get_executor", return_value=executor
        ), patch.object(service, "_executor_lock"):
            result = service.retry_job(91, workers=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["job"]["email"], source["email"])
        create_retry.assert_called_once_with(
            91,
            job_type="registration",
            email_source="generic_api",
            email="original@example.com",
            account_id=None,
        )
        executor.submit.assert_called_once_with(
            service._run_one_job, 92, "/tmp/retry-child.log"
        )

    def test_run_one_job_passes_recorded_email_to_registration(self):
        source = {
            "id": 92,
            "status": "pending",
            "email": "original@example.com",
            "log_file": "/tmp/retry-child.log",
        }
        with patch.object(service, "_activate_job"), patch.object(service, "_deactivate_job"), patch.object(
            service.db, "get_job", return_value=source
        ), patch.object(service.db, "update_job"), patch.object(
            service, "_prepare_registration_args", return_value=(source["email"], "Synthetic Name", "1990-01-01")
        ) as prepare, patch.object(service, "check_stop_requested"), patch(
            "main.run_registration", return_value={"success": True, "email": source["email"], "account_id": 12}
        ) as run_registration:
            service._run_one_job(92, source["log_file"])

        prepare.assert_called_once_with(source["email"])
        self.assertEqual(run_registration.call_args.kwargs["email"], source["email"])

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
