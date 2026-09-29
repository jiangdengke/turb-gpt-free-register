# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class WebUiChatgptPasswordTests(unittest.TestCase):
    @staticmethod
    def _storage_patches(root: Path) -> dict:
        return {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
            "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_LEGACY_SQLITE": root / "legacy.db",
            "_CODEX_DIR": root / "codex_accounts",
            "_CODEX_AGENT_DIR": root / "codex_agent_accounts",
            "_LEGACY_CODEX_EXPORT_STATE": root / "codex-export.json",
            "_SQLITE_READY": False,
            "_SQLITE_READY_PATH": None,
            "_VIEWER_HTML": root / "viewer.html",
            "_ACCOUNTS_TXT": root / "accounts.txt",
            "_TOKENS_TXT": root / "tokens.txt",
        }

    def test_single_account_endpoint_queues_without_returning_password(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                account_id = db.insert_account(
                    email="user@example.com",
                    access_token="access-token",
                    email_source="generic_api",
                )
                app = create_app(auth_code="test-auth")
                client = app.test_client()
                with patch(
                    "core.chatgpt_password_service.enqueue_account_chatgpt_password",
                    return_value={"accepted": True, "busy": False, "future": object(), "log_path": "/tmp/password.log"},
                ) as enqueue, patch(
                    "core.chatgpt_password_service.queue_settings",
                    return_value={"workers": 1, "queue_limit": 32, "running": 1},
                ):
                    response = client.post(
                        f"/api/accounts/{account_id}/chatgpt-password",
                        json={
                            "password": "ValidPassword9!",
                            "password_confirmation": "ValidPassword9!",
                        },
                        headers={"X-Auth-Code": "test-auth"},
                    )

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertNotIn("ValidPassword9!", json.dumps(body))
        self.assertNotIn("future", body)
        self.assertEqual(enqueue.call_args.kwargs["password"], "ValidPassword9!")

    def test_endpoint_rejects_mismatched_confirmation_before_queue(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                account_id = db.insert_account(email="user@example.com", access_token="access-token")
                app = create_app(auth_code="test-auth")
                client = app.test_client()
                with patch("core.chatgpt_password_service.enqueue_account_chatgpt_password") as enqueue:
                    response = client.post(
                        f"/api/accounts/{account_id}/chatgpt-password",
                        json={"password": "ValidPassword9!", "password_confirmation": "Different9!"},
                        headers={"X-Auth-Code": "test-auth"},
                    )

        self.assertEqual(response.status_code, 400)
        enqueue.assert_not_called()

    def test_lightweight_status_does_not_return_registration_password(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.insert_account(
                    email="user@example.com",
                    access_token="access-token",
                    extra={"registration_password": "ExistingPassword9!"},
                )
                app = create_app(auth_code="test-auth")
                client = app.test_client()
                response = client.get(
                    "/api/accounts/plan-check-status",
                    headers={"X-Auth-Code": "test-auth"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("ExistingPassword9!", json.dumps(response.get_json()))

    def test_endpoint_rejects_existing_chatgpt_password(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                account_id = db.insert_account(
                    email="user@example.com",
                    access_token="access-token",
                    extra={"registration_password": "ExistingPassword9!"},
                )
                app = create_app(auth_code="test-auth")
                client = app.test_client()
                with patch("core.chatgpt_password_service.enqueue_account_chatgpt_password") as enqueue:
                    response = client.post(
                        f"/api/accounts/{account_id}/chatgpt-password",
                        json={"password": "ValidPassword9!"},
                        headers={"X-Auth-Code": "test-auth"},
                    )

        self.assertEqual(response.status_code, 409)
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
