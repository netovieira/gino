"""Detector de OTP: códigos perto de palavras-chave, com remetente/app."""
from __future__ import annotations

import unittest

from core import otp_detect


class CodePatternTests(unittest.TestCase):
    def test_portuguese_code_with_accent(self):
        otp = otp_detect.detect("Seu código é 123456")
        self.assertIsNotNone(otp)
        self.assertEqual(otp["code"], "123456")

    def test_unaccented_portuguese(self):
        otp = otp_detect.detect("seu codigo de verificacao: 482913")
        self.assertEqual(otp["code"], "482913")

    def test_english_verification_code(self):
        otp = otp_detect.detect("Your verification code is 482913")
        self.assertEqual(otp["code"], "482913")

    def test_code_before_the_keyword(self):
        otp = otp_detect.detect("482913 is your Google verification code")
        self.assertEqual(otp["code"], "482913")

    def test_four_digit_code(self):
        otp = otp_detect.detect("Código: 1234")
        self.assertEqual(otp["code"], "1234")

    def test_spaced_digits_are_one_code(self):
        otp = otp_detect.detect("seu código: 482 913")
        self.assertEqual(otp["code"], "482913")

    def test_hyphenated_digits_are_one_code(self):
        otp = otp_detect.detect("seu código é 123-456")
        self.assertEqual(otp["code"], "123456")

    def test_alphanumeric_code(self):
        otp = otp_detect.detect("Use o código A1B2C3 para entrar")
        self.assertEqual(otp["code"], "A1B2C3")

    def test_prefixed_alphanumeric_code_keeps_the_letter(self):
        otp = otp_detect.detect("Use o código G-483920 para entrar")
        self.assertEqual(otp["code"], "G483920")

    def test_code_after_expiry_sentence(self):
        otp = otp_detect.detect("Seu código expira em 10 minutos: 123456")
        self.assertEqual(otp["code"], "123456")

    def test_login_context(self):
        otp = otp_detect.detect("123456 é o seu código de login")
        self.assertEqual(otp["code"], "123456")

    def test_non_string_input_is_safe(self):
        self.assertIsNone(otp_detect.detect(None))
        self.assertIsNone(otp_detect.detect(123))


class FalsePositiveTests(unittest.TestCase):
    def test_number_without_a_keyword_is_not_a_code(self):
        for text in ("seu pedido 123456 foi enviado",
                     "o telefone é 98765432",
                     "reunião às 15h no escritório",
                     "a conta vence em 2026"):
            self.assertIsNone(otp_detect.detect(text), text)

    def test_tracking_and_order_codes_are_rejected(self):
        for text in ("código do pedido: 12345",
                     "código de rastreio: 123456",
                     "código de barras 12345678"):
            self.assertIsNone(otp_detect.detect(text), text)

    def test_too_long_numbers_are_not_codes(self):
        self.assertIsNone(otp_detect.detect("código 123456789012"))

    def test_a_date_near_the_keyword_is_not_a_code(self):
        self.assertIsNone(otp_detect.detect("código válido em 2026-10"))

    def test_phone_number_is_not_a_code(self):
        self.assertIsNone(otp_detect.detect(
            "código enviado para seu telefone 98765-4321"))

    def test_copyright_year_is_not_a_code(self):
        self.assertIsNone(otp_detect.detect(
            "© 2026 Google", subject="Código de verificação do Google"))


class AppExtractionTests(unittest.TestCase):
    def test_app_from_portuguese_subject(self):
        otp = otp_detect.detect(
            "Seu código é 123-456",
            subject="Código de verificação do WhatsApp",
            sender="WhatsApp <no-reply@whatsapp.com>")
        self.assertEqual(otp["app"], "WhatsApp")

    def test_app_from_message_text(self):
        otp = otp_detect.detect(
            "Use o código 837261 para entrar no Instagram")
        self.assertEqual(otp["app"], "Instagram")

    def test_app_from_english_subject(self):
        otp = otp_detect.detect(
            "Your code is 123456", subject="Google verification code",
            sender='"Google" <no-reply@accounts.google.com>')
        self.assertEqual(otp["app"], "Google")

    def test_app_from_sender_display_name(self):
        otp = otp_detect.detect(
            "Your code is 123456", sender='"Google" <no-reply@accounts.google.com>')
        self.assertEqual(otp["app"], "Google")

    def test_app_from_sender_domain(self):
        otp = otp_detect.detect(
            "Your code is 123456", sender="no-reply@accounts.google.com")
        self.assertEqual(otp["app"], "Google")

    def test_automated_display_name_falls_back_to_the_domain(self):
        otp = otp_detect.detect(
            "Your code is 123456",
            sender='"no-reply" <no-reply@accounts.google.com>')
        self.assertEqual(otp["app"], "Google")

    def test_forward_prefix_is_not_an_app(self):
        otp = otp_detect.detect(
            "Seu código é 123456",
            subject="Fwd: Código de verificação do Google")
        self.assertEqual(otp["app"], "Google")

    def test_a_persons_whatsapp_name_is_not_an_app(self):
        otp = otp_detect.detect("Seu código é 123456", sender="Vida")
        self.assertEqual(otp["app"], "")

    def test_sender_is_kept_verbatim(self):
        otp = otp_detect.detect("Seu código é 123456", sender="Vida")
        self.assertEqual(otp["sender"], "Vida")


if __name__ == "__main__":
    unittest.main()
