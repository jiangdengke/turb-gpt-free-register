import unittest

from webui.app import create_app


class WebuiCodexAgentStatusTests(unittest.TestCase):
    def test_page_distinguishes_registry_not_enabled(self):
        app = create_app(auth_code="test-auth-code")
        client = app.test_client()

        response = client.get("/", headers={"X-Auth-Code": "test-auth-code"})
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("s === 'unsupported'", html)
        self.assertIn(">未开放</span>", html)
        self.assertIn("eligible.map(a => Number(a.id))", html)


if __name__ == "__main__":
    unittest.main()
