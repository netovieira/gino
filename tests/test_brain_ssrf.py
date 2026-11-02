"""SSRF guard around configured provider base URLs."""
from __future__ import annotations

import socket
import unittest
from unittest import mock

from core import brain


class SafeBaseUrlTest(unittest.TestCase):
    def setUp(self):
        brain._SAFE_BASE_CACHE.clear()

    def test_loopback_is_allowed(self):
        self.assertEqual(brain._safe_base_url("http://127.0.0.1:11434/v1"),
                         "http://127.0.0.1:11434/v1")
        self.assertEqual(brain._safe_base_url("http://localhost:3000/v1"),
                         "http://localhost:3000/v1")
        self.assertEqual(brain._safe_base_url("http://[::1]:8011"),
                         "http://[::1]:8011")

    def test_private_and_metadata_are_blocked(self):
        for url in (
            "http://10.0.0.1/v1",
            "http://192.168.15.27/v1",
            "http://172.16.0.10/v1",
            "http://169.254.169.254/latest/meta-data",
            "http://0.0.0.0:8000/v1",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    brain._safe_base_url(url)

    def test_non_http_and_credentials_are_blocked(self):
        for url in (
            "file:///etc/passwd",
            "ftp://example.com/v1",
            "http://user:pass@example.com/v1",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    brain._safe_base_url(url)

    def test_public_host_is_allowed_after_dns(self):
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with mock.patch.object(brain.socket, "getaddrinfo", return_value=fake):
            self.assertEqual(brain._safe_base_url("https://example.com/v1"),
                             "https://example.com/v1")

    def test_probe_rejects_internal_base_before_request(self):
        ok, note = brain._probe({"base_url": "http://10.0.0.1/v1",
                                 "api_key": "", "model": "x"})
        self.assertFalse(ok)
        self.assertIn("base_url recusada", note)

    def test_chat_once_rejects_internal_base(self):
        with self.assertRaises(RuntimeError):
            brain._chat_once([], None,
                             {"provider": "deepseek",
                              "base_url": "http://169.254.169.254/v1"},
                             {})


if __name__ == "__main__":
    unittest.main()
