# -*- coding: utf-8 -*-
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import generic_api_mail_client as client
from core.generic_api_mail_client import (
    GenericApiEmailAccount,
    _extract_code,
    _extract_code_with_meta,
    _structured_request_url,
)


class GenericApiMailClientTests(unittest.TestCase):
    def test_extract_code_reads_visible_html_otp(self):
        body = "<html><body>Your verification code is <b>654321</b>.</body></html>"

        self.assertEqual(_extract_code(body), "654321")

    def test_extract_code_reads_plain_text_otp(self):
        self.assertEqual(_extract_code("112233"), "112233")

    def test_extract_code_reads_json_otp(self):
        self.assertEqual(
            _extract_code('{"subject":"Your code","content":"Code: 445566"}'),
            "445566",
        )

    def test_extract_code_ignores_html_attribute_digits(self):
        body = """
        <html>
          <body>
            <div style="color:#666666">Microsoft account</div>
            <a href="https://example.test/privacy?id=778899">Privacy statement</a>
          </body>
        </html>
        """

        self.assertIsNone(_extract_code(body))

    def test_extracts_newest_structured_message_with_timestamp(self):
        body = json.dumps({
            "data": [
                {"date": "2026-07-25T18:10:00Z", "verifyCode": {"code": "112233"}},
                {"date": "2026-07-25T18:14:23Z", "verifyCode": {"code": "298716"}},
            ]
        })

        code, message_ts = _extract_code_with_meta(body)

        self.assertEqual(code, "298716")
        self.assertEqual(message_ts, 1785003263.0)

    def test_xiaohei_request_uses_json_and_cache_buster(self):
        url, structured = _structured_request_url(
            "https://api.xiaoheifk.cn/api/mail-new?refresh_token=TOKEN&response_type=html"
        )

        self.assertTrue(structured)
        self.assertIn("response_type=json", url)
        self.assertIn("_ts=", url)

    @patch("core.generic_api_mail_client.time.sleep")
    @patch("core.generic_api_mail_client.requests.get")
    def test_fetch_accepts_same_code_from_newer_message(self, get, _sleep):
        email = "fresh@example.test"
        client._CONTEXT_CACHE[email] = GenericApiEmailAccount(
            email=email,
            code_url=(
                "https://api.xiaoheifk.cn/api/mail-new?"
                "refresh_token=TOKEN&response_type=html"
            ),
        )
        old = {
            "data": [
                {"date": "2026-07-25T18:10:00Z", "verifyCode": {"code": "298716"}}
            ]
        }
        fresh = {
            "data": [
                {"date": "2026-07-25T18:14:23Z", "verifyCode": {"code": "298716"}}
            ]
        }
        get.side_effect = [
            SimpleNamespace(status_code=200, text=json.dumps(old)),
            SimpleNamespace(status_code=200, text=json.dumps(fresh)),
        ]

        try:
            code = client.fetch_latest_otp(
                email,
                after_ts=1785003200.0,
                max_wait=2,
                poll_interval=1,
                settle_seconds=0,
            )
            self.assertEqual(code, "298716")
            self.assertTrue(client.is_fresh_otp(email, code, 1785003200.0))
            self.assertGreaterEqual(get.call_count, 2)
        finally:
            client._CONTEXT_CACHE.pop(email, None)
            client._LAST_OTP_META.pop(email, None)


if __name__ == "__main__":
    unittest.main()
