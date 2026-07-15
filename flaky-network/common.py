"""Constants + payload generator for the HARNESS (relay, endpoints, scorer).
Student code is C / C++ / Go / Rust and never imports this file — the contract
with student processes is purely UDP ports and byte formats (see
ASSIGNMENT.md).
"""
import hashlib

FRAME_MS = 20
PAYLOAD_BYTES = 160
HEADER_FMT = "!I"          # harness framing: big-endian uint32 seq + payload

# localhost ports
SOURCE_TO_SENDER = ("127.0.0.1", 47010)  # harness source -> student sender
SEND_TO_RELAY = ("127.0.0.1", 47001)     # student sender -> relay (media)
RELAY_TO_RECV = ("127.0.0.1", 47002)     # relay -> student receiver
RECV_TO_RELAY = ("127.0.0.1", 47003)     # student receiver -> relay (feedback)
RELAY_TO_SEND = ("127.0.0.1", 47004)     # relay -> student sender (feedback)
RECV_TO_PLAYER = ("127.0.0.1", 47020)    # student receiver -> harness player


def frame_payload(seed: str, i: int) -> bytes:
    """Deterministic secret payload for frame i. The seed lives only in the
    harness endpoints process; student code must move bytes, not guess them."""
    out = b""
    j = 0
    while len(out) < PAYLOAD_BYTES:
        out += hashlib.sha256(f"{seed}:{i}:{j}".encode()).digest()
        j += 1
    return out[:PAYLOAD_BYTES]
