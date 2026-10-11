#!/usr/bin/env python3
# Usage: send-sms.py recipient sender server_ip "message"
import sys
import smpplib.client
import smpplib.exceptions
import smpplib.gsm

if len(sys.argv) != 5:
    sys.exit('Usage: send-sms.py recipient sender server_ip "message"')

recipient, sender, server, message = sys.argv[1:]

if not recipient.isdigit() or not sender.isdigit():
    sys.exit("Recipient and sender must be phone numbers")

try:
    parts, coding, flags = smpplib.gsm.make_parts(message)
    if not message.strip() or len(parts) != 1:
        sys.exit("Keep the message short enough to fit one SMS")

    with smpplib.client.Client(server, 2775, timeout=10,
                              allow_unknown_opt_params=True) as client:
        client.connect()
        client.bind_transmitter(system_id="sms-client", password="")
        request = client.send_message(
            source_addr=sender, source_addr_ton=0, source_addr_npi=1,
            destination_addr=recipient, dest_addr_ton=0, dest_addr_npi=1,
            short_message=parts[0], data_coding=coding, esm_class=flags,
        )
        reply = client.read_pdu()
        if reply.command != "submit_sm_resp" or reply.sequence != request.sequence:
            raise RuntimeError("Unexpected SMPP response")
        if reply.status:
            raise RuntimeError(f"MSC rejected the SMS (status {reply.status})")

except (OSError, RuntimeError, ValueError, smpplib.exceptions.ConnectionError) as error:
    sys.exit(f"SMS error: {error}. Check MSC logs before retrying.")

print(f"SMS accepted for {recipient}. Handset delivery is not confirmed.")
