import unittest
from types import SimpleNamespace
from unittest.mock import patch

from webui.app import create_app


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    instances = []

    def __init__(self, *, impersonate):
        self.impersonate = impersonate
        self.proxies = {}
        self.closed = False
        self.calls = []
        self.instances.append(self)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == "https://chatgpt.com/":
            return _FakeResponse(403, text="blocked")
        if url == "https://ipinfo.io/json":
            return _FakeResponse(200, {
                "ip": "203.0.113.10",
                "country": "Japan",
                "region": "Tokyo",
                "city": "Tokyo",
                "timezone": "Asia/Tokyo",
                "org": "Example Residential ISP",
            })
        raise AssertionError(url)

    def close(self):
        self.closed = True


class ProxyDiagnosticTests(unittest.TestCase):
    def setUp(self):
        _FakeSession.instances = []

    def test_http_403_still_reports_reachable_and_exit_geo(self):
        with patch("core.proxy_diagnostics.Session", _FakeSession):
            from core.proxy_diagnostics import test_proxy
            result = test_proxy(
                "us.1024proxy.io:3000:user:pass",
                "https://chatgpt.com/",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "reachable")
        self.assertEqual(result["http_status"], 403)
        self.assertFalse(result["target_ok"])
        self.assertEqual(result["exit_ip"], "203.0.113.10")
        self.assertEqual(result["country"], "JP")
        self.assertNotIn("user:pass", str(result))
        self.assertEqual(
            _FakeSession.instances[0].proxies,
            {
                "http": "http://user:pass@us.1024proxy.io:3000",
                "https": "http://user:pass@us.1024proxy.io:3000",
            },
        )

    def test_invalid_target_is_rejected(self):
        with patch("core.proxy_diagnostics.Session", _FakeSession):
            from core.proxy_diagnostics import test_proxy
            with self.assertRaises(ValueError):
                test_proxy("http://user:pass@proxy.example:3000", "file:///tmp/x")


class ProxyDiagnosticRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_route_returns_diagnostic_result(self):
        expected = {"ok": True, "status": "reachable", "exit_ip": "203.0.113.10"}
        with patch("core.proxy_diagnostics.test_proxy", return_value=expected) as check:
            response = self.client.post(
                "/api/proxy/test",
                headers=self.headers,
                json={"proxy": "http://user:pass@proxy.example:3000"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        check.assert_called_once()

    def test_route_requires_webui_authentication(self):
        response = self.client.post("/api/proxy/test", json={})
        self.assertEqual(response.status_code, 401)

    def test_modern_config_contains_proxy_test_control(self):
        response = self.client.get("/?ui=modern", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("btnOpenProxyDiagnosticV2", page)
        self.assertIn("/api/proxy/test", page)
        self.assertIn("出口 IP", page)


if __name__ == "__main__":
    unittest.main()
