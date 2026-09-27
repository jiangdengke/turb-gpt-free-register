# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core import db


class EmailPoolSourceTests(unittest.TestCase):
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

    def test_generic_api_import_keeps_source_for_all_and_filtered_lists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                self.assertEqual(
                    db.import_generic_api_emails([
                        {"email": "generic@example.com", "code_url": "https://mail.example/code"},
                    ]),
                    (1, 0),
                )

                all_items = db.list_email_pool_page(source="all", limit=10)["items"]
                generic_items = db.list_email_pool_page(source="generic_api", limit=10)["items"]
                self.assertEqual(all_items[0]["source"], "generic_api")
                self.assertEqual(generic_items[0]["source"], "generic_api")

    def test_registered_generic_api_email_cannot_be_released_or_claimed_again(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.import_generic_api_emails([
                    {"email": "registered@example.com", "code_url": "https://mail.example/registered"},
                ])
                account_id = db.insert_account(
                    email="registered@example.com",
                    access_token="synthetic-token",
                    email_source="generic_api",
                )

                pool_row = db.get_generic_api_email_by_email("registered@example.com")
                self.assertEqual(pool_row["status"], "used")
                self.assertEqual(pool_row["registered_account_id"], account_id)

                db.release_generic_api_email("registered@example.com", status="available")
                pool_row = db.get_generic_api_email_by_email("registered@example.com")
                self.assertEqual(pool_row["status"], "used")
                self.assertEqual(pool_row["registered_account_id"], account_id)
                stale_rows = db._load_generic_api_emails()
                stale_rows[0]["status"] = "available"
                stale_rows[0]["used_at"] = None
                db._save_generic_api_emails(stale_rows)
                self.assertFalse(
                    db.release_unconsumed_generic_api_email(
                        "registered@example.com", note="stale recovery"
                    )
                )
                pool_row = db.get_generic_api_email_by_email("registered@example.com")
                self.assertEqual(pool_row["status"], "used")
                self.assertIsNone(db.claim_next_generic_api_email())

    def test_generic_claim_repairs_stale_available_row_for_saved_account(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.import_generic_api_emails([
                    {"email": "registered@example.com", "code_url": "https://mail.example/registered"},
                    {"email": "fresh@example.com", "code_url": "https://mail.example/fresh"},
                ])
                account_id = db.insert_account(
                    email="registered@example.com",
                    access_token="synthetic-token",
                    email_source="generic_api",
                )

                # 模拟旧版本曾把成功账号邮箱错误恢复为 available。
                rows = db._load_generic_api_emails()
                stale = db._find_by_email(rows, "registered@example.com")
                stale["status"] = "available"
                stale["used_at"] = None
                db._save_generic_api_emails(rows)

                claimed = db.claim_next_generic_api_email()
                self.assertEqual(claimed["email"], "fresh@example.com")
                repaired = db.get_generic_api_email_by_email("registered@example.com")
                self.assertEqual(repaired["status"], "used")
                self.assertEqual(repaired["registered_account_id"], account_id)

    def test_repairs_source_for_legacy_generic_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db._ensure_sqlite()
                payload = {
                    "id": 1,
                    "email": "legacy-generic@example.com",
                    "code_url": "https://mail.example/legacy-code",
                    "status": "available",
                }
                with closing(db._sqlite_conn()) as conn:
                    conn.execute(
                        "INSERT INTO email_pool(id,email,source,status,archived,created_at,updated_at,payload) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (1, payload["email"], "", "available", 0, "", "", json.dumps(payload)),
                    )
                    conn.commit()

                db._SQLITE_READY = False
                db._SQLITE_READY_PATH = None
                items = db.list_email_pool_page(source="generic_api", limit=10)["items"]
                self.assertEqual(items[0]["source"], "generic_api")

    def test_repairs_source_for_legacy_domain_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db._ensure_sqlite()
                payload = {
                    "id": 1,
                    "email": "legacy-domain@example.com",
                    "status": "available",
                    "used_at": None,
                }
                with closing(db._sqlite_conn()) as conn:
                    conn.execute(
                        "INSERT INTO email_pool(id,email,source,status,archived,created_at,updated_at,payload) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (1, payload["email"], "", "available", 0, "", "", json.dumps(payload)),
                    )
                    conn.commit()

                db._SQLITE_READY = False
                db._SQLITE_READY_PATH = None
                items = db.list_email_pool_page(source="cloudflare_domain", limit=10)["items"]
                self.assertEqual(items[0]["source"], "cloudflare_domain")

    def test_delete_pool_supports_all_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.import_generic_api_emails([
                    {"email": "generic@example.com", "code_url": "https://mail.example/code"},
                ])
                db.import_outlook_accounts([
                    {
                        "email": "outlook@example.com",
                        "password": "password",
                        "client_id": "client",
                        "refresh_token": "refresh",
                    },
                ])
                db.claim_next_domain_email("domain@example.com")

                self.assertTrue(db.delete_email_pool("generic@example.com", source="all"))
                self.assertTrue(db.delete_email_pool("outlook@example.com", source="outlook"))
                self.assertTrue(db.delete_email_pool("domain@example.com", source="cloudflare_domain"))
                self.assertFalse(db.list_email_pool_page(source="all", limit=10)["items"])


if __name__ == "__main__":
    unittest.main()
