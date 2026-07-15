"""Run one experiment: relay + harness endpoints + YOUR two binaries.

    make && python3 run.py --profile profiles/A.json --delay_ms 60

Your binaries default to ./sender and ./receiver; override with
--sender_cmd / --receiver_cmd (any language, any command line):

    python3 run.py --sender_cmd "cargo run --release --bin sender" ...

The harness passes T0, DURATION_S, DELAY_MS to your processes as env vars
and kills them when the run ends.
"""
import argparse
import os
import secrets
import shlex
import subprocess
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="profiles/A.json")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--delay_ms", type=float, default=60)
    ap.add_argument("--duration", type=float, default=30)
    ap.add_argument("--sender_cmd", default="./sender")
    ap.add_argument("--receiver_cmd", default="./receiver")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__)) or "."

    for cmd, flag in ((args.sender_cmd, "--sender_cmd"),
                      (args.receiver_cmd, "--receiver_cmd")):
        exe = shlex.split(cmd)[0]
        if os.sep in exe and not os.path.exists(os.path.join(here, exe)):
            sys.exit(f"{exe} not found — run `make` first "
                     f"(or point {flag} at your build)")

    # a previous run's results must never be scored as this run's
    for f in ("playout_log.json", "relay_stats.json"):
        try:
            os.unlink(os.path.join(here, f))
        except FileNotFoundError:
            pass

    stream_seed = secrets.token_hex(8)
    t0 = time.time() + 1.5
    student_env = dict(os.environ, T0=str(t0), DURATION_S=str(args.duration),
                       DELAY_MS=str(args.delay_ms))

    procs = []
    try:
        relay = subprocess.Popen(
            [sys.executable, "relay.py", "--profile", args.profile,
             "--seed", str(args.seed), "--duration", str(args.duration)],
            cwd=here)
        procs.append(relay)
        recv = subprocess.Popen(shlex.split(args.receiver_cmd), cwd=here,
                                env=student_env)
        procs.append(recv)
        send = subprocess.Popen(shlex.split(args.sender_cmd), cwd=here,
                                env=student_env)
        procs.append(send)
        time.sleep(0.3)
        for p, what, hint in ((relay, "relay", "ports 47001/47003 busy?"),
                              (recv, "receiver", "port 47002 busy?"),
                              (send, "sender", "ports 47010/47004 busy?")):
            if p.poll() is not None:
                sys.exit(f"{what} exited before the run started ({hint})")
        # seed goes over a pipe: unlike env/argv it is not visible to
        # other processes, so payloads stay unguessable during the run
        endpoints = subprocess.Popen(
            [sys.executable, "endpoints.py", "--t0", str(t0),
             "--duration", str(args.duration), "--delay_ms", str(args.delay_ms)],
            cwd=here, stdin=subprocess.PIPE)
        procs.append(endpoints)
        endpoints.stdin.write((stream_seed + "\n").encode())
        endpoints.stdin.close()

        endpoints.wait(timeout=args.duration + 60)
        for p in (send, recv):
            p.kill()
        relay.wait(timeout=30)
        if endpoints.returncode != 0:
            sys.exit("endpoints failed — this run produced no playout log "
                     "(is port 47020 free?)")
        if relay.returncode != 0:
            sys.exit("relay failed — this run produced no relay stats "
                     "(are ports 47001/47003 free?)")
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()

    subprocess.run([sys.executable, "score.py", "--stream_seed", stream_seed,
                    "--duration", str(args.duration)], cwd=here, check=True)


if __name__ == "__main__":
    main()
