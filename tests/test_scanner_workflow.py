# -*- coding: utf-8 -*-
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, scan_monitor_service
from webui.app import create_app


def _account(*, plan: str = "free", job_id: str = "job-1") -> dict:
    return {
        "id": 41,
        "email": "scanner-test@example.com",
        "current_plan_type": plan,
        "plus_trial_eligible": plan == "free",
        "extract_link_status": "success",
        "extract_link_ok": True,
        "extract_link_job_id": job_id,
        "extract_link_completed_at": "2026-07-26T10:00:00",
        "extract_link_type": "upi",
        "extract_link_payment_method": "UPI",
        "extract_link_image_url_png": f"https://fixture.invalid/{job_id}.png",
        "extract_link_long_url": f"https://fixture.invalid/pay/{job_id}",
        "extract_link_expires_at": time.time() + 3600,
    }


def _extract_candidate() -> dict:
    return {
        "id": 52,
        "email": "candidate-scanner@example.com",
        "current_plan_type": "free",
        "plus_trial_eligible": True,
        "access_token": "fixture-access-token",
    }


class ScannerPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patchers = [
            patch.object(db, "_SCANNERS_JSON", root / "scanners.json"),
            patch.object(db, "_SCAN_TASKS_JSON", root / "tasks.json"),
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"),
        ]
        for item in self.patchers:
            item.start()
        db._write_json(db._ACCOUNTS_JSON, [_account()])

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.tmp.cleanup()

    def test_scanner_key_is_hashed_and_can_be_revoked(self):
        scanner, key = db.create_scanner("扫码员 A")

        persisted = db._SCANNERS_JSON.read_text(encoding="utf-8")
        self.assertNotIn(key, persisted)
        self.assertEqual(db.authenticate_scanner_key(key)["id"], scanner["id"])

        db.set_scanner_enabled(scanner["id"], False)
        self.assertIsNone(db.authenticate_scanner_key(key))

    def test_claim_is_exclusive_and_plus_completion_is_automatic(self):
        scanner_a, _ = db.create_scanner("扫码员 A")
        scanner_b, _ = db.create_scanner("扫码员 B")
        queue = db.get_scanner_queue(scanner_a["id"])

        self.assertEqual(queue["counts"]["pending"], 1)
        self.assertNotIn("qr_url", queue["pending"][0])
        task_id = queue["pending"][0]["id"]

        claimed = db.claim_scan_task(task_id, scanner_a["id"])
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["qr_url"], "https://fixture.invalid/job-1.png")
        with self.assertRaisesRegex(RuntimeError, "已被领取"):
            db.claim_scan_task(task_id, scanner_b["id"])

        with self.assertRaisesRegex(ValueError, "系统自动检测"):
            db.update_scan_task_by_scanner(task_id, scanner_a["id"], "scanned")
        account = _account(plan="plus")
        db._write_json(db._ACCOUNTS_JSON, [account])

        refreshed = db.get_scanner_queue(scanner_a["id"])
        completed = next(item for item in refreshed["mine"] if item["id"] == task_id)
        self.assertEqual(completed["status"], "completed")
        self.assertIsNotNone(completed["scanned_at"])
        self.assertNotIn("qr_url", completed)

    def test_claimed_task_is_queued_for_automatic_plan_check(self):
        scanner, _ = db.create_scanner("扫码员 A")
        task_id = db.get_scanner_queue(scanner["id"])["pending"][0]["id"]
        db.claim_scan_task(task_id, scanner["id"])
        account = _account()
        account["access_token"] = "fixture-access-token"

        with (
            patch.object(db, "get_account", return_value=account),
            patch.object(
                scan_monitor_service.plan_check_service,
                "enqueue_account_plan_check",
                return_value={"accepted": True},
            ) as enqueue,
        ):
            stats = scan_monitor_service.run_once()

        self.assertEqual(stats["queued"], 1)
        self.assertEqual(enqueue.call_args.kwargs["trigger"], "scan_auto")

    def test_expired_lease_returns_task_to_pending_queue(self):
        scanner_a, _ = db.create_scanner("扫码员 A")
        scanner_b, _ = db.create_scanner("扫码员 B")
        task_id = db.get_scanner_queue(scanner_a["id"])["pending"][0]["id"]
        db.claim_scan_task(task_id, scanner_a["id"], lease_seconds=60)

        tasks = db._read_json(db._SCAN_TASKS_JSON, [])
        tasks[0]["lease_expires_at"] = time.time() - 1
        db._write_json(db._SCAN_TASKS_JSON, tasks)

        queue = db.get_scanner_queue(scanner_b["id"])
        self.assertEqual([item["id"] for item in queue["pending"]], [task_id])
        stored = db._read_json(db._SCAN_TASKS_JSON, [])[0]
        self.assertIsNone(stored["scanner_id"])
        self.assertEqual(stored["events"][-1]["action"], "lease_expired")

    def test_new_payment_link_supersedes_unfinished_task(self):
        scanner, _ = db.create_scanner("扫码员 A")
        first_id = db.get_scanner_queue(scanner["id"])["pending"][0]["id"]
        db._write_json(db._ACCOUNTS_JSON, [_account(job_id="job-2")])

        queue = db.get_scanner_queue(scanner["id"])
        self.assertEqual(len(queue["pending"]), 1)
        self.assertNotEqual(queue["pending"][0]["id"], first_id)
        old = next(item for item in db.list_scan_tasks_admin() if item["id"] == first_id)
        self.assertEqual(old["status"], "superseded")

    def test_scanner_extract_success_auto_claims_generated_task(self):
        scanner, _ = db.create_scanner("扫码员 A")
        db._write_json(db._ACCOUNTS_JSON, [_extract_candidate()])

        before = db.get_scanner_queue(scanner["id"])
        self.assertEqual(before["counts"]["extractable"], 1)
        candidate = before["extract_candidates"][0]
        self.assertEqual(candidate["email"], "ca******@example.com")
        self.assertNotIn("access_token", candidate)

        self.assertTrue(db.claim_account_extract(
            52,
            trigger=f"scanner:{scanner['id']}",
            link_type="upi",
            scanner_id=scanner["id"],
        ))
        db.update_account_extract(52, {
            "ok": True,
            "status": "success",
            "job_id": "scanner-job",
            "link_type": "upi",
            "result": {
                "payment_method": "UPI",
                "image_url_png": "https://fixture.invalid/scanner-job.png",
                "long_url": "https://fixture.invalid/pay/scanner-job",
                "expires_at": time.time() + 3600,
            },
        })

        after = db.get_scanner_queue(scanner["id"])
        self.assertEqual(after["counts"]["extractable"], 0)
        self.assertEqual(after["counts"]["claimed"], 1)
        claimed = after["mine"][0]
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["scanner_id"], scanner["id"])
        self.assertEqual(claimed["qr_url"], "https://fixture.invalid/scanner-job.png")
        self.assertGreater(claimed["lease_expires_at"], time.time())


