import base64
import json
import unittest
from unittest.mock import patch

from core.sub2_export import build_sub2_export, build_sub2_oauth_account
from webui.app import create_app


def _jwt(payload):
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


class Sub2ExportTests(unittest.TestCase):
    def setUp(self):
        self.access_token = _jwt({
            "exp": 2_000_000_000,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-fixture",
                "chatgpt_user_id": "user-fixture",
                "chatgpt_plan_type": "plus",
            },
            "https://api.openai.com/profile": {"email": "fixture@example.com"},
        })
        self.id_token = _jwt({
            "email": "fixture@example.com",
            "https://api.openai.com/auth.user_id": "user-fixture",
        })

    def test_builds_sample_compatible_oauth_entry(self):
        entry = build_sub2_oauth_account({
            "id": 7,
            "email": "fixture@example.com",
            "oauth_credentials": {
                "access_token": self.access_token,
                "refresh_token": "refresh-fixture",
                "id_token": self.id_token,
                "account_id": "acct-fixture",
                "expired": "2033-05-18T03:33:20Z",
            },
        }, source_file="codex-fixture.json")

        self.assertEqual(entry["name"], "fixture@example.com")
        self.assertEqual(entry["platform"], "openai")
        self.assertEqual(entry["type"], "oauth")
        self.assertEqual(entry["credentials"]["chatgpt_account_id"], "acct-fixture")
        self.assertEqual(entry["credentials"]["chatgpt_user_id"], "user-fixture")
        self.assertEqual(entry["credentials"]["chatgpt_account_user_id"], "user-fixture__acct-fixture")
        self.assertEqual(entry["credentials"]["expires_at"], 2_000_000_000)
        self.assertEqual(entry["credentials"]["refresh_token"], "refresh-fixture")
        self.assertTrue(entry["extra"]["cpa_ready"])
        self.assertEqual(entry["extra"]["source_file"], "codex-fixture.json")

    def test_marks_access_token_only_export_as_not_refreshable(self):
        entry = build_sub2_oauth_account({
            "id": 8,
            "email": "fixture@example.com",
            "access_token": self.access_token,
        })

        self.assertFalse(entry["extra"]["cpa_ready"])
        self.assertIn("refresh_token", entry["extra"]["cpa_missing_reason"])
        self.assertIn("id_token", entry["extra"]["cpa_missing_reason"])
        self.assertEqual(entry["credentials"]["expires_at"], 2_000_000_000)

    def test_root_shape_matches_sub2_export(self):
        entry = build_sub2_oauth_account({
            "email": "fixture@example.com",
            "access_token": self.access_token,
        })
        payload = build_sub2_export([entry])

        self.assertEqual(list(payload), ["exported_at", "proxies", "accounts"])
        self.assertEqual(payload["proxies"], [])
        self.assertEqual(len(payload["accounts"]), 1)

    def test_web_route_downloads_sub2_json(self):
        account = {
            "id": 9,
            "email": "fixture@example.com",
            "access_token": self.access_token,
            "account_id": "acct-fixture",
            "user_id": "user-fixture",
            "plan_type": "free",
        }
        with (
            patch("webui.app.db.recover_interrupted_plan_checks", return_value=0),
            patch("webui.app.db.recover_interrupted_extract_links", return_value=0),
            patch("webui.app.db.recover_interrupted_codex_agents", return_value=0),
            patch("webui.app.db.list_codex_accounts", return_value=[]),
            patch("webui.app.db.get_account", return_value=account),
        ):
            app = create_app(auth_code="test-code")
            client = app.test_client()
            response = client.post(
                "/api/accounts/download-sub2",
                json={"account_ids": [9]},
                headers={"X-Auth-Code": "test-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("sub2api-account-", response.headers["Content-Disposition"])
        payload = json.loads(response.data)
        self.assertEqual(len(payload["accounts"]), 1)
        self.assertEqual(payload["accounts"][0]["type"], "oauth")

    def test_web_route_prefers_complete_cpa_credentials(self):
        account = {
            "id": 10,
            "email": "fixture@example.com",
            "access_token": self.access_token,
            "codex_status": "success",
        }
        callback_filename = "codex-fixture@example.com-cpa-callback.json"
        cpa_filename = "codex-abcd1234-fixture@example.com-plus.json"
        cpa_credential = {
            "access_token": self.access_token,
            "refresh_token": "refresh-from-cpa",
            "id_token": self.id_token,
            "account_id": "acct-fixture",
            "email": "fixture@example.com",
            "expired": "2033-05-18T03:33:20Z",
        }
        with (
            patch("webui.app.db.recover_interrupted_plan_checks", return_value=0),
            patch("webui.app.db.recover_interrupted_extract_links", return_value=0),
            patch("webui.app.db.recover_interrupted_codex_agents", return_value=0),
            patch("webui.app.db.list_codex_accounts", return_value=[{
                "email": "fixture@example.com",
                "filename": callback_filename,
            }]),
            patch(
                "webui.app.db.read_codex_credential",
                return_value=('{"type":"cpa_callback","email":"fixture@example.com"}', callback_filename),
            ),
            patch("webui.app.db.get_account", return_value=account),
            patch("webui.app.db.mark_codex_exported") as mark_exported,
            patch(
                "core.codex_oauth.list_cpa_codex_auth_files",
                return_value=[{
                    "name": cpa_filename,
                    "email": "fixture@example.com",
                    "type": "codex",
                }],
            ),
            patch(
                "core.codex_oauth.download_cpa_codex_auth_text",
                return_value=(
                    json.dumps(cpa_credential),
                    cpa_filename,
                    {"name": cpa_filename},
                ),
            ),
        ):
            app = create_app(auth_code="test-code")
            client = app.test_client()
            response = client.post(
                "/api/accounts/download-sub2",
                json={"account_ids": [10]},
                headers={"X-Auth-Code": "test-code"},
            )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.data)
        exported = payload["accounts"][0]
        self.assertEqual(exported["credentials"]["refresh_token"], "refresh-from-cpa")
        self.assertEqual(exported["credentials"]["id_token"], self.id_token)
        self.assertTrue(exported["extra"]["cpa_ready"])
        self.assertEqual(exported["extra"]["source_file"], cpa_filename)
        mark_exported.assert_called_once_with(callback_filename)


if __name__ == "__main__":
    unittest.main()
