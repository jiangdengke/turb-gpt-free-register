# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import register as register_config
from core import account_export
from core import db
from core import chatgpt_password_service as password_service


class RegistrationPasswordAutoTests(unittest.TestCase):
    def test_generated_password_has_required_character_classes(self):
        password = password_service.generate_strong_password()
        self.assertGreaterEqual(len(password), 12)
        self.assertTrue(any(char.islower() for char in password))
        self.assertTrue(any(char.isupper() for char in password))
        self.assertTrue(any(char.isdigit() for char in password))
        self.assertTrue(any(not char.isalnum() for char in password))
        self.assertNotEqual(password, password_service.generate_strong_password())

    def test_password_waits_for_automatic_twofa_completion(self):
        callback_holder = {}
        with patch.object(register_config, "AUTO_CHATGPT_PASSWORD_AFTER_REGISTER", True), \
             patch.object(password_service, "generate_strong_password", return_value="ValidPassword9!"), \
             patch.object(
                 password_service,
                 "enqueue_account_chatgpt_password",
                 return_value={"accepted": True, "busy": False},
             ) as password_enqueue, \
             patch(
                 "core.twofa_service.enqueue_account_totp_setup",
                 side_effect=lambda **kwargs: (
                     callback_holder.update(on_complete=kwargs.get("on_complete"))
                     or {"accepted": True, "busy": False}
                 ),
             ) as twofa_enqueue, \
             patch.object(
                 db,
                 "get_account",
                 return_value={"id": 7, "access_token": "fresh-access-token"},
             ):
            account_export._queue_registration_chatgpt_password(
                account_id=7,
                email="user@example.com",
                access_token="initial-access-token",
                email_source="generic_api",
                proxy_used=None,
                extra={},
                auto_twofa=True,
                totp_secret=None,
            )

            twofa_enqueue.assert_called_once()
            self.assertFalse(password_enqueue.called)
            self.assertIsNotNone(callback_holder["on_complete"])

            callback_holder["on_complete"]({"status": "success", "ok": True})

        password_enqueue.assert_called_once()
        self.assertEqual(password_enqueue.call_args.kwargs["access_token"], "fresh-access-token")
        self.assertEqual(password_enqueue.call_args.kwargs["password"], "ValidPassword9!")
        self.assertEqual(password_enqueue.call_args.kwargs["trigger"], "registration_auto")

    def test_existing_registration_password_skips_password_task_but_keeps_twofa(self):
        with patch.object(register_config, "AUTO_CHATGPT_PASSWORD_AFTER_REGISTER", True), \
             patch.object(password_service, "enqueue_account_chatgpt_password") as password_enqueue, \
             patch(
                 "core.twofa_service.enqueue_account_totp_setup",
                 return_value={"accepted": True, "busy": False},
             ) as twofa_enqueue:
            account_export._queue_registration_chatgpt_password(
                account_id=8,
                email="user@example.com",
                access_token="access-token",
                email_source="generic_api",
                proxy_used=None,
                extra={"registration_password": "ExistingPassword9!"},
                auto_twofa=True,
                totp_secret=None,
            )

        twofa_enqueue.assert_called_once()
        self.assertIsNone(twofa_enqueue.call_args.kwargs["on_complete"])
        password_enqueue.assert_not_called()

    def test_password_is_queued_directly_when_twofa_is_already_complete(self):
        with patch.object(register_config, "AUTO_CHATGPT_PASSWORD_AFTER_REGISTER", True), \
             patch.object(password_service, "generate_strong_password", return_value="ValidPassword9!"), \
             patch.object(
                 password_service,
                 "enqueue_account_chatgpt_password",
                 return_value={"accepted": True, "busy": False},
             ) as password_enqueue, \
             patch("core.twofa_service.enqueue_account_totp_setup") as twofa_enqueue, \
             patch.object(db, "get_account", return_value={"access_token": "access-token"}):
            account_export._queue_registration_chatgpt_password(
                account_id=9,
                email="user@example.com",
                access_token="access-token",
                email_source="generic_api",
                proxy_used=None,
                extra={},
                auto_twofa=True,
                totp_secret="TOTPSECRET",
            )

        twofa_enqueue.assert_not_called()
        password_enqueue.assert_called_once()

    def test_save_account_data_calls_post_registration_password_hook(self):
        with patch.object(register_config, "AUTO_CHATGPT_PASSWORD_AFTER_REGISTER", True), \
             patch("config.twofa.ENABLE_2FA", False), \
             patch("core.db.insert_account", return_value=11) as insert_account, \
             patch.object(account_export, "_append_batch_archive"), \
             patch.object(account_export, "_queue_registration_chatgpt_password") as password_hook, \
             patch(
                 "core.chatgpt2api_import_service.enqueue_registered_account",
                 return_value={"status": "skipped"},
             ):
            account_id = account_export.save_account_data(
                email="user@example.com",
                access_token="access-token",
                email_source="generic_api",
                extra={},
                auto_plan_check=False,
            )

        self.assertEqual(account_id, 11)
        insert_account.assert_called_once()
        password_hook.assert_called_once_with(
            account_id=11,
            email="user@example.com",
            access_token="access-token",
            email_source="generic_api",
            proxy_used=None,
            extra={},
            auto_twofa=False,
            totp_secret=None,
        )


if __name__ == "__main__":
    unittest.main()
