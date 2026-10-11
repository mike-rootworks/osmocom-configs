"""Check the basic sender offline with smpplib's real text encoder."""
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import smpplib.client

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/send-sms.py"


class SmsTests(unittest.TestCase):
    def run_sender(self, message="Hello?", reply=None, failure=None):
        client = MagicMock()
        client.__enter__.return_value = client
        client.send_message.return_value = SimpleNamespace(sequence=7)
        client.read_pdu.return_value = reply or SimpleNamespace(
            command="submit_sm_resp", sequence=7, status=0
        )
        client.read_pdu.side_effect = failure
        out, err = io.StringIO(), io.StringIO()
        arguments = [str(SCRIPT), "123", "100", "10.0.0.251", message]
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(smpplib.client, "Client", return_value=client) as factory:
                with patch("sys.argv", arguments), redirect_stdout(out), redirect_stderr(err):
                    try:
                        runpy.run_path(str(SCRIPT), run_name="__main__")
                        code = 0
                    except SystemExit as error:
                        code = error.code
        return code, out.getvalue(), client, factory

    def test_argument_order_and_acceptance(self):
        code, out, client, factory = self.run_sender()
        self.assertEqual(code, 0)
        factory.assert_called_once_with("10.0.0.251", 2775, timeout=10, allow_unknown_opt_params=True)
        client.bind_transmitter.assert_called_once_with(system_id="sms-client", password="")
        request = client.send_message.call_args.kwargs
        self.assertEqual(request["destination_addr"], "123")
        self.assertEqual(request["source_addr"], "100")
        self.assertEqual(request["short_message"], b"Hello?")
        client.read_pdu.assert_called_once()
        self.assertIn("Handset delivery is not confirmed", out)

    def test_unicode(self):
        code, _, client, _ = self.run_sender("Hello \u4f60\u597d")
        self.assertEqual(code, 0)
        self.assertEqual(client.send_message.call_args.kwargs["data_coding"], 8)
        self.assertEqual(client.send_message.call_args.kwargs["short_message"],
                         "Hello \u4f60\u597d".encode("utf-16-be"))

    def test_rejects_empty_and_multipart_text(self):
        for message in ("", "^" * 81, "\u4f60" * 71):
            code, _, _, factory = self.run_sender(message)
            self.assertIn("one SMS", code)
            factory.assert_not_called()

    def test_rejected_sms_is_not_reported_as_accepted(self):
        reply = SimpleNamespace(command="submit_sm_resp", sequence=7, status=11)
        code, out, client, _ = self.run_sender(reply=reply)
        self.assertIn("status 11", code)
        self.assertNotIn("SMS accepted", out)
        client.__exit__.assert_called_once()

    def test_wrong_response_or_sequence_is_not_acceptance(self):
        for command, sequence in (("enquire_link", 7), ("submit_sm_resp", 8)):
            reply = SimpleNamespace(command=command, sequence=sequence, status=0)
            code, out, _, _ = self.run_sender(reply=reply)
            self.assertIn("Unexpected SMPP response", code)
            self.assertNotIn("SMS accepted", out)

    def test_timeout_does_not_resubmit(self):
        code, out, client, _ = self.run_sender(failure=TimeoutError("timeout"))
        self.assertIn("Check MSC logs before retrying", code)
        self.assertNotIn("SMS accepted", out)
        client.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
