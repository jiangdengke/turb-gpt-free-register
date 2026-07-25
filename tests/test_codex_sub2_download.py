# -*- coding: utf-8 -*-
import base64
import json
import unittest
from unittest.mock import patch

from webui.app import create_app


def _jwt(payload):
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}."


class CodexSub2DownloadTests(unittest.TestCase):
    def setUp(self):
        self.access_token = _jwt({
            "exp": 2_000_000_000,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-fixture",
                "chatgpt_user_id": "user-fixture",
                "chatgpt_plan_type": "plus",
            },
        })
        self.id_token = _jwt({"email": "fixture@example.com"})

    def _app(self):
        patches = [
            patch("webui.app.db.recover_interrupted_plan_checks", return_value=0),
            patch("webui.app.db.recover_interrupted_extract_links", return_value=0),
            patch("webui.app.db.recover_interrupted_codex_agents", return_value=0),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return create_app(auth_code="test-code")

    def test_callback_receipt_downloads_complete_cpa_tokens(self):
        local_filename = "codex-fixture@example.com-cpa-callback.json"
        cpa_filename = "codex-abcd-fixture@example.com-plus.json"
        local_receipt = {
            "type": "cpa_callback",
            "email": "fixture@example.com",
        }
        cpa_credential = {
            "access_token": self.access_token,
            "refresh_token": "refresh-fixture",
            "id_token": self.id_token,
            "account_id": "acct-fixture",
            "email": "fixture@example.com",
        }
        with (
            patch(
                "webui.app.db.read_codex_credential",
                return_value=(json.dumps(local_receipt), local_filename),
            ),
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
            client = self._app().test_client()
            prepared = client.post(
                "/api/codex/download-sub2",
                json={"filenames": [local_filename], "prepare": True},
                headers={"X-Auth-Code": "test-code"},
            )
            self.assertEqual(prepared.status_code, 200)
            meta = prepared.get_json()
            downloaded = client.get(
                meta["download_url"],
                headers={"X-Auth-Code": "test-code"},
            )

        payload = json.loads(downloaded.data)
        credentials = payload["accounts"][0]["credentials"]
        self.assertEqual(credentials["refresh_token"], "refresh-fixture")
        self.assertEqual(credentials["id_token"], self.id_token)
        self.assertEqual(meta["partial_count"], 0)
        self.assertEqual(meta["error_count"], 0)
        mark_exported.assert_called_once_with(local_filename)

    def test_complete_local_tokens_do_not_call_cpa(self):
        local_filename = "codex-fixture@example.com-plus.json"
        local_credential = {
            "access_token": self.access_token,
            "refresh_token": "refresh-local",
            "id_token": self.id_token,
            "account_id": "acct-fixture",
            "email": "fixture@example.com",
        }
        with (
            patch(
                "webui.app.db.read_codex_credential",
                return_value=(json.dumps(local_credential), local_filename),
            ),
            patch("webui.app.db.mark_codex_exported"),
            patch("core.codex_oauth.list_cpa_codex_auth_files") as list_cpa,
        ):
            client = self._app().test_client()
            response = client.post(
                "/api/codex/download-sub2",
                json={"filenames": [local_filename]},
                headers={"X-Auth-Code": "test-code"},
            )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.data)
        self.assertEqual(
            payload["accounts"][0]["credentials"]["refresh_token"],
            "refresh-local",
        )
        list_cpa.assert_not_called()


if __name__ == "__main__":
    unittest.main()
