# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class ChatGPT2APIConnectionRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_connection_route_returns_diagnostic_result(self):
        expected = {
            "ok": True,
            "status": "connected",
            "http_status": 200,
            "message": "连接成功，chatgpt2api 管理密钥有效",
        }
        with patch(
            "core.chatgpt2api_import_service.test_connection",
            return_value=expected,
        ) as check:
            response = self.client.post(
                "/api/chatgpt2api/test-connection",
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), expected)
        check.assert_called_once_with()

    def test_connection_route_requires_webui_authentication(self):
        response = self.client.post("/api/chatgpt2api/test-connection")
        self.assertEqual(response.status_code, 401)

    def test_modern_config_contains_connection_test_control(self):
        response = self.client.get("/?ui=modern", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("/api/chatgpt2api/test-connection", page)
        self.assertIn("btnTestChatGPT2API", page)
        self.assertIn("不会创建账号、导入账号或修改 Account Service 数据", page)


if __name__ == "__main__":
    unittest.main()
