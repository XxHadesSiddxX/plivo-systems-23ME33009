#!/usr/bin/env python3
"""sim.py — fast vectorized surrogate that RANKS knob configs for autotune.py.

ROLE (read this before trusting a number here)
    This is an APPROXIMATE Monte-Carlo model of the channel + FEC, used ONLY to
    order the knob grid so the real harness tries promising configs first. It
    never decides VALID/INVALID and never rejects a config — the real harness
    (run.py -> score.py) is always the source of truth. If the surrogate is
    wrong, autotune.py still finds the right answer; it just converges slower.

WHY IT EXISTS (the honest GPU story)
    run.py is real-time and cannot be GPU-accelerated. This surrogate can:
    it draws thousands of channel realizations as array ops, so a full grid is
    ranked in well under a second. It auto-uses the GPU via CuPy if installed
    (`pip install cupy-cuda12x`), else falls back to NumPy on CPU — same code.

MODEL (first-order, deliberately simple)
    Each frame i can arrive before its deadline t0+delay+i*20ms via:
      - direct media : sent at i*20ms, needs !drop and net-delay <= delay
      - parity @S1   : sent at (i+S1)*20ms, needs slack delay-S1*20-net >= 0,
                       its partner frame known, and not skipped
      - parity @S2   : sent at (i+S2)*20ms, same idea one stride longer
    ARQ is ignored (it is a backstop; FEC dominates the ranking). Partner
    availability is approximated as independent media delivery. These
    simplifications bias miss UP uniformly, so relative ordering holds.
"""
import numpy as _np

try:
    import cupy as _cp  # noqa
    xp = _cp
    ON_GPU = True
except Exception:
    xp = _np
    ON_GPU = False

FRAME_MS = 20.0


def _drops(prof, F, T, rng):
    """(T,F) boolean up-lane drop mask for a profile."""
    loss = float(prof.get("loss", 0.0))
    base = rng.random((T, F)) < loss
    burst = prof.get("burst_loss")
    if not burst:
        return base
    p_enter = burst["p_enter"]; p_exit = burst["p_exit"]; p_lib = burst["p_loss_in_burst"]
    in_b = xp.zeros(T, dtype=bool)
    out = xp.zeros((T, F), dtype=bool)
    for i in range(F):
        r = rng.random(T)
        enter = (~in_b) & (r < p_enter)
        exitb = in_b & (rng.random(T) < p_exit)
        in_b = (in_b | enter) & ~exitb
        lib = in_b & (rng.random(T) < p_lib)
        out[:, i] = base[:, i] | lib
    return out


def _net_delay(prof, shape, rng):
    dmin = float(prof.get("delay_min_ms", 5)); dmax = float(prof.get("delay_max_ms", 30))
    d = rng.random(shape) * (dmax - dmin) + dmin
    spike = prof.get("spike")
    if spike:
        hit = rng.random(shape) < spike["prob"]
        d = d + hit * spike["extra_ms"]
    return d


def estimate_miss(prof, knobs, delay_ms, F=600, T=400, seed=0):
    """Approx worst-plausible miss fraction for a knob set at a given delay."""
    rng = xp.random.default_rng(seed)
    S1 = int(knobs.get("FLAKY_S1", 2)); S2 = int(knobs.get("FLAKY_S2", 6))
    SKIP = int(knobs.get("FLAKY_SKIP", 12))
    if S2 <= S1:
        S2 = S1 + 1

    drop = _drops(prof, F, T, rng)                 # (T,F) media dropped on up lane
    dly = _net_delay(prof, (T, F), rng)            # (T,F) media net delay
    media_ok = (~drop) & (dly <= delay_ms)

    # parity packets: index by the frame f at which they are emitted
    pdrop = _drops(prof, F, T, rng)
    pdly = _net_delay(prof, (T, F), rng)
    skipped = (xp.arange(F) % SKIP) == (SKIP - 1)   # (F,) sender skips these

    ok = media_ok.copy()
    # parity @S1 recovers frame i (its 'a=f-S1' member), emitted at f=i+S1
    if S1 < F:
        par = ~pdrop[:, S1:] & (pdly[:, S1:] <= (delay_ms - S1 * FRAME_MS))
        par &= ~skipped[S1:][None, :]
        partner = xp.zeros((T, F), dtype=bool)      # partner = i + S1 - S2 (earlier)
        j = S1 - S2
        if j >= 0:
            partner[:, :F - j] |= media_ok[:, j:]   # (rare) partner ahead
        else:
            partner[:, -j:] |= media_ok[:, :F + j]  # partner = i-(S2-S1)
        ok[:, :F - S1] |= par & partner[:, :F - S1]
    # parity @S2 recovers frame i (its 'b=f-S2' member), emitted at f=i+S2
    if S2 < F:
        par = ~pdrop[:, S2:] & (pdly[:, S2:] <= (delay_ms - S2 * FRAME_MS))
        par &= ~skipped[S2:][None, :]
        partner = xp.zeros((T, F), dtype=bool)      # partner = i + S2 - S1 (later)
        k = S2 - S1
        partner[:, :F - k] |= media_ok[:, k:]
        ok[:, :F - S2] |= par & partner[:, :F - S2]

    miss_per_trial = 1.0 - ok.mean(axis=1)          # (T,)
    # rank on a pessimistic quantile so burst-sensitive configs are penalized
    q = xp.quantile(miss_per_trial, 0.9)
    return float(q)


def overhead_estimate(knobs, warmup=10, F=600):
    SKIP = int(knobs.get("FLAKY_SKIP", 12))
    S2 = int(knobs.get("FLAKY_S2", 6))
    media = 165.0 * F
    n_par = sum(1 for f in range(F) if f >= S2 and (f % SKIP) != (SKIP - 1))
    par = 169.0 * n_par
    return (media + par) / (160.0 * F)


def rank_grid(profiles, grid, args):
    """Return grid reordered best-first by estimated miss across profiles."""
    import json
    probe = (args.delay_lo + args.delay_hi) // 2
    profs = {n: json.load(open(p)) for n, p in profiles.items()}
    scored = []
    for knobs in grid:
        # rank by the hardest profile's estimated miss at the probe delay,
        # tie-broken by mean miss then overhead
        per = {n: estimate_miss(pr, knobs, probe) for n, pr in profs.items()}
        worst = max(per.values())
        mean = sum(per.values()) / len(per)
        scored.append((worst, mean, overhead_estimate(knobs), knobs))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in scored]


if __name__ == "__main__":
    # quick self-check / demo
    import json
    import sys
    prof = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "profiles/C.json"))
    print("backend:", "GPU/CuPy" if ON_GPU else "CPU/NumPy")
    for kb in ({"FLAKY_S1": 2, "FLAKY_S2": 4, "FLAKY_SKIP": 12},
               {"FLAKY_S1": 2, "FLAKY_S2": 6, "FLAKY_SKIP": 16},
               {"FLAKY_S1": 3, "FLAKY_S2": 9, "FLAKY_SKIP": 24}):
        for d in (100, 130, 160):
            m = estimate_miss(prof, kb, d)
            print(f"  {kb} @{d}ms -> ~{m*100:.2f}% miss, ovh~{overhead_estimate(kb):.2f}x")
