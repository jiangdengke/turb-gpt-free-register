# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core.roxy_codex_oauth import (
    _classify_phone_page_failure,
    _prepare_add_phone_submission,
)


class RoxyCodexPhoneTests(unittest.TestCase):
    def test_whatsapp_text_does_not_override_selected_sms_channel(self):
        state = {
            "url": "https://auth.openai.com/add-phone",
            "radios": [
                {"value": "sms", "checked": True},
                {"value": "whatsapp", "checked": False},
            ],
            "bodyText": "Send by SMS or WhatsApp",
        }

        self.assertEqual(_classify_phone_page_failure(state), "")

    def test_selected_whatsapp_channel_is_rejected(self):
        state = {
            "url": "https://auth.openai.com/add-phone",
            "radios": [
                {"value": "sms", "checked": False},
                {"value": "whatsapp", "checked": True},
            ],
            "bodyText": "Send by SMS or WhatsApp",
        }

        self.assertEqual(
            _classify_phone_page_failure(state),
            "whatsapp_channel",
        )

    @patch("core.roxy_codex_oauth._verify_add_phone_value_before_submit")
    @patch("core.roxy_codex_oauth._set_phone_value")
    @patch("core.roxy_codex_oauth._blur_active_input_and_wait")
    @patch("core.roxy_codex_oauth._select_sms_channel_or_raise")
    @patch("core.roxy_codex_oauth._ensure_add_phone_input")
    def test_sms_channel_is_selected_before_phone_is_filled(
        self,
        ensure_input,
        select_sms,
        blur,
        set_phone,
        verify_phone,
    ):
        events = []
        ensure_input.side_effect = lambda *args, **kwargs: events.append("ensure")
        select_sms.side_effect = lambda *args, **kwargs: events.append("select_sms")
        blur.side_effect = lambda *args, **kwargs: events.append(
            f"blur:{kwargs.get('label')}"
        )
        set_phone.side_effect = lambda *args, **kwargs: (
            events.append("set_phone")
            or {
                "e164": "+14752745378",
                "actualVisible": "+1 475 274 5378",
                "hiddenValue": "+14752745378",
                "dialCode": "1",
                "selectedText": "United States (+1)",
                "selectedChanged": False,
            }
        )
        verify_phone.side_effect = lambda *args, **kwargs: (
            events.append("verify_phone")
            or {
                "visibleValue": "+1 475 274 5378",
                "hiddenValue": "+14752745378",
            }
        )

        _prepare_add_phone_submission(object(), "14752745378")

        self.assertLess(events.index("select_sms"), events.index("set_phone"))
        self.assertLess(events.index("set_phone"), events.index("verify_phone"))
        verify_phone.assert_called_once_with(
            unittest.mock.ANY,
            "+14752745378",
        )


if __name__ == "__main__":
    unittest.main()
