# -*- coding: utf-8 -*-
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from config import codex as codex_config
from config import env_loader
from core import sms_provider
from webui import config_editor


class _Resp:
    status_code = 200

    def __init__(self, text):
        self.text = text


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params or {}})
        return _Resp(self.responses.pop(0))

    def close(self):
        self.closed = True


class SmsActivateProviderTests(unittest.TestCase):
    def _config(self):
        stack = ExitStack()
        values = {
            "SMS_PROVIDER": "sms_activate",
            "SMS_ACTIVATE_API_BASE": "https://sms-verification-number.com/stubs/handler_api",
            "SMS_ACTIVATE_API_KEY": "test-key",
            "SMS_ACTIVATE_LANG": "en",
            "SMS_ACTIVATE_SERVICE": "dr",
            "SMS_ACTIVATE_COUNTRY": "187",
            "SMS_SERVICE": "wrong-generic-service",
            "SMS_COUNTRY": "999",
            "SMS_MAX_PRICE": "",
        }
        for key, value in values.items():
            stack.enter_context(patch.object(codex_config, key, value))
        return stack

    def test_secret_registry_and_webui_fields_include_sms_activate(self):
        self.assertIn("SMS_ACTIVATE_API_KEY", env_loader.SECRET_ENV_KEYS)
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertIn("SMS_ACTIVATE_API_BASE", fields)
        self.assertIn("SMS_ACTIVATE_LANG", fields)
        self.assertIn("SMS_ACTIVATE_SERVICE", fields)
        self.assertIn("SMS_ACTIVATE_COUNTRY", fields)
        self.assertTrue(fields["SMS_ACTIVATE_API_KEY"].get("secret"))

    def test_acquire_number_adds_required_lang(self):
        http = _Http(["ACCESS_NUMBER:4100:+12025550123"])
        with self._config():
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual(activation_id, "4100")
        self.assertEqual(phone, "12025550123")
        self.assertEqual(
            http.calls[0]["url"],
            "https://sms-verification-number.com/stubs/handler_api",
        )
        self.assertEqual(
            http.calls[0]["params"],
            {
                "api_key": "test-key",
                "lang": "en",
                "action": "getNumber",
                "service": "dr",
                "country": "187",
            },
        )

    def test_wait_for_sms_code_and_complete(self):
        http = _Http(["STATUS_OK:654321", "ACCESS_ACTIVATION"])
        with self._config():
            code = sms_provider.wait_for_sms_code(
                "4100", http=http, max_wait=1, poll_interval=0
            )
            sms_provider.complete("4100", http=http)

        self.assertEqual(code, "654321")
        self.assertEqual(http.calls[0]["params"]["action"], "getStatus")
        self.assertEqual(http.calls[1]["params"]["action"], "setStatus")
        self.assertEqual(http.calls[1]["params"]["status"], "6")
        self.assertEqual(http.calls[1]["params"]["lang"], "en")

    def test_missing_api_key_is_explicit(self):
        http = _Http([])
        with patch.object(codex_config, "SMS_PROVIDER", "svnumber"), patch.object(
            codex_config, "SMS_ACTIVATE_API_KEY", ""
        ):
            with self.assertRaisesRegex(
                sms_provider.SmsProviderError, "SMS_ACTIVATE_API_KEY"
            ):
                sms_provider.acquire_number(http=http)


if __name__ == "__main__":
    unittest.main()
