# -*- coding: utf-8 -*-
import json
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from config import email as email_config
from core.generic_api_mail_client import (
    GenericApiEmailAccount,
    _mida_messages_api_url,
    _normalize_generic_api_proxy,
    _parse_generic_api_ts,
    _public_inbox_latest_code_url,
    fetch_latest_otp,
)


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.urls = []
        self.proxies = {}
        self.trust_env = True

    def get(self, url, **_kwargs):
        self.urls.append(url)
        return _Response({
            "mailbox": {"address": "inbox-0141-d678@071898.7bcb28.221wx.com"},
            "messages": [{
                "id": "msg_01",
                "receivedAt": "2026-09-06T12:00:00.000Z",
                "subject": "ChatGPT の一時的な認証コード",
                # 该站点日文邮件 verificationCodes 可能为空，但页面 preview 已有验证码。
                "verificationCodes": [],
                "preview": "この一時検証コードを入力して続行してください: 739201 ChatGPT",
                "fromAddress": "service@example.com",
            }],
        })


class _MidaSession(_Session):
    def get(self, url, **_kwargs):
        self.urls.append(url)
        return _Response({
            "ok": True,
            "data": {
                "aliasEmail": "slender-beaks-0y@icloud.com",
                "emails": [{
                    "id": 113421,
                    "receivedAt": "2026-09-25T12:31:07.000Z",
                    "createdAt": "2026-09-25T12:31:14.053Z",
                    "subject": "ChatGPT の一時的な認証コード",
                    "verificationCode": "539948",
                    "textContent": "この一时検証コードを入力して続行してください: 539948",
                    "htmlContent": "",
                    "fromAddress": "noreply@example.com",
                }],
            },
        })


class GenericApiPublicInboxTests(unittest.TestCase):
    def test_mida_share_link_converts_to_messages_api(self):
        url = "https://icloud.mida.vip/messages/slender-beaks-0y%40icloud.com?token=share-token"
        api_url = _mida_messages_api_url(url)
        parsed = urlsplit(api_url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.path, "/api/messages")
        self.assertEqual(query["alias_email"], ["slender-beaks-0y@icloud.com"])
        self.assertEqual(query["token"], ["share-token"])
        self.assertEqual(query["range"], ["latest"])

    def test_mida_messages_api_returns_openai_verification_code(self):
        email = "slender-beaks-0y@icloud.com"
        account = GenericApiEmailAccount(
            email=email,
            code_url="https://icloud.mida.vip/messages/slender-beaks-0y%40icloud.com?token=share-token",
        )
        session = _MidaSession()
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client.requests.Session", return_value=session), \
             patch.object(email_config, "GENERIC_API_PROXY", "direct"), \
             patch("core.generic_api_mail_client._proxy_cfg.pick_proxy") as pick_proxy:
            code = fetch_latest_otp(
                email,
                after_ts=_parse_generic_api_ts("2026-09-25T12:31:10.000Z"),
                max_wait=2,
                poll_interval=0.01,
                settle_seconds=0,
            )

        self.assertEqual(code, "539948")
        self.assertEqual(len(session.urls), 1)
        parsed = urlsplit(session.urls[0])
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/api/messages")
        self.assertEqual(query["alias_email"], [email])
        self.assertEqual(query["token"], ["share-token"])
        self.assertEqual(query["range"], ["latest"])
        self.assertFalse(session.proxies)
        pick_proxy.assert_not_called()

    def test_direct_generic_api_proxy_setting_disables_proxy(self):
        self.assertEqual(_normalize_generic_api_proxy("direct"), "")
        self.assertEqual(_normalize_generic_api_proxy("NONE"), "")
        self.assertEqual(_normalize_generic_api_proxy("http://127.0.0.1:7897"), "http://127.0.0.1:7897")

    def test_public_link_is_converted_to_latest_code_api(self):
        self.assertEqual(
            _public_inbox_latest_code_url("https://mail.knm03.com/i/HTOJyWzFuVXC"),
            "https://mail.knm03.com/api/public/inboxes/HTOJyWzFuVXC/latest-code",
        )

    def test_direct_latest_code_api_is_also_accepted(self):
        url = "https://mail.knm03.com/api/public/inboxes/HTOJyWzFuVXC/latest-code"
        self.assertEqual(_public_inbox_latest_code_url(url), url)

    def test_fetch_latest_otp_uses_public_inbox_api(self):
        email = "inbox-0141-d678@071898.7bcb28.221wx.com"
        account = GenericApiEmailAccount(
            email=email,
            code_url="https://mail.knm03.com/i/HTOJyWzFuVXC",
        )
        session = _Session()
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch("core.generic_api_mail_client.requests.Session", return_value=session):
            code = fetch_latest_otp(
                email,
                after_ts=1788690000,
                max_wait=2,
                poll_interval=0.01,
                settle_seconds=0,
            )
        self.assertEqual(code, "739201")
        self.assertEqual(len(session.urls), 1)
        self.assertTrue(session.urls[0].startswith(
            "https://mail.knm03.com/api/public/inboxes/HTOJyWzFuVXC?"
        ))
        self.assertFalse(session.trust_env)

    def test_fetch_latest_otp_applies_proxy_pool_route(self):
        email = "inbox-0141-d678@071898.7bcb28.221wx.com"
        account = GenericApiEmailAccount(email=email, code_url="https://mail.knm03.com/i/token")
        session = _Session()
        proxy = "socks5://user:secret@127.0.0.1:7897"
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch.object(email_config, "GENERIC_API_PROXY", ""), \
             patch("core.generic_api_mail_client._proxy_cfg.pick_proxy", return_value=proxy), \
             patch("core.generic_api_mail_client.requests.Session", return_value=session):
            code = fetch_latest_otp(email, max_wait=2, poll_interval=0.01, settle_seconds=0)
        self.assertEqual(code, "739201")
        self.assertEqual(session.proxies, {"http": proxy, "https": proxy})

    def test_proxy_connection_error_falls_back_to_direct(self):
        email = "inbox-0141-d678@071898.7bcb28.221wx.com"
        account = GenericApiEmailAccount(email=email, code_url="https://mail.knm03.com/i/token")
        proxy_session = _Session()
        direct_session = _Session()

        def proxy_failure(_url, **_kwargs):
            import requests
            raise requests.ConnectionError("proxy reset")

        proxy_session.get = proxy_failure
        with patch("core.generic_api_mail_client.get_account_context", return_value=account), \
             patch.object(email_config, "GENERIC_API_PROXY", ""), \
             patch("core.generic_api_mail_client._proxy_cfg.pick_proxy", return_value="socks5://127.0.0.1:7897"), \
             patch("core.generic_api_mail_client.requests.Session", side_effect=[proxy_session, direct_session]):
            code = fetch_latest_otp(email, max_wait=2, poll_interval=0.01, settle_seconds=0)
        self.assertEqual(code, "739201")
        self.assertEqual(proxy_session.proxies["https"], "socks5://127.0.0.1:7897")
        self.assertEqual(direct_session.proxies, {})
        self.assertFalse(direct_session.trust_env)


if __name__ == "__main__":
    unittest.main()
