#!/usr/bin/env python3
"""Basic SMPP sender: python3 scripts/send-sms.py 123 100 'Hello there!'

Requires smpplib and the MSC settings in examples/smpp.cfg.
Set SMPP_PASSWORD to the ESME password; SMPP_HOST defaults to 127.0.0.1.
"""
import argparse
import os
import re
import sys

import smpplib.client
import smpplib.exceptions
import smpplib.gsm


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send one SMS through OsmoMSC SMPP.")
    for name in ("recipient", "sender", "message"):
        parser.add_argument(name)
    args = parser.parse_args(argv)
    if not all(re.fullmatch(r"[0-9]{1,15}", value) for value in (args.recipient, args.sender)):
        parser.error("Recipient and sender must each contain 1-15 digits")
    password = os.environ.get("SMPP_PASSWORD", "")
    if not re.fullmatch(r"[!-~]{1,8}", password):
        parser.error("Set SMPP_PASSWORD to your 1-8 character ESME password (ASCII, no spaces)")
    if not args.message.strip() or len(args.message) > 160:
        parser.error("Use a nonempty message that fits one SMS")
    parts, coding, flags = smpplib.gsm.make_parts(args.message)
    if len(parts) != 1:
        parser.error("Message is too long for one SMS with this text encoding")
    accepted = set()
    try:
        with smpplib.client.Client(os.getenv("SMPP_HOST", "127.0.0.1"), 2775,
                                  timeout=10, allow_unknown_opt_params=True) as client:
            client.set_message_sent_handler(lambda pdu: accepted.add(pdu.sequence))
            client.connect()
            client.bind_transmitter(system_id="sms-client", password=password)
            request = client.send_message(
                source_addr_ton=0, source_addr_npi=1, source_addr=args.sender,
                dest_addr_ton=0, dest_addr_npi=1, destination_addr=args.recipient,
                short_message=parts[0], data_coding=coding, esm_class=flags,
            )
            while request.sequence not in accepted:
                client.read_once(auto_send_enquire_link=False)
    except (OSError, smpplib.exceptions.ConnectionError, smpplib.exceptions.PDUError) as error:
        sys.exit(f"SMPP error: {error}. Check MSC logs before retrying.")
    print(f"SMS accepted by OsmoMSC for {args.recipient}; handset delivery is not confirmed.")


if __name__ == "__main__":
    main()
