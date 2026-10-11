#!/usr/bin/env python3
"""Submit one text SMS through OsmoMSC's VTY (Python standard library only).

Run on the MSC host for the localhost VTY used by the native and Docker labs:
  python3 scripts/send-sms.py --to 123 --sender 100 --message "Hello from GSMLab"
  python3 scripts/send-sms.py --imsi 001010123456789 --message "Hello"

VTY transport accepts printable ASCII only. Question marks invoke VTY help and
cannot be sent this way; backticks are outside the GSM alphabet. Repeated spaces
are normalized by VTY. Messages must fit a single SMS (160 GSM septets).
Submission does not confirm database storage or delivery to the handset: the
VTY does not report every SMSC failure. Inspect MSC logs for those failures.
Protocol reference: https://downloads.osmocom.org/docs/osmo-msc/master/osmomsc-vty-reference.pdf
"""
from __future__ import annotations

import argparse
import math
import re
import socket
import sys
import time

PROMPT = re.compile(rb"(?m)^([A-Za-z0-9_.:/-]+)([>#]) ?\Z")
EXTENDED_GSM = frozenset("^{}\\[~]|")


class VtyError(RuntimeError):
    """The VTY rejected a command or did not finish its response."""


def number(value: str) -> str:
    """Accept a numeric MSISDN, optionally with an international '+' prefix."""
    value = value.removeprefix("+")
    if not re.fullmatch(r"[0-9]{1,15}", value):
        raise argparse.ArgumentTypeError("Use 1-15 digits, optionally preceded by +")
    return value


def imsi(value: str) -> str:
    if not re.fullmatch(r"[0-9]{5,15}", value):
        raise argparse.ArgumentTypeError("IMSI must contain 5-15 digits")
    return value


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535")
    return port


