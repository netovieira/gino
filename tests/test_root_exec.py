"""The root command must never be handed to a shell."""
from __future__ import annotations

import unittest

from actions import root_exec


class RootCommandPolicyTest(unittest.TestCase):
    def test_simple_root_commands_are_accepted(self):
        self.assertEqual(root_exec._command_argv("apt-get install -y adb"),
                         ["apt-get", "install", "-y", "adb"])
        self.assertEqual(root_exec._command_argv("systemctl restart bluetooth"),
                         ["systemctl", "restart", "bluetooth"])

    def test_shell_operators_are_refused(self):
        for command in (
            "apt-get install -y foo && reboot",
            "systemctl restart foo; poweroff",
            "printf x | tee /etc/foo",
            "echo $(id)",
            "apt-get install -y `id`",
            "apt-get install -y foo > /etc/foo",
        ):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    root_exec._command_argv(command)

    def test_other_executables_and_sudo_are_refused(self):
        for command in (
            "sudo apt-get install -y foo",
            "bash -c 'id'",
            "sh -c 'rm -rf /'",
            "python3 -c 'print(1)'",
            "rm -rf /",
            "dd if=/dev/zero of=/dev/sda",
        ):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    root_exec._command_argv(command)

    def test_catastrophic_guard_still_runs(self):
        self.assertTrue(root_exec._looks_catastrophic("rm -rf /"))
        self.assertTrue(root_exec._looks_catastrophic("mkfs.ext4 /dev/sda"))

    def test_root_exec_refuses_before_pending(self):
        root_exec.clear_pending()
        out = root_exec.root_exec({"command": "bash -c 'id'"})
        self.assertIn("não executo", out)
        self.assertFalse(root_exec.has_pending())


if __name__ == "__main__":
    unittest.main()
