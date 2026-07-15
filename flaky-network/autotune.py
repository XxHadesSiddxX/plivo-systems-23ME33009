#!/usr/bin/env python3
"""autotune.py — automated min-delay tuner for the flaky-network assignment.

WHAT IT DOES
    For each profile it finds the LOWEST --delay_ms at which the real harness
    (run.py -> score.py) reports VALID (miss <= 1% AND overhead <= 2.0x),
    robustly across several relay seeds, while searching the sender's env
    knobs (FLAKY_S1 / FLAKY_S2 / FLAKY_SKIP, and any other FLAKY_* you add).

    The graded path is the REAL harness — this tool never fakes a score. It
    only automates the search around it:
        - config ranking  -> pick promising knob sets at a forgiving delay
        - binary search    -> minimal valid delay per config
        - local refinement -> nudge knobs to push the delay lower
        - confirmation     -> re-run the winner across more seeds

WHY IT IS FAST
    run.py is a wall-clock, real-time UDP sim (~duration seconds each). A GPU
    cannot speed that up. The lever is CPU cores: each trial runs inside its
    own network namespace (isolated localhost ports) AND its own symlink
    rundir (isolated output files), so N trials run truly in parallel. On a
    20-core box that is ~10x throughput on the graded path.

USAGE (the other agent just runs this)
    python3 autotune.py --dir /path/to/flaky-network --build
    python3 autotune.py --dir . --profiles A,B,C --seeds 3 --parallel 12
    python3 autotune.py --dir . --profiles C --resume        # continue/refine

    Re-running with --resume reuses every cached trial and only does new work,
    so it converges further each time instead of repeating itself.

OUTPUT (in --workdir, default ./autotune_out)
    trials.jsonl        every trial ever run (the resumable cache)
    best_configs.json   machine-readable best config per profile
    BEST.md             human leaderboard + exact reproduce commands
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# ----- score constants (mirror score.py; the harness remains the source of truth)
VALID_MISS = 0.01
VALID_OVERHEAD = 2.0

# ----- output parsing
_RE_MISS = re.compile(r"deadline misses\s*:\s*\d+\s*\(([\d.]+)%\)")
_RE_DELAY = re.compile(r"playout delay\s*:\s*([\d.]+)\s*ms")
_RE_OVH = re.compile(r"bandwidth overhead\s*:\s*([\d.]+)x")
_RE_RESULT = re.compile(r"RESULT\s*:\s*(VALID|INVALID)")


class StepMonitor(threading.Thread):
    """Watches CLOCK_REALTIME vs CLOCK_MONOTONIC. A step (WSL timesync) instantly
    expires a block of frame deadlines and poisons whatever run it lands in.
    Trials overlapping a recorded step are discarded and retried."""

    def __init__(self):
        super().__init__(daemon=True)
        self._steps = []
        self._lock = threading.Lock()
        self._stop = False

    def run(self):
        lr, lm = time.time(), time.monotonic()
        while not self._stop:
            time.sleep(0.01)
            r, m = time.time(), time.monotonic()
            if abs((r - lr) - (m - lm)) > 0.05:
                with self._lock:
                    self._steps.append(r)
            lr, lm = r, m

    def stepped_between(self, t0, t1):
        with self._lock:
            return any(t0 - 0.25 <= s <= t1 + 0.25 for s in self._steps)

    def stop(self):
        self._stop = True


def knobs_hash(knobs):
    return hashlib.sha1(json.dumps(knobs, sort_keys=True).encode()).hexdigest()[:10]


def netns_available():
    try:
        r = subprocess.run(
            ["unshare", "-rn", "bash", "-c",
             "ip link set lo up 2>/dev/null && ip addr show lo | grep -q 127.0.0.1 && echo OK"],
            capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "OK"
    except Exception:
        return False


class Runner:
    """Executes real harness trials in isolated rundirs (+ optional netns)."""

    HARNESS_FILES = ["run.py", "relay.py", "endpoints.py", "score.py", "common.py"]
    BINARIES = ["sender", "receiver"]

    def __init__(self, project_dir, workdir, duration, use_netns, monitor):
        self.dir = os.path.abspath(project_dir)
        self.runs_root = os.path.join(workdir, ".runs")
        os.makedirs(self.runs_root, exist_ok=True)
        self.duration = duration
        self.use_netns = use_netns
        self.monitor = monitor
        self._n = 0
        self._nlock = threading.Lock()
        for f in self.HARNESS_FILES:
            if not os.path.exists(os.path.join(self.dir, f)):
                sys.exit(f"missing {f} in {self.dir}")
        for b in self.BINARIES:
            if not os.path.exists(os.path.join(self.dir, b)):
                sys.exit(f"missing ./{b} in {self.dir} — run `make` (or pass --build)")

    def _rundir(self):
        with self._nlock:
            self._n += 1
            rd = os.path.join(self.runs_root, f"t{self._n:06d}")
        if os.path.exists(rd):
            for f in os.listdir(rd):
                try:
                    os.unlink(os.path.join(rd, f))
                except OSError:
                    pass
        else:
            os.makedirs(rd)
        for f in self.HARNESS_FILES + self.BINARIES:
            link = os.path.join(rd, f)
            if not os.path.lexists(link):
                os.symlink(os.path.join(self.dir, f), link)
        return rd

    def run(self, profile_path, delay, knobs, seed):
        """One real trial. Returns dict(miss, overhead, valid, delay, poison, err)."""
        rd = self._rundir()
        env = dict(os.environ)
        for k, v in knobs.items():
            env[str(k)] = str(v)
        cmd = [sys.executable, "run.py",
               "--profile", os.path.abspath(profile_path),
               "--seed", str(seed), "--delay_ms", str(delay),
               "--duration", str(self.duration)]
        if self.use_netns:
            inner = "ip link set lo up; exec " + shlex.join(cmd)
            argv = ["unshare", "-rn", "bash", "-c", inner]
        else:
            argv = cmd
        t0 = time.time()
        try:
            p = subprocess.run(argv, cwd=rd, env=env, capture_output=True,
                               text=True, timeout=self.duration + 90)
            out = p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return {"err": "timeout", "poison": False}
        t1 = time.time()
        if self.monitor and self.monitor.stepped_between(t0, t1):
            return {"poison": True}
        m, o, r = _RE_MISS.search(out), _RE_OVH.search(out), _RE_RESULT.search(out)
        if not (m and o and r):
            tail = out.strip().splitlines()[-1] if out.strip() else "no output"
            return {"err": f"no score ({tail})", "poison": False}
        miss = float(m.group(1)) / 100.0
        ovh = float(o.group(1))
        return {"miss": miss, "overhead": ovh,
                "valid": r.group(1) == "VALID", "delay": float(delay),
                "poison": False}


class Cache:
    """Resumable trial cache (trials.jsonl). Key: profile|delay|knobs|seed."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        self._lock = threading.Lock()
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        self.data[rec["key"]] = rec
                    except Exception:
                        pass

    @staticmethod
    def key(profile, delay, knobs, seed):
        return f"{profile}|{int(round(delay))}|{knobs_hash(knobs)}|{seed}"

    def get(self, key):
        return self.data.get(key)

    def put(self, key, rec):
        rec = dict(rec, key=key)
        with self._lock:
            self.data[key] = rec
            with open(self.path, "a") as f:
                f.write(json.dumps(rec) + "\n")


