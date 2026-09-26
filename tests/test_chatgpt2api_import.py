# -*- coding: utf-8 -*-
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from core import chatgpt2api_import_service as importer
from core import db
from core.web_oauth import _build_authorize_url, _exchange_code, _generate_web_pkce, _is_web_callback_url


class ChatGPT2APIImportTests(unittest.TestCase):
    def _storage_patches(self, root: Path) -> dict:
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
        }

    def test_authorize_url_matches_chatgpt2api_pkce_contract(self):
        class Session:
            device_id = "device-id"

        url = _build_authorize_url(Session(), "state-value", "challenge-value", "user@example.com")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["client_id"], ["app_2SKx67EdpoN0G6j64rFvigXD"])
        self.assertEqual(query["redirect_uri"], ["https://platform.openai.com/auth/callback"])
        self.assertEqual(query["audience"], ["https://api.openai.com/v1"])
        self.assertEqual(query["code_challenge"], ["challenge-value"])
        self.assertIn("offline_access", query["scope"][0])
        self.assertEqual(query["login_hint"], ["user@example.com"])

    def test_web_token_exchange_uses_platform_contract(self):
        captured = {}

        class Response:
            status_code = 200

            def json(self):
                return {
                    "access_token": "web-access",
                    "refresh_token": "web-refresh",
                    "id_token": "",
                }

        class Session:
            def _get_common_headers(self):
                return {"User-Agent": "test"}

            def post(self, url, **kwargs):
                captured["url"] = url
                captured["kwargs"] = kwargs
                return Response()

        credential = _exchange_code(Session(), "authorization-code", "state-value", "verifier")
        self.assertEqual(credential.access_token, "web-access")
        self.assertEqual(credential.refresh_token, "web-refresh")
        self.assertEqual(captured["url"], "https://auth.openai.com/api/accounts/oauth/token")
        self.assertEqual(captured["kwargs"]["json"]["client_id"], "app_2SKx67EdpoN0G6j64rFvigXD")
        self.assertEqual(captured["kwargs"]["json"]["redirect_uri"], "https://platform.openai.com/auth/callback")
        self.assertNotIn("data", captured["kwargs"])

    def test_web_pkce_uses_s256_and_rfc_length(self):
        verifier, challenge = _generate_web_pkce()
        import base64
        import hashlib

        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(len(verifier), 86)
        self.assertEqual(len(challenge), 43)
        self.assertEqual(challenge, expected)

    def test_web_callback_matcher_rejects_localhost_and_other_paths(self):
        self.assertTrue(_is_web_callback_url("https://platform.openai.com/auth/callback?code=x"))
        self.assertFalse(_is_web_callback_url("http://localhost:1455/auth/callback?code=x"))
        self.assertFalse(_is_web_callback_url("https://platform.openai.com/other?code=x"))

    def test_session_json_payload_preserves_web_session_metadata(self):
        payload = importer._credential_payload(
            access_token="session-access",
            email="user@example.com",
            session_info={
                "user": {"id": "user-id", "email": "user@example.com", "mfa": True},
                "account": {"planType": "free", "structure": "individual"},
                "expires": "2030-01-01T00:00:00.000Z",
            },
        )
        self.assertEqual(payload["access_token"], "session-access")
        self.assertEqual(payload["user"]["id"], "user-id")
        self.assertEqual(payload["account"]["planType"], "free")
        self.assertEqual(payload["expires"], "2030-01-01T00:00:00.000Z")
        self.assertEqual(payload["source_type"], "web")
        self.assertNotIn("refresh_token", payload)

    def test_session_json_mode_does_not_start_oauth_and_imports_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                payload = importer._credential_payload(
                    access_token="session-access",
                    email="user@example.com",
                    session_info={
                        "user": {"id": "user-id", "email": "user@example.com"},
                        "account": {"planType": "free"},
                        "expires": "2030-01-01T00:00:00.000Z",
                    },
                )
                db.save_account_pool_import_credentials(13, "user@example.com", payload)
                db.enqueue_account_pool_import(13, "user@example.com", "session_json")
                with patch.object(importer, "_request_import", return_value={
                    "added": 1,
                    "skipped": 0,
                    "synced": 1,
                    "management_id": "session-management-id",
                }) as request_import, patch(
                    "core.web_oauth.obtain_web_oauth_credentials"
                ) as obtain:
                    result = importer.process_due_imports()

                obtain.assert_not_called()
                request_import.assert_called_once_with(payload)
                self.assertEqual(result[0]["status"], "success")

        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                raise AssertionError("connection check must not read account items")

        class Opener:
            def open(self, request, timeout):
                captured["url"] = request.full_url
                captured["method"] = request.method
                captured["headers"] = dict(request.header_items())
                captured["data"] = request.data
                captured["timeout"] = timeout
                return Response()

        with patch.object(importer._cfg, "CHATGPT2API_BASE_URL", "http://127.0.0.1:3001"), patch.object(
            importer._cfg, "CHATGPT2API_MANAGEMENT_KEY", "management-secret"
        ), patch.object(importer, "build_opener", return_value=Opener()):
            result = importer.test_connection()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "connected")
        self.assertEqual(captured["url"], "http://127.0.0.1:3001/api/accounts?page=1&page_size=1")
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer management-secret")
        self.assertIsNone(captured["data"])

    def test_connection_distinguishes_missing_key_invalid_key_and_unreachable(self):
        with patch.object(importer._cfg, "CHATGPT2API_BASE_URL", "http://127.0.0.1:3001"), patch.object(
            importer._cfg, "CHATGPT2API_MANAGEMENT_KEY", ""
        ):
            result = importer.test_connection()
        self.assertEqual(result["status"], "missing_key")

        class Opener:
            def open(self, _request, timeout):
                del timeout
                raise importer.HTTPError(
                    "http://127.0.0.1:3001/api/accounts",
                    401,
                    "Unauthorized",
                    {},
                    None,
                )

        with patch.object(importer._cfg, "CHATGPT2API_MANAGEMENT_KEY", "bad-key"), patch.object(
            importer, "build_opener", return_value=Opener()
        ):
            result = importer.test_connection()
        self.assertEqual(result["status"], "invalid_key")
        self.assertEqual(result["http_status"], 401)

        class NetworkOpener:
            def open(self, _request, timeout):
                del timeout
                raise URLError("connection refused")

        with patch.object(importer._cfg, "CHATGPT2API_MANAGEMENT_KEY", "network-key"), patch.object(
            importer, "build_opener", return_value=NetworkOpener()
        ):
            result = importer.test_connection()
        self.assertEqual(result["status"], "unreachable")

    def test_connection_reports_missing_address_without_network_request(self):
        with patch.object(importer._cfg, "CHATGPT2API_BASE_URL", ""), patch.object(
            importer._cfg, "CHATGPT2API_MANAGEMENT_KEY", "management-secret"
        ), patch.object(importer, "build_opener") as build_opener:
            result = importer.test_connection()

        self.assertEqual(result["status"], "missing_base_url")
        build_opener.assert_not_called()

        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "added": 1,
                    "skipped": 0,
                    "synced": 1,
                    "updated_ids": ["management-id"],
                }).encode()

        class Opener:
            def open(self, request, timeout):
                captured["url"] = request.full_url
                captured["headers"] = dict(request.header_items())
                captured["body"] = json.loads(request.data.decode())
                captured["timeout"] = timeout
                return Response()

        with patch.object(importer._cfg, "CHATGPT2API_BASE_URL", "http://127.0.0.1:3001"), patch.object(
            importer._cfg, "CHATGPT2API_MANAGEMENT_KEY", "management-secret"
        ), patch.object(importer._cfg, "CHATGPT2API_SYNC_AFTER_IMPORT", True), patch.object(
            importer, "build_opener", return_value=Opener()
        ):
            result = importer._request_import({
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "email": "user@example.com",
                "source_type": "web",
            })

        self.assertEqual(captured["url"], "http://127.0.0.1:3001/api/accounts")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer management-secret")
        self.assertEqual(captured["body"]["accounts"][0]["source_type"], "web")
        self.assertTrue(captured["body"]["sync_after_import"])
        self.assertFalse(captured["body"]["return_items"])
        self.assertEqual(result["management_id"], "management-id")

    def test_update_request_uses_management_id_and_omits_unaccepted_fields(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"updated_ids": ["management-id"]}).encode()

        class Opener:
            def open(self, request, timeout):
                captured["url"] = request.full_url
                captured["body"] = json.loads(request.data.decode())
                return Response()

        with patch.object(importer._cfg, "CHATGPT2API_BASE_URL", "http://127.0.0.1:3001"), patch.object(
            importer._cfg, "CHATGPT2API_MANAGEMENT_KEY", "management-secret"
        ), patch.object(importer, "build_opener", return_value=Opener()):
            result = importer._request_update("management-id", {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "email": "user@example.com",
                "source_type": "web",
            })

        self.assertEqual(captured["url"], "http://127.0.0.1:3001/api/accounts/update")
        self.assertEqual(captured["body"], {
            "id": "management-id",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "source_type": "web",
        })
        self.assertEqual(result["management_id"], "management-id")

    def test_outbox_status_contains_no_credentials_and_records_management_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.save_account_pool_import_credentials(
                    7,
                    "user@example.com",
                    {"access_token": "access-token", "refresh_token": "refresh-token"},
                )
                db.enqueue_account_pool_import(7, "user@example.com", "oauth_pkce")
                with patch.object(importer, "_request_import", return_value={
                    "added": 1,
                    "skipped": 0,
                    "synced": 1,
                    "management_id": "management-id",
                }):
                    result = importer.process_due_imports()

                self.assertEqual(result[0]["status"], "success")
                status = db.get_account_pool_import_status(7)
                self.assertEqual(status["management_id"], "management-id")
                self.assertNotIn("access-token", json.dumps(status))
                self.assertNotIn("refresh-token", json.dumps(status))

    def test_oauth_worker_persists_token_set_before_import(self):
        class OAuthCredential:
            email = "user@example.com"

            def as_import_payload(self):
                return {
                    "access_token": "oauth-access",
                    "refresh_token": "oauth-refresh",
                    "id_token": "oauth-id",
                    "email": "user@example.com",
                    "user_id": "user-id",
                    "type": "free",
                    "source_type": "web",
                }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.save_account_pool_import_credentials(
                    8,
                    "user@example.com",
                    {"access_token": "session-access", "email": "user@example.com", "source_type": "web"},
                )
                db.enqueue_account_pool_import(8, "user@example.com", "oauth_pkce")
                with patch(
                    "core.web_oauth.obtain_web_oauth_credentials",
                    return_value=OAuthCredential(),
                ) as obtain, patch.object(importer, "_request_import", return_value={
                    "added": 1,
                    "skipped": 0,
                    "synced": 1,
                    "management_id": "management-id",
                }):
                    result = importer.process_due_imports()

                obtain.assert_called_once_with("user@example.com", proxy=None)
                self.assertEqual(result[0]["status"], "success")
                stored = db.get_account_pool_import_credentials(8)
                self.assertEqual(stored["refresh_token"], "oauth-refresh")
                self.assertNotIn("session-access", json.dumps(db.get_account_pool_import_status(8)))

    def test_existing_management_id_uses_update_without_creating_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.save_account_pool_import_credentials(
                    9,
                    "user@example.com",
                    {"access_token": "new-access", "refresh_token": "new-refresh", "source_type": "web"},
                )
                db.enqueue_account_pool_import(9, "user@example.com", "oauth_pkce")
                db.claim_account_pool_import(9)
                db.finish_account_pool_import(9, status="success", management_id="existing-id")
                db.enqueue_account_pool_import(9, "user@example.com", "oauth_pkce")
                with patch.object(importer, "_request_import") as create, patch.object(
                    importer, "_request_update", return_value={
                        "added": 0,
                        "skipped": 0,
                        "synced": 0,
                        "management_id": "existing-id",
                    }
                ) as update:
                    result = importer.process_due_imports()

                create.assert_not_called()
                update.assert_called_once_with(
                    "existing-id",
                    {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "source_type": "web",
                    },
                )
                self.assertEqual(result[0]["management_id"], "existing-id")

    def test_reenqueue_during_running_preserves_new_work_and_old_management_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.save_account_pool_import_credentials(
                    11,
                    "user@example.com",
                    {"access_token": "new-access", "refresh_token": "new-refresh", "source_type": "web"},
                )
                db.enqueue_account_pool_import(11, "user@example.com", "session")
                self.assertTrue(db.claim_account_pool_import(11))

                reopened = db.enqueue_account_pool_import(11, "user@example.com", "session")
                self.assertEqual(reopened["status"], "pending")
                self.assertEqual(reopened["attempts"], 0)
                self.assertFalse(
                    db.finish_account_pool_import(
                        11,
                        status="success",
                        management_id="created-by-older-request",
                    )
                )
                status = db.get_account_pool_import_status(11)
                self.assertEqual(status["status"], "pending")
                self.assertEqual(status["management_id"], "created-by-older-request")

                with patch.object(importer, "_request_import") as create, patch.object(
                    importer, "_request_update", return_value={
                        "added": 0,
                        "skipped": 0,
                        "synced": 1,
                        "management_id": "created-by-older-request",
                    }
                ) as update:
                    result = importer.process_due_imports()

                create.assert_not_called()
                update.assert_called_once_with(
                    "created-by-older-request",
                    {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "source_type": "web",
                    },
                )
                self.assertEqual(result[0]["status"], "success")

    def test_non_retryable_import_failure_is_delayed_and_redacted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.save_account_pool_import_credentials(
                    12,
                    "user@example.com",
                    {"access_token": "access-token", "refresh_token": "refresh-token"},
                )
                db.enqueue_account_pool_import(12, "user@example.com", "session")
                with patch.object(importer._cfg, "CHATGPT2API_MANAGEMENT_KEY", "management-key"), patch.object(
                    importer,
                    "_request_import",
                    side_effect=importer.ChatGPT2APIError(
                        "HTTP 401 access_token=access-token management-key",
                        status_code=401,
                        retryable=False,
                    ),
                ):
                    result = importer.process_due_imports()

                self.assertEqual(result[0]["status"], "failed")
                status = db.get_account_pool_import_status(12)
                self.assertGreater(status["next_attempt_at"], time.time() + 3600)
                self.assertNotIn("access-token", status["last_error"])
                self.assertNotIn("refresh-token", status["last_error"])
                self.assertNotIn("management-key", status["last_error"])

    def test_invalid_json_response_is_retryable_without_persisting_body(self):
        with self.assertRaises(importer.ChatGPT2APIError) as raised:
            importer._read_json_response(b"access_token=access-token 123456")
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("access-token", str(raised.exception))
        self.assertNotIn("123456", str(raised.exception))

    def test_restart_requeues_interrupted_worker_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.enqueue_account_pool_import(10, "user@example.com", "session")
                self.assertTrue(db.claim_account_pool_import(10))
                self.assertEqual(db.get_account_pool_import_status(10)["status"], "running")
                self.assertEqual(db.recover_account_pool_imports(), 1)
                self.assertEqual(db.get_account_pool_import_status(10)["status"], "pending")
