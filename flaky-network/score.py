"""Score the last run: reads playout_log.json + relay_stats.json.

VALID run = deadline-miss rate <= 1.0% AND bandwidth overhead <= 2.0x.
Among valid runs, LOWER PLAYOUT DELAY WINS; overhead breaks ties.

    python score.py --stream_seed <hex> --duration 30
"""
import argparse
import hashlib
import json

import common


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream_seed", required=True)
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--playout_log", default="playout_log.json")
    ap.add_argument("--relay_stats", default="relay_stats.json")
    args = ap.parse_args()

    log = json.load(open(args.playout_log))
    stats = json.load(open(args.relay_stats))
    frames = log["frames"]
    n = len(frames)

    misses = 0
    for fr in frames:
        if not fr["present"]:
            misses += 1
            continue
        want = hashlib.sha256(
            common.frame_payload(args.stream_seed, fr["i"])).hexdigest()
        if fr["sha"] != want:
            misses += 1  # wrong/corrupt payload = miss
    miss_rate = misses / max(1, n)

    raw = n * common.PAYLOAD_BYTES
    overhead = (stats["up_bytes"] + stats["down_bytes"]) / max(1, raw)

    print("================ SCORE ================")
    print(f"  frames               : {n}")
    print(f"  deadline misses      : {misses}  ({miss_rate*100:.2f}%)   [cap 1.00%]")
    print(f"  playout delay        : {log['delay_ms']:.0f} ms   <-- your score if valid; lower wins")
    print(f"  bandwidth overhead   : {overhead:.2f}x   [cap 2.00x]"
          f"   (up {stats['up_bytes']}B, feedback {stats['down_bytes']}B)")
    valid = miss_rate <= 0.01 and overhead <= 2.0
    print(f"  RESULT               : {'VALID' if valid else 'INVALID'}")
    if not valid:
        print("  (reduce misses under 1% and overhead under 2x, THEN minimize delay)")


if __name__ == "__main__":
    main()
