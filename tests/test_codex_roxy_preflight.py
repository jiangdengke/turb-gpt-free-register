import unittest
from unittest.mock import patch

try:
    from core import codex_oauth
except ModuleNotFoundError as exc:  # system Python may omit optional OAuth dependencies
    codex_oauth = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


class CodexRoxyPreflightTests(unittest.TestCase):
    @unittest.skipIf(codex_oauth is None, f"Codex OAuth dependencies unavailable: {_IMPORT_ERROR}")
    def test_roxy_unreachable_stops_before_browser_oauth(self):
        check_result = {
            "ok": False,
            "reachable": False,
            "reason": "unreachable",
            "message": "Roxy API 不可达：http://127.0.0.1:50100；请先启动 RoxyBrowser",
        }
        with patch.object(codex_oauth._cfg, "CODEX_OAUTH_DRIVER", "roxy"), \
                patch.object(codex_oauth._cfg, "CODEX_PROXY_MODE", "pool", create=True), \
                patch("core.roxybrowser_client.RoxyBrowserClient.check_availability", return_value=check_result) as check_mock:
            result = codex_oauth.run_codex_oauth("account@example.com", force=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("先启动 RoxyBrowser", result["message"])
        check_mock.assert_called_once_with()

    @unittest.skipIf(codex_oauth is None, f"Codex OAuth dependencies unavailable: {_IMPORT_ERROR}")
    def test_roxy_available_continues_to_browser_driver(self):
        check_result = {
            "ok": True,
            "reachable": True,
            "reason": "ok",
            "status_code": 200,
            "elapsed_ms": 3,
        }
        with patch.object(codex_oauth._cfg, "CODEX_OAUTH_DRIVER", "roxy"), \
                patch.object(codex_oauth._cfg, "CODEX_PROXY_MODE", "pool", create=True), \
                patch("core.roxy_codex_oauth.run_roxy_codex_oauth", return_value={"ok": True, "status": "success"}) as run_mock, \
                patch("core.roxybrowser_client.RoxyBrowserClient.check_availability", return_value=check_result) as check_mock:
            result = codex_oauth.run_codex_oauth("account@example.com", force=True)

        self.assertEqual(result["status"], "success")
        check_mock.assert_called_once_with()
        run_mock.assert_called_once_with(
            "account@example.com",
            otp_provider=None,
            proxy=None,
            force=True,
        )


if __name__ == "__main__":
    unittest.main()
