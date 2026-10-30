"""Results leave a lab VM as one framed payload on its second serial port.

A frame is a marker line, a JSON header with the payload's length and SHA-256, then the payload.
A serial port has no end-of-file, so the length bounds the payload and the hash catches truncation
or stray bytes.
"""

import hashlib
import json

MARKER = b"\n--- schedlab results v1 ---\n"


def encode(payload):
    header = json.dumps({"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    return MARKER + header.encode() + b"\n" + payload


def decode(data):
    """The payload of the first frame in `data`; ValueError if it is missing, short or corrupt."""
    start = data.find(MARKER)
    if start < 0:
        raise ValueError("no results frame: the guest did not finish")
    header_start = start + len(MARKER)
    header_end = data.find(b"\n", header_start)
    if header_end < 0:
        raise ValueError("results frame header is incomplete")
    header = json.loads(data[header_start:header_end])
    payload = data[header_end + 1 : header_end + 1 + header["bytes"]]
    if len(payload) != header["bytes"]:
        raise ValueError(f"results frame is truncated: {len(payload)} of {header['bytes']} bytes")
    if hashlib.sha256(payload).hexdigest() != header["sha256"]:
        raise ValueError("results frame is corrupt: SHA-256 mismatch")
    return payload
