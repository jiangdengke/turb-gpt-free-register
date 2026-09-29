# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

import main


class ProtocolPreflightSessionRotationTests(unittest.TestCase):
    def test_retryable_preflight_rebuilds_session_before_otp(self):
        first = object()
        second = object()

        with patch.object(main, "network_preflight", side_effect=[RuntimeError("HTTP Error 403"), None]) as preflight, \
             patch.object(main, "BrowserSession", return_value=second) as create_session, \
             patch.object(main, "close_browser_session") as close_session:
            result = main._network_preflight_with_session_rotation(first, None)

        self.assertIs(result, second)
        self.assertEqual(preflight.call_count, 2)
        create_session.assert_called_once_with(proxy=None)
        close_session.assert_called_once_with(first)

    def test_csrf_403_restarts_the_complete_pre_otp_chain(self):
        first = object()
        second = object()

        with patch.object(main._protocol_cfg, "CHATGPT_ANON_BOOTSTRAP_ENABLED", False), \
             patch.object(main, "network_preflight", side_effect=[None, None]) as preflight, \
             patch.object(main, "get_providers", return_value={}) as providers, \
             patch.object(main, "get_csrf_token", side_effect=[RuntimeError("HTTP Error 403"), "csrf-token"]) as csrf, \
             patch.object(main, "signin_openai", return_value="https://auth.openai.com/authorize") as signin, \
             patch.object(main, "BrowserSession", return_value=second) as create_session, \
             patch.object(main, "close_browser_session") as close_session, \
             patch.object(main, "human_delay"):
            result_session, authorize_url = main._start_chatgpt_auth_with_session_rotation(
                first,
                None,
                "user@example.com",
            )

        self.assertIs(result_session, second)
        self.assertEqual(authorize_url, "https://auth.openai.com/authorize")
        self.assertEqual(preflight.call_count, 2)
        self.assertEqual(providers.call_count, 2)
        self.assertEqual(csrf.call_count, 2)
        signin.assert_called_once_with(second, "csrf-token", "user@example.com")
        create_session.assert_called_once_with(proxy=None)
        close_session.assert_called_once_with(first)

    def test_authorize_403_restarts_the_complete_pre_otp_chain(self):
        first = object()
        second = object()

        with patch.object(
            main,
            "_start_chatgpt_auth_with_session_rotation",
            side_effect=[(first, "authorize-1"), (second, "authorize-2")],
        ) as start, \
             patch.object(main, "follow_authorize", side_effect=[RuntimeError("HTTP Error 403"), "email-verification"]) as follow, \
             patch.object(main, "BrowserSession", return_value=second) as create_session, \
             patch.object(main, "close_browser_session") as close_session:
            result_session, final_url = main._run_pre_otp_authorization_with_session_rotation(
                first,
                None,
                "user@example.com",
            )

        self.assertIs(result_session, second)
        self.assertEqual(final_url, "email-verification")
        self.assertEqual(start.call_count, 2)
        self.assertEqual(start.call_args_list[0].args, (first, None, "user@example.com"))
        self.assertEqual(start.call_args_list[1].args, (second, None, "user@example.com"))
        self.assertEqual(start.call_args_list[0].kwargs, {"max_attempts": 1})
        self.assertEqual(start.call_args_list[1].kwargs, {"max_attempts": 1})
        self.assertEqual(follow.call_args_list[0].args, (first, "authorize-1"))
        self.assertEqual(follow.call_args_list[1].args, (second, "authorize-2"))
        create_session.assert_called_once_with(proxy=None)
        close_session.assert_called_once_with(first)

    def test_proxy_failure_can_fall_back_to_direct_before_otp(self):
        first = object()
        direct = object()
        final_url = "https://auth.openai.com/email-verification"

        with patch.object(
            main._protocol_cfg,
            "OPENAI_DIRECT_FALLBACK_ON_PROXY_FAILURE",
            True,
        ), patch.object(
            main,
            "_run_pre_otp_authorization_with_session_rotation",
            side_effect=[RuntimeError("HTTP Error 403"), (direct, final_url)],
        ) as authorize, patch.object(
            main,
            "BrowserSession",
            return_value=object(),
        ) as create_session, patch.object(main, "close_browser_session") as close_session:
            result = main._run_pre_otp_authorization_with_direct_fallback(
                first,
                None,
                "user@example.com",
            )

        self.assertEqual(result, (direct, final_url))
        self.assertEqual(authorize.call_args_list[0].args, (first, None, "user@example.com"))
        self.assertEqual(authorize.call_args_list[1].args[1:], ("", "user@example.com"))
        create_session.assert_called_once_with(proxy="")
        close_session.assert_called_once_with(first)

    def test_explicit_proxy_does_not_fall_back_to_direct(self):
        first = object()
        with patch.object(
            main._protocol_cfg,
            "OPENAI_DIRECT_FALLBACK_ON_PROXY_FAILURE",
            True,
        ), patch.object(
            main,
            "_run_pre_otp_authorization_with_session_rotation",
            side_effect=RuntimeError("HTTP Error 403"),
        ) as authorize, patch.object(main, "BrowserSession") as create_session:
            with self.assertRaisesRegex(RuntimeError, "HTTP Error 403"):
                main._run_pre_otp_authorization_with_direct_fallback(
                    first,
                    "http://proxy.example:8080",
                    "user@example.com",
                )

        authorize.assert_called_once_with(first, "http://proxy.example:8080", "user@example.com")
        create_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