def timeout_seconds(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Timeout must be a number") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("Timeout must be finite and greater than zero")
    return timeout


def sms_text(value: str) -> str:
    if not value.strip(" "):
        raise argparse.ArgumentTypeError("Message must not be empty")
    if any(not 32 <= ord(char) <= 126 for char in value):
        raise argparse.ArgumentTypeError(
            "VTY supports one line of printable ASCII; Unicode and control characters are unsupported"
        )
    if "?" in value:
        raise argparse.ArgumentTypeError("VTY treats '?' as help; question marks cannot be sent through VTY")
    if "`" in value:
        raise argparse.ArgumentTypeError("Backticks are unsupported by the GSM SMS alphabet")
    # OsmoMSC's VTY joins the message tokens with single spaces.
    value = " ".join(value.split())
    septets = sum(2 if char in EXTENDED_GSM else 1 for char in value)
    if septets > 160:
        raise argparse.ArgumentTypeError(
            f"Message uses {septets} GSM septets; maximum is 160 (no multipart SMS)"
        )
    return value


class VtyClient:
    """Small Telnet client with bounded, prompt-based reads; no telnetlib needed."""

    def __init__(self, connection: socket.socket, timeout: float):
        self.connection = connection
        self.timeout = timeout
        self.state = "data"
        self.negotiation = 0
        self.hostname: bytes | None = None
        self.prompt_level: bytes | None = None

    def telnet_text(self, data: bytes) -> bytes:
        """Strip Telnet controls, including negotiations split across packets."""
        text = bytearray()
        for char in data:
            if self.state == "data":
                if char == 255:  # IAC
                    self.state = "iac"
                elif char != 0:  # Telnet CR NUL
                    text.append(char)
            elif self.state == "iac":
                if char == 255:
                    text.append(char)
                    self.state = "data"
                elif char in (251, 252, 253, 254):  # WILL/WONT/DO/DONT
                    self.negotiation = char
                    self.state = "option"
                elif char == 250:  # SB
                    self.state = "subnegotiation"
                else:
                    self.state = "data"
            elif self.state == "option":
                if self.negotiation == 251:
                    # Accept the server's echo and suppress-go-ahead options.
                    reply = 253 if char in (1, 3) else 254
                    self.connection.sendall(bytes((255, reply, char)))
                elif self.negotiation == 253:
                    self.connection.sendall(bytes((255, 252, char)))
                self.state = "data"
            elif self.state == "subnegotiation":
                if char == 255:
                    self.state = "subnegotiation_iac"
            elif self.state == "subnegotiation_iac":
                self.state = "data" if char == 240 else "subnegotiation"
        return bytes(text)

    def read_prompt(self) -> str:
        deadline = time.monotonic() + self.timeout
        received = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VtyError("Timed out waiting for an MSC VTY prompt")
            self.connection.settimeout(remaining)
            try:
                chunk = self.connection.recv(4096)
            except TimeoutError as error:
                raise VtyError("Timed out waiting for an MSC VTY prompt") from error
            if not chunk:
                raise VtyError("MSC closed the VTY connection before replying")
            received.extend(self.telnet_text(chunk))
            if len(received) > 65536:
                raise VtyError("MSC VTY response exceeded 64 KiB")
            normalized = bytes(received).replace(b"\r", b"")
            if re.search(rb"(?i)(?:password|username):\s*\Z", normalized):
                raise VtyError(
                    "MSC VTY requires authentication; this helper expects the lab's 'no login' VTY"
                )
            match = PROMPT.search(normalized)
            if match and (self.hostname is None or match[1] == self.hostname):
                self.hostname = match[1]
                self.prompt_level = match[2]
                return normalized[:match.start()].decode("ascii", errors="replace")

    def command(self, command: str) -> str:
        self.connection.settimeout(self.timeout)
        # CR alone executes exactly once; CRLF would execute an extra blank line.
        self.connection.sendall(command.encode("ascii") + b"\r")
        reply = self.read_prompt()
        lines = [line for line in reply.splitlines() if line.strip() != command]
        errors = [line.strip() for line in lines if line.lstrip().startswith("%")]
        if errors:
            raise VtyError("; ".join(errors))
        return "\n".join(lines).strip()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Submit one SMS to a GSM subscriber through OsmoMSC's VTY.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    recipient = result.add_mutually_exclusive_group(required=True)
    recipient.add_argument("--to", type=number, metavar="MSISDN", help="Recipient phone number")
    recipient.add_argument("--imsi", type=imsi, help="Identify recipient by IMSI instead")
    result.add_argument("--sender", type=number, default="100", help="Displayed sender number (default: 100)")
    result.add_argument("--message", "-m", type=sms_text, required=True, help="Single SMS text, quoted in the shell")
    result.add_argument("--host", default="127.0.0.1", help="MSC VTY host (default: 127.0.0.1)")
    result.add_argument("--port", type=port_number, default=4254, help="MSC VTY port (default: 4254)")
    result.add_argument("--timeout", type=timeout_seconds, default=10.0, help="Connection/response timeout in seconds")
    result.add_argument("--dry-run", action="store_true", help="Validate and display the command without connecting")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    kind, recipient = ("imsi", args.imsi) if args.imsi else ("msisdn", args.to)
    command = f"subscriber {kind} {recipient} sms sender msisdn {args.sender} send {args.message}"
    if args.dry_run:
        print(f"MSC VTY: {args.host}:{args.port}\n{command}")
        return 0
    submitted = False
    try:
        with socket.create_connection((args.host, args.port), timeout=args.timeout) as connection:
            client = VtyClient(connection, args.timeout)
            client.read_prompt()
            if client.prompt_level != b"#":
                client.command("enable")
                if client.prompt_level != b"#":
                    raise VtyError("Could not enter the enabled MSC VTY prompt")
            # No automatic retries: a timeout after transmission may still mean
            # the SMS was submitted, and retrying could duplicate it.
            submitted = True
            client.command(command)
    except VtyError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if submitted and ("Timed out" in str(error) or "closed" in str(error)):
            print("Submission outcome is unknown; inspect MSC logs before retrying.", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"ERROR: MSC VTY {args.host}:{args.port}: {error}", file=sys.stderr)
        if submitted:
            print("Submission outcome is unknown; inspect MSC logs before retrying.", file=sys.stderr)
        return 1
    print(f"SMS command submitted to OsmoMSC for {kind.upper()} {recipient} from {args.sender}.")
    print("Storage and handset delivery are not confirmed by the VTY; check MSC logs if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
