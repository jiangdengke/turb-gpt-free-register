import unittest
from unittest.mock import patch

from core import extract_link_service
from webui.app import create_app


class ExtractLinkConfigTests(unittest.TestCase):
    def test_validate_settings_reports_all_missing_values(self):
        with patch("core.extract_link_service._runtime_setting", return_value=""):
            with self.assertRaisesRegex(
                ValueError,
                "EXTRACT_LINK_API_BASE、EXTRACT_LINK_CDK",
            ):
                extract_link_service.validate_settings()

    @patch(
        "webui.app.extract_link_service.validate_settings",
        side_effect=ValueError(
            "提链配置缺失：EXTRACT_LINK_API_BASE、EXTRACT_LINK_CDK"
        ),
    )
    def test_bulk_api_returns_configuration_error(self, _validate):
        app = create_app(auth_code="test-auth-code")
        client = app.test_client()

        response = client.post(
            "/api/accounts/extract-link-bulk",
            headers={"X-Auth-Code": "test-auth-code"},
            json={"account_ids": [5]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("EXTRACT_LINK_API_BASE", response.get_json()["error"])
        self.assertIn("EXTRACT_LINK_CDK", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
