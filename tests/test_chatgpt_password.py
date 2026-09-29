# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core import chatgpt_password_service as password_service
from core.openai_auth import EmailOtpInvalidError


class _Response:
    def __init__(self, status=200, payload=None, url="https://auth.openai.com/add-password/new-password"):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.url = url
        self.text = ""

    def json(self):
        return self._payload


class _Session:
    device_id = "device-test"
    proxy = None
    proxy_target = None

    def __init__(self):
        self.posts = []
        self.gets = []

    def get_nextauth_headers(self, referer=""):
        return {"referer": referer}

    def get_auth_headers(self, referer=""):
        return {"referer": referer}

    def get_auth_navigate_headers(self, referer="", **kwargs):
        return {"referer": referer}

    def post(self, url, headers=None, data=None):
        self.posts.append((url, headers or {}, data))
        return _Response(
            payload={"url": "https://auth.openai.com/add-password/new-password"},
            url="https://auth.openai.com/add-password/new-password",
        )

    def get(self, url, headers=None, **kwargs):
        self.gets.append((url, headers or {}, kwargs))
        return _Response(url=url)


class ChatgptPasswordProtocolTests(unittest.TestCase):
    def test_task_error_redacts_password_and_otp_values(self):
        error = password_service._safe_error(
            RuntimeError("password=ValidPassword9! otp=123456"),
            "ValidPassword9!",
        )
        self.assertNotIn("ValidPassword9!", error)
        self.assertNotIn("123456", error)

    def test_signin_uses_authenticated_add_password_parameters(self):
        session = _Session()
        with patch.object(password_service, "get_csrf_token", return_value="csrf-value"):
            url = password_service._trigger_add_password(session, "user@example.com")

        self.assertEqual(url, "https://auth.openai.com/add-password/new-password")
        posted_url, _, body = session.posts[0]
        self.assertIn("connection=password", posted_url)
        self.assertIn("reauth=password", posted_url)
        self.assertIn("post_login_add_password=true", posted_url)
        self.assertIn("max_age=0", posted_url)
        self.assertIn("login_hint=user%40example.com", posted_url)
        self.assertIn("csrfToken=csrf-value", body)
        self.assertNotIn("password", body.lower())

    def test_password_mutation_is_after_successful_email_otp_validation(self):
        session = _Session()
        events = []

        def validated(*args, **kwargs):
            events.append("otp_validated")
            return {"continue_url": "https://auth.openai.com/add-password/new-password"}

        def final_post(url, headers=None, data=None):
            events.append("password_post")
            session.posts.append((url, headers or {}, data))
            return _Response(
                payload={"page": {"type": "external_url"}, "continue_url": "https://chatgpt.com/callback"},
                url="https://auth.openai.com/add-password/new-password",
            )

        session.post = final_post
        with patch.object(password_service, "_trigger_add_password", return_value="https://auth.openai.com/authorize"), \
             patch.object(password_service, "_follow_reauth_with_retry", return_value="https://auth.openai.com/email-verification"), \
             patch.object(password_service, "_auth_session_dump"), \
             patch.object(password_service, "_validate_add_password_otp", side_effect=validated), \
             patch.object(password_service, "_follow_add_password_page", return_value="https://auth.openai.com/add-password/new-password"), \
             patch.object(password_service, "_sentinel_headers", return_value={"openai-sentinel-token": "sentinel", "openai-sentinel-so-token": "so-token"}), \
             patch.object(password_service, "follow_oauth_callback"), \
             patch.object(password_service, "fetch_session", side_effect=RuntimeError("no refreshed session")):
            result = password_service.set_chatgpt_password(
                session,
                "user@example.com",
                "ValidPassword9!",
                otp_code="123456",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["otp_validated", "password_post"])
        self.assertEqual(session.posts[0][0], password_service._PASSWORD_ENDPOINT)
        self.assertEqual(json.loads(session.posts[0][2]), {"password": "ValidPassword9!"})
        self.assertEqual(session.posts[0][1]["openai-sentinel-token"], "sentinel")
        self.assertEqual(session.posts[0][1]["openai-sentinel-so-token"], "so-token")

    def test_email_otp_mfa_is_completed_before_password_page(self):
        session = _Session()
        events = []

        def final_post(url, headers=None, data=None):
            events.append("password_post")
            session.posts.append((url, headers or {}, data))
            return _Response(
                payload={"page": {"type": "external_url"}, "continue_url": "https://chatgpt.com/callback"},
                url="https://auth.openai.com/add-password/new-password",
            )

        session.post = final_post
        page_calls = []

        def follow_password_page(session_arg, continue_url, referer):
            page_calls.append((session_arg, continue_url, referer))
            return "https://auth.openai.com/add-password/new-password"

        with patch.object(password_service, "_trigger_add_password", return_value="https://auth.openai.com/authorize"), \
             patch.object(password_service, "_follow_reauth_with_retry", return_value="https://auth.openai.com/email-verification"), \
             patch.object(password_service, "_auth_session_dump"), \
             patch.object(
                 password_service,
                 "_validate_add_password_otp",
                 return_value={
                     "page": {"type": "mfa_challenge", "payload": {"factor_id": "factor-1"}},
                     "continue_url": "https://auth.openai.com/mfa-challenge/factor-1",
                 },
             ), \
             patch.object(password_service, "_account_totp_code", return_value="123456"), \
             patch.object(password_service, "_mfa_issue_challenge", side_effect=lambda *args: events.append("mfa_issue")), \
             patch.object(
                 password_service,
                 "_mfa_verify",
                 side_effect=lambda *args: (events.append("mfa_verify") or {
                     "continue_url": "https://auth.openai.com/add-password/new-password",
                 }),
             ), \
             patch.object(password_service, "_follow_add_password_page", side_effect=follow_password_page), \
             patch.object(password_service, "_sentinel_headers", return_value={"openai-sentinel-token": "sentinel"}), \
             patch.object(password_service, "follow_oauth_callback"), \
             patch.object(password_service, "fetch_session", side_effect=RuntimeError("no refreshed session")):
            result = password_service.set_chatgpt_password(
                session,
                "user@example.com",
                "ValidPassword9!",
                otp_code="123456",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["mfa_issue", "mfa_verify", "password_post"])
        self.assertEqual(session.posts[0][0], password_service._PASSWORD_ENDPOINT)
        self.assertEqual(page_calls[0][2], "https://auth.openai.com/mfa-challenge/factor-1")

    def test_authorize_error_rebuilds_add_password_state_once(self):
        session = _Session()
        with patch.object(
            password_service,
            "_follow_reauth_with_retry",
            side_effect=[
                "https://auth.openai.com/error",
                "https://auth.openai.com/email-verification",
            ],
        ), \
             patch.object(
                 password_service,
                 "_trigger_add_password",
                 return_value="https://auth.openai.com/authorize/rebuilt",
             ) as trigger, \
             patch.object(password_service, "human_delay"):
            final_url = password_service._follow_add_password_authorize(
                session,
                "user@example.com",
                "https://auth.openai.com/authorize/initial",
            )

        self.assertEqual(final_url, "https://auth.openai.com/email-verification")
        trigger.assert_called_once_with(session, "user@example.com")

    def test_mfa_referer_uses_factor_path(self):
        self.assertEqual(
            password_service._mfa_referer("factor-1"),
            "https://auth.openai.com/mfa-challenge/factor-1",
        )

    def test_mfa_continue_url_can_navigate_to_add_password_page(self):
        session = _Session()
        session.get = lambda url, headers=None, **kwargs: _Response(
            url="https://auth.openai.com/add-password/new-password"
        )
        final_url = password_service._follow_add_password_page(
            session,
            "https://auth.openai.com/authorize/resume",
            referer="https://auth.openai.com/mfa-challenge/factor-1",
        )
        self.assertEqual(final_url, "https://auth.openai.com/add-password/new-password")

    def test_add_password_page_rejects_wrong_final_path_with_safe_diagnostic(self):
        session = _Session()
        session.get = lambda url, headers=None, **kwargs: _Response(
            url="https://auth.openai.com/error?state=secret"
        )
        with self.assertRaisesRegex(RuntimeError, r"path=/error"):
            password_service._follow_add_password_page(
                session,
                "https://auth.openai.com/authorize/resume",
                referer="https://auth.openai.com/mfa-challenge/factor-1",
            )

    def test_invalid_otp_does_not_reach_password_endpoint(self):
        session = _Session()
        with patch.object(password_service, "_trigger_add_password", return_value="https://auth.openai.com/authorize"), \
             patch.object(password_service, "_follow_reauth_with_retry", return_value="https://auth.openai.com/email-verification"), \
             patch.object(password_service, "_auth_session_dump"), \
             patch.object(
                 password_service,
                 "_validate_add_password_otp",
                 side_effect=EmailOtpInvalidError("wrong code"),
             ), \
             patch.object(password_service, "_follow_add_password_page") as follow_page:
            with self.assertRaises(EmailOtpInvalidError):
                password_service.set_chatgpt_password(
                    session,
                    "user@example.com",
                    "ValidPassword9!",
                    otp_code="000000",
                )

        follow_page.assert_not_called()
        self.assertEqual(session.posts, [])


