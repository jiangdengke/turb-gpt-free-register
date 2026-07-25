import unittest
from unittest.mock import patch

from core import db


class ExtractLinkExpiryTests(unittest.TestCase):
    def test_decorate_account_marks_second_timestamp_expired(self):
        with patch("core.db.time.time", return_value=2_000):
            row = db._decorate_account({
                "id": 1,
                "extract_link_status": "success",
                "extract_link_expires_at": "1999",
            })

        self.assertTrue(row["extract_link_expired"])

    def test_decorate_account_supports_millisecond_timestamp(self):
        with patch("core.db.time.time", return_value=2_000_000_000):
            row = db._decorate_account({
                "id": 1,
                "extract_link_status": "success",
                "extract_link_expires_at": "2001000000000",
            })

        self.assertFalse(row["extract_link_expired"])

    def test_status_snapshot_exposes_expiry_and_changes_revision(self):
        account = {
            "id": 26,
            "email": "fixture@example.com",
            "updated_at": "2026-07-25T15:42:19",
            "extract_link_status": "success",
            "extract_link_expires_at": "2000",
        }
        with patch("core.db._load_accounts", return_value=[account]):
            with patch("core.db.time.time", return_value=1_999):
                before = db.list_account_plan_check_statuses()
            with patch("core.db.time.time", return_value=2_001):
                after = db.list_account_plan_check_statuses()

        self.assertFalse(before["items"][0]["extract_link_expired"])
        self.assertTrue(after["items"][0]["extract_link_expired"])
        self.assertNotEqual(before["revision"], after["revision"])


if __name__ == "__main__":
    unittest.main()