class Tuner:
    def __init__(self, args, runner, cache, pool, monitor):
        self.a = args
        self.runner = runner
        self.cache = cache
        self.pool = pool
        self.monitor = monitor
        self.evals = 0

    def _one(self, profile, delay, knobs, seed, retries=3):
        key = Cache.key(profile, delay, knobs, seed)
        hit = self.cache.get(key)
        if hit and "miss" in hit:
            return hit
        res = None
        for _ in range(retries):
            res = self.runner.run(self._profile_path(profile), delay, knobs, seed)
            if res.get("poison"):
                continue
            break
        self.evals += 1
        if res and "miss" in res:
            self.cache.put(key, res)
        return res

    def _profile_path(self, name):
        return self.profiles[name]

    def evaluate(self, profile, delay, knobs, seeds):
        """Run knobs@delay across `seeds` seeds in parallel. Worst-case verdict."""
        futs = [self.pool.submit(self._one, profile, delay, knobs, s)
                for s in seeds]
        results = [f.result() for f in futs]
        good = [r for r in results if r and "miss" in r]
        if not good:
            return {"ok": False, "miss": 1.0, "overhead": 9.9, "reason": "no valid runs"}
        wmiss = max(r["miss"] for r in good)
        wovh = max(r["overhead"] for r in good)
        ok = wmiss <= VALID_MISS and wovh <= VALID_OVERHEAD
        return {"ok": ok, "miss": wmiss, "overhead": wovh, "n": len(good)}

    def min_delay(self, profile, knobs, seeds, lo, hi):
        """Binary-search minimal integer delay that is valid across seeds."""
        top = self.evaluate(profile, hi, knobs, seeds)
        if not top["ok"]:
            return None, top
        best = (hi, top)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            r = self.evaluate(profile, mid, knobs, seeds)
            if r["ok"]:
                hi, best = mid, (mid, r)
            else:
                lo = mid
        return best[0], best[1]

    def tune_profile(self, name):
        a = self.a
        grid = self.grid
        seeds = list(range(1, a.seeds + 1))
        probe_seeds = list(range(1, min(a.seeds, 2) + 1))
        print(f"\n### profile {name}: {len(grid)} configs, "
              f"delay [{a.delay_lo},{a.delay_hi}], seeds={a.seeds}")

        # Phase A: rank configs at the most forgiving delay (cheap probe).
        ranked = []
        for i, knobs in enumerate(grid):
            r = self.evaluate(name, a.delay_hi, knobs, probe_seeds)
            ranked.append((r["ok"], r["miss"], r["overhead"], knobs))
            print(f"  rank {i+1}/{len(grid)} {knobs} -> "
                  f"miss {r['miss']*100:.2f}% ovh {r['overhead']:.2f}x "
                  f"{'ok' if r['ok'] else 'x'}")
        # prefer valid & low-miss & low-overhead
        ranked.sort(key=lambda t: (not t[0], t[1], t[2]))
        topk = [t[3] for t in ranked[:a.topk] if t[0] or ranked[0][0] is False]
        topk = topk or [t[3] for t in ranked[:a.topk]]

        # Phase B: minimal delay for each top config.
        best = None  # (delay, overhead, knobs, verdict)
        for knobs in topk:
            d, v = self.min_delay(name, knobs, seeds, a.delay_lo, a.delay_hi)
            if d is None:
                print(f"  {knobs}: no valid delay in range")
                continue
            print(f"  {knobs}: min valid delay = {d} ms "
                  f"(miss {v['miss']*100:.2f}% ovh {v['overhead']:.2f}x)")
            cand = (d, v["overhead"], knobs, v)
            if best is None or (d, v["overhead"]) < (best[0], best[1]):
                best = cand

        # Phase C: local refinement — nudge the winner's knobs to push delay down.
        for _ in range(a.rounds):
            if best is None:
                break
            improved = False
            for knobs in self._neighbors(best[2]):
                d, v = self.min_delay(name, knobs, seeds, a.delay_lo, best[0])
                if d is not None and (d, v["overhead"]) < (best[0], best[1]):
                    print(f"  refine: {knobs} -> {d} ms "
                          f"(was {best[0]} ms)")
                    best = (d, v["overhead"], knobs, v)
                    improved = True
            if not improved:
                break

        # Phase D: confirm winner across more seeds.
        if best is not None and a.confirm_seeds > a.seeds:
            cseeds = list(range(1, a.confirm_seeds + 1))
            c = self.evaluate(name, best[0], best[2], cseeds)
            print(f"  confirm @{best[0]}ms x{a.confirm_seeds} seeds: "
                  f"miss {c['miss']*100:.2f}% ovh {c['overhead']:.2f}x "
                  f"{'OK' if c['ok'] else 'FAILED — raising'}")
            if not c["ok"]:
                d, v = self.min_delay(name, best[2], cseeds, best[0], a.delay_hi)
                if d is not None:
                    best = (d, v["overhead"], best[2], v)
        return best

    def _neighbors(self, knobs):
        out = []
        s1 = int(knobs.get("FLAKY_S1", 2))
        s2 = int(knobs.get("FLAKY_S2", 6))
        skip = int(knobs.get("FLAKY_SKIP", 12))
        for ds1 in (-1, 0, 1):
            for ds2 in (-1, 0, 1):
                for dsk in (-4, 0, 4):
                    n1, n2, nk = s1 + ds1, s2 + ds2, skip + dsk
                    if n1 < 1 or n2 <= n1 or nk < 4:
                        continue
                    cand = dict(knobs, FLAKY_S1=n1, FLAKY_S2=n2, FLAKY_SKIP=nk)
                    if cand != knobs:
                        out.append(cand)
        return out


