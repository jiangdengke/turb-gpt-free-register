# -*- coding: utf-8 -*-
import base64
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


def fake_jwt(payload):
    def enc(value):
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode("utf-8")
        ).rstrip(b"=").decode("ascii")

    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}.sig"


class WebUiCodexExportTests(unittest.TestCase):
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

    @staticmethod
    def _credential(account_id: str, email: str, *, callback: bool = False) -> dict:
        if callback:
            return {"type": "codex_cpa_callback", "email": email}
        access = fake_jwt({
            "exp": 1767332645,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_plan_type": "free",
                "chatgpt_user_id": f"user-{account_id}",
            },
            "https://api.openai.com/profile": {"email": email},
        })
        return {
            "type": "codex",
            "id_token": access,
            "access_token": access,
            "refresh_token": "oauth-refresh",
            "account_id": account_id,
            "email": email,
            "expired": "2026-01-02T04:04:05Z",
        }

    def _app_with_credentials(self, root: Path, credentials: list[tuple[str, dict]]):
        patches = self._storage_patches(root)
        context = patch.multiple(db, **patches)
        context.__enter__()
        for filename, payload in credentials:
            db.upsert_codex_credential(payload, filename)
        app = create_app(auth_code="test-auth")
        return context, app.test_client()

    def test_local_cpa_export_returns_converted_single_file_without_cpa(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context, client = self._app_with_credentials(
                root,
                [("codex-demo@example.com-free.json", self._credential("a1", "demo@example.com"))],
            )
            try:
                response = client.post(
                    "/api/codex/export-local-format",
                    json={"target": "cpa", "filenames": ["codex-demo@example.com-free.json"]},
                    headers={"X-Auth-Code": "test-auth"},
                )
            finally:
                context.__exit__(None, None, None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        body = response.get_json()
        self.assertEqual(body["type"], "codex")
        self.assertEqual(body["account_id"], "a1")
        self.assertEqual(body["chatgpt_account_id"], "a1")
        self.assertNotIn("credentials", body)

    def test_raw_download_keeps_original_local_payload_shape(self):
        payload = self._credential("a1", "demo@example.com")
        payload["local_only_marker"] = "raw-value"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context, client = self._app_with_credentials(
                root,
                [("codex-demo@example.com-free.json", payload)],
            )
            try:
                response = client.get(
                    "/api/codex/download/codex-demo@example.com-free.json",
                    headers={"X-Auth-Code": "test-auth"},
                )
            finally:
                context.__exit__(None, None, None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        downloaded = json.loads(response.data.decode("utf-8"))
        self.assertEqual(downloaded["local_only_marker"], "raw-value")
        self.assertEqual(downloaded["type"], "codex")

    def test_local_cpa_bulk_export_is_individual_files_zip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context, client = self._app_with_credentials(
                root,
                [
                    ("codex-a@example.com-free.json", self._credential("a1", "a@example.com")),
                    ("codex-b@example.com-free.json", self._credential("a2", "b@example.com")),
                ],
            )
            try:
                response = client.post(
                    "/api/codex/export-local-format",
                    json={
                        "target": "cpa",
                        "filenames": [
                            "codex-a@example.com-free.json",
                            "codex-b@example.com-free.json",
                        ],
                    },
                    headers={"X-Auth-Code": "test-auth"},
                )
            finally:
                context.__exit__(None, None, None)

        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            names = set(archive.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("codex-a@example.com-free.json", names)
            self.assertIn("codex-b@example.com-free.json", names)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["target"], "cpa")
            self.assertEqual(manifest["count"], 2)
            self.assertNotIn("oauth-refresh", archive.read("manifest.json").decode("utf-8"))

    def test_local_sub2_export_returns_accounts_package(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context, client = self._app_with_credentials(
                root,
                [("codex-demo@example.com-free.json", self._credential("a1", "demo@example.com"))],
            )
            try:
                response = client.post(
                    "/api/codex/export-local-format",
                    json={"target": "sub2api", "filenames": ["codex-demo@example.com-free.json"]},
                    headers={"X-Auth-Code": "test-auth"},
                )
            finally:
                context.__exit__(None, None, None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        body = response.get_json()
        self.assertEqual(body["proxies"], [])
        self.assertEqual(len(body["accounts"]), 1)
        self.assertEqual(body["accounts"][0]["type"], "oauth")
        self.assertEqual(body["accounts"][0]["credentials"]["email"], "demo@example.com")

    def test_both_webui_templates_expose_local_export_controls(self):
        for filename in ("webui/templates/index.html", "webui/templates/index_legacy.html"):
            text = Path(filename).read_text(encoding="utf-8")
            self.assertIn("/api/codex/export-local-format", text)
            self.assertIn("导出CPA", text)
            self.assertIn("导出Sub2", text)
            self.assertIn("下载原文", text)

    def test_callback_receipt_is_rejected_for_sub2_export(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            context, client = self._app_with_credentials(
                root,
                [("codex-demo@example.com-cpa-callback.json", self._credential("", "demo@example.com", callback=True))],
            )
            try:
                response = client.post(
                    "/api/codex/export-local-format",
                    json={"target": "sub2api", "filenames": ["codex-demo@example.com-cpa-callback.json"]},
                    headers={"X-Auth-Code": "test-auth"},
                )
            finally:
                context.__exit__(None, None, None)

        self.assertEqual(response.status_code, 422)
        self.assertIn("access_token", json.dumps(response.get_json()))


if __name__ == "__main__":
    unittest.main()
