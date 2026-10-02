# -*- coding: utf-8 -*-
import base64
import json
import unittest
from datetime import datetime, timezone

from core.codex_export_formats import (
    CodexExportError,
    build_cpa_document,
    build_sub2api_account,
    build_sub2api_document,
    validate_cpa_document,
    validate_sub2api_document,
)


NOW = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)


def fake_jwt(payload):
    def enc(value):
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode("utf-8")
        ).rstrip(b"=").decode("ascii")

    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}.sig"


class CodexExportFormatTests(unittest.TestCase):
    def setUp(self):
        self.access = fake_jwt({
            "exp": 1767332645,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "account-1",
                "chatgpt_plan_type": "plus",
                "chatgpt_user_id": "user-1",
            },
            "https://api.openai.com/profile": {"email": "demo@example.com"},
        })
        self.id_token = fake_jwt({
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "account-1",
                "chatgpt_user_id": "user-1",
            },
            "email": "demo@example.com",
        })
        self.payload = {
            "type": "codex",
            "access_token": self.access,
            "refresh_token": "oauth-refresh",
            "id_token": self.id_token,
            "account_id": "account-1",
            "email": "demo@example.com",
            "expired": "2026-01-02T04:04:05Z",
        }

    def test_cpa_matches_flat_reference_shape_and_preserves_explicit_session_token(self):
        result = build_cpa_document(
            {**self.payload, "session_token": "session-jwe"},
            filename="codex-demo@example.com-plus.json",
            now=NOW,
        )

        self.assertEqual(result["type"], "codex")
        self.assertEqual(result["account_id"], "account-1")
        self.assertEqual(result["chatgpt_account_id"], "account-1")
        self.assertEqual(result["access_token"], self.access)
        self.assertEqual(result["refresh_token"], "oauth-refresh")
        self.assertEqual(result["session_token"], "session-jwe")
        self.assertEqual(result["id_token"], self.id_token)
        self.assertEqual(result["plan_type"], "plus")
        self.assertEqual(result["chatgpt_plan_type"], "plus")
        self.assertEqual(result["last_refresh"], "2026-01-02T03:04:05.678Z")

    def test_schema_validators_accept_builder_outputs(self):
        cpa = build_cpa_document(self.payload, filename="codex-demo@example.com-plus.json", now=NOW)
        sub2 = build_sub2api_document(
            [("codex-demo@example.com-plus.json", self.payload)],
            now=NOW,
        )
        self.assertEqual(validate_cpa_document(cpa)["type"], "codex")
        self.assertEqual(len(validate_sub2api_document(sub2)["accounts"]), 1)

    def test_cpa_does_not_infer_session_token_from_refresh_token(self):
        result = build_cpa_document(self.payload, filename="codex-demo@example.com-plus.json", now=NOW)
        self.assertNotIn("session_token", result)
        self.assertEqual(result["refresh_token"], "oauth-refresh")

    def test_cpa_synthesizes_id_token_when_local_record_has_none(self):
        result = build_cpa_document(
            {key: value for key, value in self.payload.items() if key != "id_token"},
            filename="codex-demo@example.com-plus.json",
            now=NOW,
        )
        self.assertTrue(result["id_token"].endswith(".synthetic"))
        self.assertIs(result["id_token_synthetic"], True)
        payload = result["id_token"].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        self.assertEqual(
            claims["https://api.openai.com/auth"]["chatgpt_account_id"],
            "account-1",
        )

    def test_sub2api_matches_reference_oauth_shape(self):
        result = build_sub2api_account(
            self.payload,
            filename="codex-demo@example.com-plus.json",
            now=NOW,
        )
        self.assertEqual(result["platform"], "openai")
        self.assertEqual(result["type"], "oauth")
        self.assertEqual(result["credentials"]["access_token"], self.access)
        self.assertEqual(result["credentials"]["chatgpt_account_id"], "account-1")
        self.assertEqual(result["credentials"]["chatgpt_user_id"], "user-1")
        self.assertEqual(result["credentials"]["email"], "demo@example.com")
        self.assertNotIn("expires_at", result)
        self.assertNotIn("refresh_token", result["credentials"])
        self.assertEqual(result["extra"]["email_key"], "demo_example_com")

    def test_sub2api_adds_expiry_pause_for_session_without_refresh(self):
        payload = {key: value for key, value in self.payload.items() if key != "refresh_token"}
        result = build_sub2api_account(payload, filename="codex-demo@example.com-free.json", now=NOW)
        self.assertEqual(result["expires_at"], 1767332645)
        self.assertTrue(result["auto_pause_on_expired"])
        self.assertIn("expires_at", result["credentials"])
        self.assertIn("expires_in", result["credentials"])

    def test_sub2api_package_contains_all_accounts(self):
        result = build_sub2api_document(
            [("codex-demo@example.com-plus.json", self.payload)],
            now=NOW,
        )
        self.assertEqual(result["exported_at"], "2026-01-02T03:04:05.678Z")
        self.assertEqual(result["proxies"], [])
        self.assertEqual(len(result["accounts"]), 1)

    def test_epoch_milliseconds_are_normalized_without_crashing(self):
        payload = {**self.payload, "expired": 1767326645000}
        result = build_cpa_document(payload, filename="codex-demo@example.com-plus.json", now=NOW)
        self.assertEqual(result["expired"], "2026-01-02T04:04:05.000Z")

    def test_callback_receipt_without_access_token_is_rejected(self):
        with self.assertRaisesRegex(CodexExportError, "access_token"):
            build_cpa_document(
                {"type": "codex_cpa_callback", "email": "demo@example.com"},
                filename="codex-demo@example.com-cpa-callback.json",
                now=NOW,
            )


if __name__ == "__main__":
    unittest.main()
