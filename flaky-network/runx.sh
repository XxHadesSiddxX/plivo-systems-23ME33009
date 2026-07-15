#!/usr/bin/env bash
# Local-only wrapper around run.py for WSL2: systemd-timesyncd steps
# CLOCK_REALTIME forward ~2.9s every ~30s here, which instantly expires a
# block of frame deadlines and poisons the run. Detect a step during the
# run and retry. Not part of the submission; grading calls run.py directly.
set -u
WATCH=$(dirname "$0")/.clockwatch.py
cat > "$WATCH" <<'EOF'
import sys, time
end = time.monotonic() + float(sys.argv[1])
lr, lm = time.time(), time.monotonic()
while time.monotonic() < end:
    time.sleep(0.01)
    r, m = time.time(), time.monotonic()
    if abs((r - lr) - (m - lm)) > 0.05:
        print("CLOCK_STEP")
        sys.exit(1)
    lr, lm = r, m
EOF
DURATION=15
prev=""
for a in "$@"; do case $prev in --duration) DURATION=$a;; esac; prev=$a; done
for try in 1 2 3 4 5; do
    python3 "$WATCH" $((DURATION + 8)) & wpid=$!
    python3 run.py "$@" 2>/dev/null | grep -E "misses|overhead|RESULT"
    if wait $wpid; then exit 0; fi
    echo "(clock step during run — retrying)"
done
echo "gave up: clock stepped in 5 consecutive runs" >&2
exit 1
