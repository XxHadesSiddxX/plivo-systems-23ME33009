"""The hostile relay. DO NOT MODIFY — grading uses our copy with profiles
you have not seen. It forwards UDP datagrams in both directions and, per a
seeded random profile, drops, delays, duplicates, and (via random delay)
reorders them. It also counts every byte you transmit, in both directions,
for the overhead cap.

Impairments are drawn per packet the relay receives on a lane, so a run is
only reproducible if you send the identical packet sequence: change what
you send and the loss/delay pattern shifts even with the same --seed.

    python relay.py --profile profiles/A.json --seed 1 --duration 30
"""
import argparse
import heapq
import json
import random
import select
import socket
import time

import common


class Impair:
    def __init__(self, prof, rng):
        self.p = prof
        self.rng = rng
        self.in_burst = False  # Gilbert-Elliott burst-loss state

    def drop(self):
        p = self.p
        if p.get("burst_loss"):
            b = p["burst_loss"]
            if self.in_burst:
                if self.rng.random() < b["p_exit"]:
                    self.in_burst = False
            else:
                if self.rng.random() < b["p_enter"]:
                    self.in_burst = True
            if self.in_burst and self.rng.random() < b["p_loss_in_burst"]:
                return True
        return self.rng.random() < p.get("loss", 0.0)

    def delay_s(self):
        p = self.p
        d = self.rng.uniform(p.get("delay_min_ms", 5), p.get("delay_max_ms", 30))
        spike = p.get("spike")
        if spike and self.rng.random() < spike["prob"]:
            d += spike["extra_ms"]
        return d / 1000.0

    def dup(self):
        return self.rng.random() < self.p.get("dup", 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--stats_out", default="relay_stats.json")
    args = ap.parse_args()
    prof = json.load(open(args.profile))

    up_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # from sender
    up_in.bind(common.SEND_TO_RELAY)
    dn_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # from receiver
    dn_in.bind(common.RECV_TO_RELAY)
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for s in (up_in, dn_in, out):
        s.setblocking(False)

    lanes = {
        up_in: (common.RELAY_TO_RECV, Impair(prof, random.Random(args.seed)),
                "up"),
        dn_in: (common.RELAY_TO_SEND, Impair(prof, random.Random(args.seed + 1)),
                "down"),
    }
    stats = {"up_bytes": 0, "down_bytes": 0, "up_pkts": 0, "down_pkts": 0,
             "dropped": 0, "duplicated": 0}
    heap = []  # (release_time, seqno, dest, payload)
    n = 0
    end = time.time() + args.duration + 3.0

    while time.time() < end:
        timeout = 0.05
        if heap:
            timeout = max(0.0, min(timeout, heap[0][0] - time.time()))
        ready, _, _ = select.select([up_in, dn_in], [], [], timeout)
        now = time.time()
        for s in ready:
            try:
                data, _ = s.recvfrom(65535)
            except BlockingIOError:
                continue
            dest, imp, lane = lanes[s]
            stats[f"{lane}_bytes"] += len(data)
            stats[f"{lane}_pkts"] += 1
            if imp.drop():
                stats["dropped"] += 1
                continue
            heapq.heappush(heap, (now + imp.delay_s(), n, dest, data)); n += 1
            if imp.dup():
                stats["duplicated"] += 1
                heapq.heappush(heap, (now + imp.delay_s(), n, dest, data)); n += 1
        while heap and heap[0][0] <= time.time():
            _, _, dest, data = heapq.heappop(heap)
            try:
                out.sendto(data, dest)
            except OSError:
                pass

    with open(args.stats_out, "w") as f:
        json.dump(stats, f, indent=1)
    print("relay done:", stats)


if __name__ == "__main__":
    main()
