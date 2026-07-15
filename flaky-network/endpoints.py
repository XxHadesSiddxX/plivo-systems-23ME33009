"""Harness endpoints — DO NOT MODIFY (grading uses our copy).

SOURCE thread: emits frame i to the student sender at exactly t0 + i*20ms.
The student sender physically cannot send a frame early: it does not have
it yet.

PLAYER thread: listens for frames from the student receiver and judges each
frame i against its playout deadline t0 + delay_ms + i*20ms. A frame counts
only if a packet with the correct seq arrived BEFORE its deadline; payload
correctness is verified offline by score.py (the seed never leaves the
harness).

Message format on BOTH harness legs (source->sender, receiver->player):
    4-byte big-endian unsigned seq  +  160-byte payload   (164 bytes total)
"""
import argparse
import json
import socket
import struct
import sys
import threading
import time

import common


def source(t0, n_frames, seed):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i in range(n_frames):
        wait = t0 + i * common.FRAME_MS / 1000 - time.time()
        if wait > 0:
            time.sleep(wait)
        pkt = struct.pack(common.HEADER_FMT, i) + common.frame_payload(seed, i)
        sock.sendto(pkt, common.SOURCE_TO_SENDER)


def player(t0, n_frames, delay_ms, log_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(common.RECV_TO_PLAYER)
    sock.settimeout(0.05)
    first_arrival = {}   # seq -> (arrival_time, sha256 of payload)
    import hashlib
    last_deadline = t0 + delay_ms / 1000 + (n_frames - 1) * common.FRAME_MS / 1000
    while time.time() < last_deadline + 0.5:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        if len(data) < 4:
            continue
        (i,) = struct.unpack(common.HEADER_FMT, data[:4])
        if 0 <= i < n_frames and i not in first_arrival:
            first_arrival[i] = (time.time(),
                                hashlib.sha256(data[4:]).hexdigest())
    frames = []
    for i in range(n_frames):
        deadline = t0 + delay_ms / 1000 + i * common.FRAME_MS / 1000
        arr = first_arrival.get(i)
        ok = arr is not None and arr[0] <= deadline
        frames.append({"i": i, "present": bool(ok),
                       "sha": arr[1] if ok else None})
    with open(log_path, "w") as f:
        json.dump({"delay_ms": delay_ms, "frames": frames}, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--t0", type=float, required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--delay_ms", type=float, required=True)
    ap.add_argument("--log", default="playout_log.json")
    args = ap.parse_args()
    # seed arrives on stdin (a pipe from run.py): unlike env vars or argv
    # it is not readable by other processes, so it never leaves the harness
    seed = sys.stdin.readline().strip()
    if not seed:
        sys.exit("no stream seed on stdin (endpoints.py is launched by run.py)")
    n = int(args.duration * 1000 // common.FRAME_MS)
    player_err = []

    def run_player():
        try:
            player(args.t0, n, args.delay_ms, args.log)
        except BaseException as e:
            player_err.append(e)
            raise

    tp = threading.Thread(target=run_player)
    tp.start()
    source(args.t0, n, seed)
    tp.join()
    if player_err:
        sys.exit(f"player failed, no playout log written: {player_err[0]!r}")
    print("endpoints done")


if __name__ == "__main__":
    main()
