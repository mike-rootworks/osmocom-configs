"""Offline checks using the real smpplib encoder and a simulated SMPP client."""
import importlib.util
import io
import os
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    "send_sms", Path(__file__).resolve().parents[1] / "scripts/send-sms.py"
)
sms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sms)


class SmsTests(unittest.TestCase):
    def run_sender(self, message="Hello?", password="labpass", failure=None):
        client = MagicMock()
        client.__enter__.return_value = client
        client.send_message.return_value = SimpleNamespace(sequence=7)
        client.read_once.side_effect = failure or (
            lambda **kwargs: client.set_message_sent_handler.call_args.args[0](
                SimpleNamespace(sequence=7)
            )
        )
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"SMPP_PASSWORD": password}, clear=True):
            with patch.object(sms.smpplib.client, "Client", return_value=client) as factory:
                with redirect_stdout(out), redirect_stderr(err):
                    try:
                        sms.main(["123", "100", message])
                        code = 0
                    except SystemExit as error:
                        code = error.code
        return code, out.getvalue(), err.getvalue(), client, factory

    def test_success_waits_for_submit_response(self):
        code, out, _, client, factory = self.run_sender()
        self.assertEqual(code, 0)
        factory.assert_called_once_with("127.0.0.1", 2775, timeout=10, allow_unknown_opt_params=True)
        client.bind_transmitter.assert_called_once_with(system_id="sms-client", password="labpass")
        request = client.send_message.call_args.kwargs
        self.assertEqual(request["destination_addr"], "123")
        self.assertEqual(request["source_addr"], "100")
        self.assertEqual(request["short_message"], b"Hello?")
        client.read_once.assert_called_once_with(auto_send_enquire_link=False)
        self.assertIn("handset delivery is not confirmed", out)

    def test_unicode_uses_ucs2(self):
        code, _, _, client, _ = self.run_sender("Hello \u03a9 \u4f60\u597d")
        self.assertEqual(code, 0)
        self.assertEqual(client.send_message.call_args.kwargs["data_coding"], 8)
        self.assertEqual(client.send_message.call_args.kwargs["short_message"],
                         "Hello \u03a9 \u4f60\u597d".encode("utf-16-be"))

    def test_missing_or_overlength_password_does_not_connect(self):
        for password in ("", "123456789"):
            with self.subTest(password=password):
                code, _, _, _, factory = self.run_sender(password=password)
                self.assertEqual(code, 2)
                factory.assert_not_called()

    def test_multipart_and_empty_text_are_rejected(self):
        for message in ("", "^" * 81, "\u4f60" * 71, "x" * 161):
            with self.subTest(length=len(message)):
                code, _, _, _, factory = self.run_sender(message)
                self.assertEqual(code, 2)
                factory.assert_not_called()

    def test_smpp_rejection_does_not_report_success(self):
        error = sms.smpplib.exceptions.PDUError("Invalid destination", 11)
        code, out, _, client, _ = self.run_sender(failure=error)
        self.assertIn("SMPP error", code)
        self.assertNotIn("SMS accepted", out)
        client.send_message.assert_called_once()
        client.__exit__.assert_called_once()

    def test_timeout_does_not_resubmit(self):
        code, out, _, client, _ = self.run_sender(failure=TimeoutError("timeout"))
        self.assertIn("Check MSC logs before retrying", code)
        self.assertNotIn("SMS accepted", out)
        client.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
