# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core.roxy_codex_oauth import _otp_values_match, _wait_for_fresh_email_otp


class RoxyCodexEmailOtpTests(unittest.TestCase):
    def test_single_otp_input_must_contain_full_code(self):
        state = {
            "inputs": [
                {
                    "autocomplete": "one-time-code",
                    "value": "298716",
                }
            ]
        }

        self.assertTrue(_otp_values_match(state, "298716"))
        state["inputs"][0]["value"] = "29871"
        self.assertFalse(_otp_values_match(state, "298716"))

    def test_split_otp_inputs_are_joined_for_validation(self):
        state = {
            "inputs": [
                {"inputmode": "numeric", "value": digit}
                for digit in "298716"
            ]
        }

        self.assertTrue(_otp_values_match(state, "298716"))

    @patch("core.generic_api_mail_client.is_fresh_otp", return_value=True)
    def test_new_message_may_reuse_previously_submitted_code(self, is_fresh):
        provider = lambda email, after_ts=None: "298716"

        code = _wait_for_fresh_email_otp(
            provider,
            "fresh@example.test",
            after_ts=1785003200.0,
            used_codes={"298716"},
            timeout=1,
        )

        self.assertEqual(code, "298716")
        is_fresh.assert_called_once_with(
            "fresh@example.test",
            "298716",
            1785003200.0,
        )


if __name__ == "__main__":
    unittest.main()
