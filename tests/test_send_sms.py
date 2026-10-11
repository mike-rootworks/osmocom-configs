"""Exercise SMS validation and fragmented VTY responses without a live MSC."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/send-sms.py"
SPEC = importlib.util.spec_from_file_location("send_sms", SCRIPT)
sms = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sms)


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = deque(chunks)
        self.sent = []

    def recv(self, size):
        chunk = self.chunks.popleft() if self.chunks else b""
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MessageValidationTests(unittest.TestCase):
    def test_gsm_extension_characters_use_two_septets(self):
        self.assertEqual(sms.sms_text("^" * 80), "^" * 80)
        with self.assertRaises(argparse.ArgumentTypeError):
            sms.sms_text("^" * 81)
        self.assertEqual(sms.sms_text("a" * 160), "a" * 160)
        with self.assertRaises(argparse.ArgumentTypeError):
            sms.sms_text("a" * 161)

    def test_unsafe_or_unrepresentable_text_is_rejected(self):
        for value in ("", "  ", "Hello\nshutdown", "Hi\rshutdown", "Tab\there",
                      "Emoji \U0001f600", "Hello?", "`code`", "\x1b[31mtext"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                sms.sms_text(value)

    def test_spaces_are_normalized_and_percent_is_literal(self):
        self.assertEqual(sms.sms_text("  Battery   50%  "), "Battery 50%")

    def test_numbers_preserve_leading_zeroes_and_reject_injection(self):
        self.assertEqual(sms.number("+00123"), "00123")
        for value in ("", "12 34", "1\nshutdown", "abc", "9" * 16, "\u0661\u0662"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                sms.number(value)

    def test_invalid_timeouts_and_ports_are_rejected(self):
        for value in ("nan", "inf", "0", "-1"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                sms.timeout_seconds(value)
        for value in ("0", "65536", "abc"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                sms.port_number(value)


class VtyProtocolTests(unittest.TestCase):
    def run_cli(self, connection, arguments=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sms.socket, "create_connection", return_value=connection) as connect:
            with redirect_stdout(out), redirect_stderr(err):
                result = sms.main(arguments or ["--to", "123", "--message", "Hello"])
        return result, out.getvalue(), err.getvalue(), connect

    def test_fragmented_telnet_negotiation_and_command_echo(self):
        connection = FakeSocket([
            b"\xff", b"\xfb\x01\xff\xfd", b"\x1f\xff\xfa\x1f\x00\x50\xff",
            b"\xf0Welcome\r\nOsmo", b"MSC> ",
            b"enable\r\nOsmoMSC", b"# ",
            b"subscriber msisdn 123 sms sender msisdn 100 send Battery 50%\r\n",
            b"OsmoMSC# ",
        ])
        result, out, err, connect = self.run_cli(
            connection, ["--to", "123", "--message", "Battery 50%"]
        )
        self.assertEqual(result, 0, err)
        self.assertIn("Storage and handset delivery are not confirmed", out)
        self.assertIn(b"\xff\xfd\x01", connection.sent)  # DO ECHO
        self.assertIn(b"\xff\xfc\x1f", connection.sent)  # WONT NAWS
        self.assertIn(b"enable\r", connection.sent)
        self.assertEqual(connection.sent[-1],
                         b"subscriber msisdn 123 sms sender msisdn 100 send Battery 50%\r")
        connect.assert_called_once_with(("127.0.0.1", 4254), timeout=10.0)

    def test_unknown_recipient_is_an_error(self):
        connection = FakeSocket([
            b"OsmoMSC# ",
            b"subscriber msisdn 123 sms sender msisdn 100 send Hello\r\n"
            b"% No subscriber found for msisdn 123\r\nOsmoMSC# ",
        ])
        result, out, err, _ = self.run_cli(connection)
        self.assertEqual(result, 1)
        self.assertIn("No subscriber found", err)
        self.assertNotIn("SMS command submitted", out)

    def test_authenticated_vty_fails_before_sms(self):
        for greeting in (b"Password: ", b"Username: "):
            with self.subTest(greeting=greeting):
                connection = FakeSocket([greeting])
                result, _, err, _ = self.run_cli(connection)
                self.assertEqual(result, 1)
                self.assertIn("authentication", err)
                self.assertEqual(connection.sent, [])

    def test_enable_password_fails_before_sms(self):
        connection = FakeSocket([b"OsmoMSC> ", b"Password: "])
        result, _, err, _ = self.run_cli(connection)
        self.assertEqual(result, 1)
        self.assertIn("authentication", err)
        self.assertEqual(connection.sent, [b"enable\r"])

    def test_timeout_after_sms_does_not_retry(self):
        connection = FakeSocket([b"OsmoMSC# ", TimeoutError("timeout")])
        result, _, err, connect = self.run_cli(connection)
        self.assertEqual(result, 1)
        self.assertIn("Submission outcome is unknown", err)
        self.assertEqual(len(connection.sent), 1)
        connect.assert_called_once()

    def test_disconnection_after_sms_reports_uncertain_outcome(self):
        connection = FakeSocket([b"OsmoMSC# ", b""])
        result, _, err, _ = self.run_cli(connection)
        self.assertEqual(result, 1)
        self.assertIn("Submission outcome is unknown", err)

    def test_imsi_target_and_custom_endpoint(self):
        connection = FakeSocket([b"OsmoMSC# ", b"\r\nOsmoMSC# "])
        result, _, err, connect = self.run_cli(connection, [
            "--imsi", "001010123456789", "--sender", "+100", "--message", "Hello",
            "--host", "localhost", "--port", "14254", "--timeout", "2",
        ])
        self.assertEqual(result, 0, err)
        self.assertEqual(connection.sent, [
            b"subscriber imsi 001010123456789 sms sender msisdn 100 send Hello\r"
        ])
        connect.assert_called_once_with(("localhost", 14254), timeout=2.0)

    def test_dry_run_never_connects(self):
        result, out, err, connect = self.run_cli(
            FakeSocket([]), ["--to", "123", "--message", "Hello", "--dry-run"]
        )
        self.assertEqual(result, 0, err)
        self.assertIn("subscriber msisdn 123", out)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
