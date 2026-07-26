# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class PlusTrialEmailExportTests(unittest.TestCase):
    def _client(self):
        patches = [
            patch("webui.app.db.recover_interrupted_plan_checks", return_value=0),
            patch("webui.app.db.recover_interrupted_extract_links", return_value=0),
            patch("webui.app.db.recover_interrupted_codex_agents", return_value=0),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return create_app(auth_code="test-code").test_client()

    def test_exports_only_eligible_email_and_code_url_without_middle_password(self):
        accounts = [
            {
                "id": 3,
                "email": "missing@example.com",
                "current_plan_type": "free",
                "plus_trial_eligible": True,
            },
            {
                "id": 2,
                "email": "not-eligible@example.com",
                "current_plan_type": "free",
                "plus_trial_eligible": False,
            },
            {
                "id": 1,
                "email": "eligible@example.com",
                "current_plan_type": "free",
                "plus_trial_eligible": True,
            },
        ]

        def mailbox(email):
            if email == "eligible@example.com":
                return {
                    "email": email,
                    "password": "mailbox-secret",
                    "code_url": "https://mail.example.test/code?token=fixture",
                }
            return None

        with (
            patch("webui.app.db.list_accounts", return_value=accounts) as list_accounts,
            patch("webui.app.db.get_generic_api_email_by_email", side_effect=mailbox),
        ):
            response = self._client().post(
                "/api/accounts/export-plus-trial-emails",
                json={"archived": "0"},
                headers={"X-Auth-Code": "test-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_data(as_text=True),
            "eligible@example.com----https://mail.example.test/code?token=fixture\n",
        )
        self.assertNotIn("mailbox-secret", response.get_data(as_text=True))
        self.assertEqual(response.headers["X-Exported-Count"], "1")
        self.assertEqual(response.headers["X-Skipped-Count"], "1")
        list_accounts.assert_called_once_with(limit=100000, archived="0", plan_filter="free")

    def test_exports_opened_plus_email_and_code_url(self):
        account = {
            "id": 8,
            "email": "plus@example.com",
            "current_plan_type": "plus",
        }
        mailbox = {
            "email": "plus@example.com",
            "password": "mailbox-secret",
            "code_url": "https://mail.example.test/plus-code?email=plus@example.com",
        }
        with (
            patch("webui.app.db.get_account", return_value=account) as get_account,
            patch("webui.app.db.get_generic_api_email_by_email", return_value=mailbox),
        ):
            response = self._client().post(
                "/api/accounts/export-plus-emails",
                json={"archived": "0", "account_ids": [8]},
                headers={"X-Auth-Code": "test-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_data(as_text=True),
            "plus@example.com---https://mail.example.test/plus-code?email=plus@example.com\n",
        )
        self.assertNotIn("mailbox-secret", response.get_data(as_text=True))
        self.assertEqual(response.get_data(as_text=True).count("---"), 1)
        self.assertNotIn("----", response.get_data(as_text=True))
        get_account.assert_called_once_with(8)

    def test_plus_export_skips_selected_non_plus_account(self):
        account = {
            "id": 9,
            "email": "free@example.com",
            "current_plan_type": "free",
            "plus_trial_eligible": True,
        }
        with patch("webui.app.db.get_account", return_value=account):
            response = self._client().post(
                "/api/accounts/export-plus-emails",
                json={"account_ids": [9]},
                headers={"X-Auth-Code": "test-code"},
            )

        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertEqual(payload["skipped_count"], 1)
        self.assertIn("不是已开通 Plus", payload["skipped"][0]["reason"])

    def test_prepares_one_time_text_download(self):
        account = {
            "id": 1,
            "email": "eligible@example.com",
            "plan_type": "free",
            "plus_trial_eligible": True,
        }
        mailbox = {
            "email": "eligible@example.com",
            "code_url": "https://mail.example.test/code",
        }
        with (
            patch("webui.app.db.list_accounts", return_value=[account]),
            patch("webui.app.db.get_generic_api_email_by_email", return_value=mailbox),
        ):
            client = self._client()
            prepared = client.post(
                "/api/accounts/export-plus-trial-emails",
                json={"prepare": True},
                headers={"X-Auth-Code": "test-code"},
            )
            payload = prepared.get_json()
            downloaded = client.get(
                payload["download_url"],
                headers={"X-Auth-Code": "test-code"},
            )

        self.assertEqual(prepared.status_code, 200)
        self.assertEqual(payload["exported_count"], 1)
        self.assertEqual(payload["skipped_count"], 0)
        self.assertIn("plus-trial-emails-", payload["filename"])
        self.assertEqual(
            downloaded.get_data(as_text=True),
            "eligible@example.com----https://mail.example.test/code\n",
        )
        self.assertIn("text/plain", downloaded.content_type)

    def test_accounts_page_has_plus_codex_retry_selector_without_email_export_buttons(self):
        response = self._client().get("/", headers={"X-Auth-Code": "test-code"})
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="plusAccountsCount"', html)
        self.assertIn('id="showPlusTrialAccountsOnly"', html)
        self.assertIn('id="plusTrialAccountsCount"', html)
        self.assertIn("activeAccountPlanFilter()", html)
        self.assertIn("updateAccountFilterCounts()", html)
        self.assertIn("SHOW_PLUS_TRIAL_ACCOUNTS_ONLY", html)
        self.assertIn('id="btnSelectPlusCodexRetry"', html)
        self.assertIn('id="plusCodexRetryCount"', html)
        self.assertIn("isPlusCodexRetryCandidate", html)
        self.assertIn("['skipped', 'failed']", html)
        self.assertNotIn('id="btnExportPlusEmails"', html)
        self.assertNotIn('id="btnExportPlusTrialEmails"', html)

    def test_filter_counts_reports_plus_and_plus_trial_accounts(self):
        with patch(
            "webui.app.db.list_accounts",
            side_effect=[
                [
                    {"id": 1, "codex_status": "failed"},
                    {"id": 4, "codex_status": "skipped"},
                    {"id": 5, "codex_status": "success"},
                ],
                [{"id": 2}, {"id": 3}],
            ],
        ) as list_accounts:
            response = self._client().get(
                "/api/accounts/filter-counts?archived=only",
                headers={"X-Auth-Code": "test-code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"plus": 3, "plus_trial": 2, "plus_codex_retry": 2})
        self.assertEqual(list_accounts.call_count, 2)
        list_accounts.assert_any_call(limit=100000, archived="only", plan_filter="plus")
        list_accounts.assert_any_call(limit=100000, archived="only", plan_filter="plus_trial")

    def test_plus_trial_plan_filter_requires_free_and_eligible(self):
        from core import db

        self.assertTrue(db._account_matches_plan_filter({
            "current_plan_type": "free",
            "plus_trial_eligible": True,
        }, "plus_trial"))
        self.assertFalse(db._account_matches_plan_filter({
            "current_plan_type": "free",
            "plus_trial_eligible": False,
        }, "plus_trial"))
        self.assertFalse(db._account_matches_plan_filter({
            "current_plan_type": "plus",
            "plus_trial_eligible": True,
        }, "plus_trial"))


if __name__ == "__main__":
    unittest.main()