def default_grid():
    grid = []
    for s1 in (2, 3):
        for s2 in (4, 6, 8, 9):
            for skip in (10, 12, 16, 24):
                if s2 > s1:
                    grid.append({"FLAKY_S1": s1, "FLAKY_S2": s2, "FLAKY_SKIP": skip})
    return grid


def write_reports(workdir, dir_, duration, results):
    best_json = {}
    lines = ["# Autotune leaderboard", "",
             "VALID = miss <= 1.00% AND overhead <= 2.00x; lower delay wins.", "",
             "| profile | delay (ms) | worst miss | worst overhead | knobs |",
             "|---|---|---|---|---|"]
    for name, best in results.items():
        if best is None:
            lines.append(f"| {name} | — | — | — | no valid config found |")
            best_json[name] = None
            continue
        d, ovh, knobs, v = best
        kb = " ".join(f"{k}={val}" for k, val in sorted(knobs.items()))
        lines.append(f"| {name} | **{d}** | {v['miss']*100:.2f}% | {v['overhead']:.2f}x | {kb} |")
        best_json[name] = {"delay_ms": d, "worst_miss": v["miss"],
                           "worst_overhead": v["overhead"], "knobs": knobs}
    lines += ["", "## Reproduce", ""]
    for name, best in results.items():
        if best is None:
            continue
        d, ovh, knobs, v = best
        envs = " ".join(f"{k}={val}" for k, val in sorted(knobs.items()))
        lines.append(f"- **{name}**: `{envs} python3 run.py "
                     f"--profile profiles/{name}.json --delay_ms {d} --duration 30`")
    with open(os.path.join(workdir, "BEST.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(workdir, "best_configs.json"), "w") as f:
        json.dump(best_json, f, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".", help="flaky-network project dir")
    ap.add_argument("--profiles", default="A,B,C",
                    help="comma list of names (profiles/<name>.json) or paths")
    ap.add_argument("--seeds", type=int, default=3, help="relay seeds per verdict")
    ap.add_argument("--confirm-seeds", type=int, default=5)
    ap.add_argument("--duration", type=float, default=12)
    ap.add_argument("--parallel", type=int, default=0, help="0 = auto (cores-4)")
    ap.add_argument("--delay-lo", type=int, default=60)
    ap.add_argument("--delay-hi", type=int, default=220)
    ap.add_argument("--topk", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=1, help="local-refinement rounds")
    ap.add_argument("--grid", help="JSON file: list of knob dicts (default built-in)")
    ap.add_argument("--prefilter", action="store_true",
                    help="order the grid with sim.py surrogate (optional, GPU-friendly)")
    ap.add_argument("--workdir", default="autotune_out")
    ap.add_argument("--resume", action="store_true", help="reuse cached trials")
    ap.add_argument("--build", action="store_true", help="run `make` in --dir first")
    ap.add_argument("--no-netns", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    if args.build:
        print("building...")
        r = subprocess.run(["make"], cwd=args.dir)
        if r.returncode:
            sys.exit("make failed")

    if args.parallel <= 0:
        args.parallel = max(1, (os.cpu_count() or 4) - 4)
    use_netns = (not args.no_netns) and args.parallel > 1 and netns_available()
    print(f"parallel={args.parallel}  netns={'on' if use_netns else 'OFF (serial-safe)'}"
          f"  duration={args.duration}s")
    if args.parallel > 1 and not use_netns:
        print("  !! netns unavailable -> forcing serial to avoid port/file collisions")
        args.parallel = 1

    # profiles map name -> path
    profiles = {}
    for tok in args.profiles.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if os.sep in tok or tok.endswith(".json"):
            name = os.path.splitext(os.path.basename(tok))[0]
            profiles[name] = os.path.join(args.dir, tok) if not os.path.isabs(tok) else tok
        else:
            profiles[tok] = os.path.join(args.dir, "profiles", f"{tok}.json")
    for n, p in profiles.items():
        if not os.path.exists(p):
            sys.exit(f"profile {n} not found: {p}")

    grid = default_grid()
    if args.grid:
        grid = json.load(open(args.grid))
    if args.prefilter:
        try:
            import sim
            grid = sim.rank_grid(profiles, grid, args)
            print(f"prefilter: reordered {len(grid)} configs via surrogate")
        except Exception as e:
            print(f"prefilter skipped ({e})")

    if not args.resume:
        # start a fresh cache (keep old file backed up)
        tp = os.path.join(args.workdir, "trials.jsonl")
        if os.path.exists(tp):
            os.replace(tp, tp + ".bak")

    monitor = StepMonitor()
    monitor.start()
    runner = Runner(args.dir, args.workdir, args.duration, use_netns, monitor)
    cache = Cache(os.path.join(args.workdir, "trials.jsonl"))
    pool = ThreadPoolExecutor(max_workers=args.parallel)

    tuner = Tuner(args, runner, cache, pool, monitor)
    tuner.profiles = profiles
    tuner.grid = grid

    t_start = time.time()
    results = {}
    for name in profiles:
        results[name] = tuner.tune_profile(name)
        write_reports(args.workdir, args.dir, args.duration, results)  # incremental

    monitor.stop()
    pool.shutdown(wait=False)
    dt = time.time() - t_start
    print(f"\n==== done in {dt/60:.1f} min, {tuner.evals} real trials ====")
    print(open(os.path.join(args.workdir, "BEST.md")).read())


if __name__ == "__main__":
    main()
