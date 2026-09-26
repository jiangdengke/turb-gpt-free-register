# -*- coding: utf-8 -*-
import importlib
import os
import unittest
from unittest.mock import patch

from config import env_loader
from webui import config_editor


class CodexSmsApiBaseTests(unittest.TestCase):
    def test_sms_api_base_reads_environment_override(self):
        from config import codex

        old_loaded = env_loader._LOADED
        try:
            env_loader._LOADED = True
            with patch.dict(
                os.environ,
                {"SMS_API_BASE": "https://hero-sms.example/stubs/handler_api.php"},
                clear=False,
            ):
                importlib.reload(codex)
                self.assertEqual(
                    codex.SMS_API_BASE,
                    "https://hero-sms.example/stubs/handler_api.php",
                )
        finally:
            env_loader._LOADED = old_loaded
            importlib.reload(codex)

    def test_codex_proxy_mode_reads_environment_override(self):
        from config import codex

        old_loaded = env_loader._LOADED
        try:
            env_loader._LOADED = True
            with patch.dict(os.environ, {"CODEX_PROXY_MODE": "direct"}, clear=False):
                importlib.reload(codex)
                self.assertEqual(codex.CODEX_PROXY_MODE, "direct")
        finally:
            env_loader._LOADED = old_loaded
            importlib.reload(codex)

    def test_codex_direct_proxy_mode_resolves_without_global_proxy_change(self):
        from config import codex

        try:
            from core import codex_oauth
        except ModuleNotFoundError as exc:
            if exc.name in {"pyotp", "curl_cffi"}:
                self.skipTest(f"optional Codex dependency unavailable: {exc.name}")
            raise

        with patch.object(codex, "CODEX_PROXY_MODE", "direct", create=True):
            self.assertEqual(codex_oauth._resolve_codex_proxy(None), "")
        with patch.object(codex, "CODEX_PROXY_MODE", "pool", create=True):
            self.assertIsNone(codex_oauth._resolve_codex_proxy(None))
        self.assertEqual(codex_oauth._resolve_codex_proxy("http://proxy.example:1"), "http://proxy.example:1")

    def test_webui_exposes_codex_proxy_mode_field(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields["CODEX_PROXY_MODE"]["file"], "codex.py")
        self.assertEqual(fields["CODEX_PROXY_MODE"]["choices"][1]["value"], "direct")

    def test_webui_exposes_sms_api_base_field(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields["SMS_API_BASE"]["file"], "codex.py")
        self.assertEqual(fields["SMS_API_BASE"]["type"], "str")


if __name__ == "__main__":
    unittest.main()