class ScannerRoleTests(unittest.TestCase):
    def test_scanner_login_is_restricted_to_workbench(self):
        scanner = {
            "id": 7,
            "name": "扫码员 A",
            "enabled": True,
            "key_version": 1,
        }
        queue = {
            "scanner": scanner,
            "pending": [],
            "mine": [],
            "counts": {"pending": 0, "claimed": 0, "scanned": 0, "completed": 0},
        }
        with (
            patch("webui.auth.db.authenticate_scanner_key", return_value=scanner),
            patch("webui.auth.db.get_scanner", return_value=scanner),
            patch("webui.app.db.get_scanner_queue", return_value=queue),
        ):
            app = create_app(auth_code="admin-code")
            client = app.test_client()

            login = client.post("/login", data={"auth_code": "scanner-key", "next": "/"})
            self.assertEqual(login.status_code, 302)
            self.assertTrue(login.headers["Location"].endswith("/scan"))
            self.assertEqual(client.get("/scan").status_code, 200)
            self.assertEqual(client.get("/api/scan/queue").status_code, 200)
            self.assertEqual(client.get("/api/summary").status_code, 403)
            self.assertTrue(client.get("/").headers["Location"].endswith("/scan"))

    def test_key_reset_invalidates_existing_scanner_session(self):
        scanner = {
            "id": 7,
            "name": "扫码员 A",
            "enabled": True,
            "key_version": 1,
        }
        with (
            patch("webui.auth.db.authenticate_scanner_key", return_value=scanner),
            patch("webui.auth.db.get_scanner", return_value=scanner) as get_scanner,
        ):
            app = create_app(auth_code="admin-code")
            client = app.test_client()
            client.post("/login", data={"auth_code": "scanner-key"})
            get_scanner.return_value = {**scanner, "key_version": 2}

            response = client.get("/scan")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login", response.headers["Location"])

    def test_admin_session_cannot_use_scanner_queue_api(self):
        app = create_app(auth_code="admin-code")
        client = app.test_client()

        response = client.get("/api/scan/queue", headers={"X-Auth-Code": "admin-code"})

        self.assertEqual(response.status_code, 403)
        self.assertIn("仅供扫码员", response.get_json()["error"])

    def test_scanner_can_enqueue_extract_without_receiving_cdk(self):
        scanner = {
            "id": 7,
            "name": "扫码员 A",
            "enabled": True,
            "key_version": 1,
        }
        account = _extract_candidate()
        queued = {"accepted": True, "busy": False, "link_type": "upi"}
        with (
            patch("webui.auth.db.authenticate_scanner_key", return_value=scanner),
            patch("webui.auth.db.get_scanner", return_value=scanner),
            patch("webui.app.db.get_account", return_value=account),
            patch("webui.app.extract_link_service.validate_settings"),
            patch("webui.app.extract_link_service.enqueue_account_extract", return_value=queued) as enqueue,
        ):
            app = create_app(auth_code="admin-code")
            client = app.test_client()
            client.post("/login", data={"auth_code": "scanner-key"})

            response = client.post("/api/scan/accounts/52/extract")

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["started"])
        self.assertEqual(enqueue.call_args.kwargs["scanner_id"], scanner["id"])
        self.assertEqual(enqueue.call_args.kwargs["trigger"], "scanner:7")
        self.assertIsNone(enqueue.call_args.kwargs.get("cdk"))


if __name__ == "__main__":
    unittest.main()
