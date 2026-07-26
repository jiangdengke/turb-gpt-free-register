# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core import registration_service


class RegistrationRecoveryTests(unittest.TestCase):
    def test_batch_retry_info_matches_single_task_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_path = root / "jobs.json"
            accounts_path = root / "accounts.json"
            jobs_path.write_text(json.dumps([
                {"id": 1, "status": "failed", "email": "one@example.com", "account_id": 10},
                {"id": 2, "status": "success", "root_job_id": 1, "email": "one@example.com", "account_id": 10},
                {"id": 3, "status": "failed", "email": "two@example.com"},
                {"id": 4, "status": "running", "email": "three@example.com"},
            ]), encoding="utf-8")
            accounts_path.write_text(json.dumps([
                {"id": 10, "email": "one@example.com", "codex_status": "success"},
                {"id": 11, "email": "two@example.com", "codex_status": "failed"},
            ]), encoding="utf-8")

            with patch.object(db, "_JOBS_JSON", jobs_path), patch.object(db, "_ACCOUNTS_JSON", accounts_path):
                jobs = db.list_jobs(limit=20)
                batch = registration_service.get_retry_info_batch(jobs)
                single = [registration_service.get_retry_info(job) for job in jobs]

            self.assertEqual(batch, single)
            by_id = {job["id"]: info for job, info in zip(jobs, batch)}
            self.assertEqual(by_id[1]["successful_retry_job_id"], 2)
            self.assertFalse(by_id[1]["retryable"])
            self.assertEqual(by_id[3]["retry_action"], "codex")
            self.assertTrue(by_id[3]["retryable"])
            self.assertFalse(by_id[4]["retryable"])

    def test_recovers_only_unconsumed_emails_from_interrupted_registration_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_path = root / "jobs.json"
            accounts_path = root / "accounts.json"
            generic_path = root / "generic.json"
            generic_txt_path = root / "generic.txt"
            outlook_path = root / "outlook.json"
            domain_path = root / "domain.json"

            jobs_path.write_text(json.dumps([
                {
                    "id": 1,
                    "job_type": "registration",
                    "status": "running",
                    "email": "orphan@example.com",
                },
                {
                    "id": 2,
                    "job_type": "registration",
                    "status": "pending",
                    "email": None,
                },
                {
                    "id": 3,
                    "job_type": "registration",
                    "status": "running",
                    "email": "registered@example.com",
                },
                {
                    "id": 4,
                    "job_type": "codex_retry",
                    "status": "running",
                    "email": "codex@example.com",
                },
            ]), encoding="utf-8")
            accounts_path.write_text(json.dumps([
                {"id": 10, "email": "registered@example.com", "access_token": "token"},
            ]), encoding="utf-8")
            generic_path.write_text(json.dumps([
                {"id": 1, "email": "orphan@example.com", "status": "used", "used_at": "old"},
                {"id": 2, "email": "registered@example.com", "status": "used", "used_at": "old"},
            ]), encoding="utf-8")
            outlook_path.write_text("[]", encoding="utf-8")
            domain_path.write_text("[]", encoding="utf-8")

            with (
                patch.object(db, "_JOBS_JSON", jobs_path),
                patch.object(db, "_ACCOUNTS_JSON", accounts_path),
                patch.object(db, "_GENERIC_API_EMAIL_JSON", generic_path),
                patch.object(db, "_GENERIC_API_EMAIL_TXT", generic_txt_path),
                patch.object(db, "_OUTLOOK_JSON", outlook_path),
                patch.object(db, "_DOMAIN_EMAIL_JSON", domain_path),
            ):
                recovered = db.recover_interrupted_registration_jobs()

            self.assertEqual(recovered, {"jobs": 3, "emails": 1})
            jobs = {row["id"]: row for row in json.loads(jobs_path.read_text(encoding="utf-8"))}
            self.assertEqual(jobs[1]["status"], "failed")
            self.assertEqual(jobs[2]["status"], "failed")
            self.assertEqual(jobs[3]["status"], "failed")
            self.assertEqual(jobs[4]["status"], "running")

            pool = {
                row["email"]: row
                for row in json.loads(generic_path.read_text(encoding="utf-8"))
            }
            self.assertEqual(pool["orphan@example.com"]["status"], "available")
            self.assertIsNone(pool["orphan@example.com"]["used_at"])
            self.assertIn("自动回收", pool["orphan@example.com"]["note"])
            self.assertEqual(pool["registered@example.com"]["status"], "used")


if __name__ == "__main__":
    unittest.main()