class ChatgptPasswordDbTests(unittest.TestCase):
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

    def test_success_persists_registration_password_and_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                account_id = db.insert_account(
                    email="user@example.com",
                    access_token="access-token",
                    email_source="generic_api",
                )
                self.assertTrue(db.claim_account_chatgpt_password(account_id))
                self.assertTrue(db.mark_account_chatgpt_password_running(account_id))
                self.assertTrue(db.update_account_chatgpt_password(
                    account_id,
                    {
                        "ok": True,
                        "status": "success",
                        "message": "完成",
                        "_password": "ValidPassword9!",
                    },
                ))
                row = db.get_account(account_id)

        self.assertEqual(row["chatgpt_password_status"], "success")
        self.assertEqual(json.loads(row["extra_json"])["registration_password"], "ValidPassword9!")
        self.assertEqual(db._extract_registration_password(row), "ValidPassword9!")

    def test_duplicate_enqueue_is_busy_while_worker_owns_account(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                account_id = db.insert_account(email="user@example.com", access_token="access-token")
                with patch.object(password_service, "_RUNNING", {account_id}):
                    result = password_service.enqueue_account_chatgpt_password(
                        account_id=account_id,
                        email="user@example.com",
                        password="ValidPassword9!",
                        access_token="access-token",
                    )
        self.assertTrue(result["busy"])
        self.assertFalse(result["accepted"])


        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                account_id = db.insert_account(
                    email="user@example.com",
                    access_token="access-token",
                    email_source="generic_api",
                )
                db.claim_account_chatgpt_password(account_id)
                db.update_account_chatgpt_password(
                    account_id,
                    {"ok": False, "status": "failed", "error": "OTP 无效"},
                )
                row = db.get_account(account_id)

        self.assertEqual(row["chatgpt_password_status"], "failed")
        self.assertEqual(db._extract_registration_password(row), "")


if __name__ == "__main__":
    unittest.main()
