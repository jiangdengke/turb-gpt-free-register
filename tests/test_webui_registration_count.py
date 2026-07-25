# -*- coding: utf-8 -*-
import unittest

from webui.app import create_app


class RegistrationCountWebUiTests(unittest.TestCase):
    def test_registration_count_defaults_to_available_pool_until_edited(self):
        client = create_app(auth_code="test-auth").test_client()
        response = client.get("/", headers={"X-Auth-Code": "test-auth"})
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("let regCountFollowsPool = true", html)
        self.assertIn("syncRegCountToPool(s.outlook_available)", html)
        self.assertIn("Math.min(200, poolCount)", html)
        self.assertIn("regCountFollowsPool = false", html)

    def test_email_pool_can_filter_registered_and_unregistered_addresses(self):
        client = create_app(auth_code="test-auth").test_client()
        response = client.get("/", headers={"X-Auth-Code": "test-auth"})
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="poolRegistrationFilter"', html)
        self.assertIn('value="unregistered"', html)
        self.assertIn("function filteredOutlookRows()", html)
        self.assertIn("未注册 (${unregistered})", html)


if __name__ == "__main__":
    unittest.main()
