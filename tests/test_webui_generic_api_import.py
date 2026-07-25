# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class GenericApiImportWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.import_generic_api_emails", return_value=(1, 0))
    def test_imports_two_field_generic_api_format(self, importer):
        response = self.client.post("/api/outlook/import", json={
            "source": "generic_api",
            "text": "user@example.com----https://mail.example.test/code",
        })

        self.assertEqual(response.status_code, 200)
        importer.assert_called_once_with([{
            "email": "user@example.com",
            "code_url": "https://mail.example.test/code",
            "access_token": "",
            "totp_secret": "",
        }])

    @patch("webui.app.db.import_generic_api_emails", return_value=(1, 0))
    def test_imports_vendor_email_password_url_format(self, importer):
        response = self.client.post("/api/outlook/import", json={
            "source": "generic_api",
            "text": "user@example.com----mail-password----https://mail.example.test/code",
        })

        self.assertEqual(response.status_code, 200)
        importer.assert_called_once_with([{
            "email": "user@example.com",
            "code_url": "https://mail.example.test/code",
            "access_token": "",
            "totp_secret": "",
        }])

    @patch("webui.app.db.import_outlook_accounts")
    def test_outlook_selection_explains_generic_api_format(self, importer):
        response = self.client.post("/api/outlook/import", json={
            "source": "outlook",
            "text": "user@example.com----mail-password----https://mail.example.test/code",
        })

        self.assertEqual(response.status_code, 400)
        self.assertIn("通用 API 取码邮箱", response.get_json()["error"])
        importer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
