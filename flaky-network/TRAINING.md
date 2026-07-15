# Autotune harness — how to "train" the flaky-network solution

This is a **search harness**, not a model trainer. The assignment has no weights
to learn; the score is: **lowest `--delay_ms` that stays VALID** (miss ≤ 1% and
overhead ≤ 2.0x). So "training" = automatically searching `delay_ms` and the
sender's env knobs against the **real** harness, in parallel, and reporting the
lowest valid delay per profile.

## TL;DR for the other agent

After any change to `sender.c` / `receiver.c`, rebuild and run:

```bash
python3 autotune.py --dir . --build --profiles A,B,C --seeds 3 --duration 15
```

It prints a leaderboard and writes `autotune_out/BEST.md` +
`autotune_out/best_configs.json`. The number in the **delay** column is your
score for that profile (lower is better). That single command replaces manual
`run.py` sweeps — it finds the minimal valid delay and the best knobs for you.

To keep improving without redoing work (e.g. after editing the code again):

```bash
python3 autotune.py --dir . --resume --profiles C --seeds 5 --rounds 2
```

`--resume` reuses every cached trial and only runs what's new, so each pass
converges further instead of repeating itself.

## Why it's fast (and the honest GPU note)

`run.py` is a **real-time** UDP simulation — one run costs real wall-clock
seconds and a GPU cannot speed it up. The real lever is **CPU cores**: every
trial runs in its own **network namespace** (isolated localhost ports) *and* its
own **symlink rundir** (isolated `playout_log.json` / `relay_stats.json`), so
many real runs execute at once with no collisions. On this 20-core box that is
~10x throughput on the graded path.

The **GPU** has one legitimate job here: `sim.py`, a vectorized Monte-Carlo
*surrogate* that ranks knob configs so the real search tries good ones first.
Enable it with `--prefilter`. It only reorders the grid — it never decides
VALID/INVALID, so it can't give a wrong answer, only a slower or faster search.
It auto-uses the GPU if CuPy is installed (`pip install cupy-cuda12x`), else
NumPy on CPU.

```bash
python3 autotune.py --dir . --build --prefilter --profiles A,B,C
```

## What it searches

- `--delay_ms`: binary-searched between `--delay-lo` and `--delay-hi`.
- Sender env knobs: `FLAKY_S1`, `FLAKY_S2`, `FLAKY_SKIP` (parity strides + skip).
  The default grid covers S1∈{2,3}, S2∈{4,6,8,9}, SKIP∈{10,12,16,24}.

### Adding new knobs (recommended for deeper tuning)

Any `FLAKY_*` environment variable your C code reads is searchable — just put it
in a grid file and pass `--grid`:

```bash
echo '[{"FLAKY_S1":2,"FLAKY_S2":4,"FLAKY_SKIP":12,"FLAKY_NACK_TRIES":3}]' > grid.json
python3 autotune.py --dir . --grid grid.json
```

The **receiver** currently has no env knobs (its NACK/RTT params are `#define`s).
To let the harness tune them, make them `getenv`-readable, e.g. in `receiver.c`:

```c
double respace = getenv("FLAKY_NACK_RESPACE_MS")
    ? atof(getenv("FLAKY_NACK_RESPACE_MS"))/1000.0 : NACK_RESPACE_S;
```

Then add `FLAKY_NACK_RESPACE_MS` to your grid. Nothing else changes.

## How the search works (per profile)

1. **Rank** — each knob config is run at the most forgiving delay (`--delay-hi`)
   across a couple of seeds; configs are ordered by worst-case miss. With
   `--prefilter`, the surrogate pre-orders the whole grid first (cheap).
2. **Minimize delay** — the top `--topk` configs each get a binary search for
   the lowest delay that stays VALID across all `--seeds` seeds (worst-case).
3. **Refine** — `--rounds` of local search nudge the winner's knobs (±1 strides,
   ±SKIP) to try to push the delay lower.
4. **Confirm** — the winner is re-run across `--confirm-seeds` seeds; if it
   fails the wider check, the delay is raised until it holds.

VALID uses the **worst** seed (miss and overhead), so a reported delay is robust,
not a lucky single run.

## Robustness guards

- **Clock-step immunity**: a background monitor watches CLOCK_REALTIME vs
  MONOTONIC (the WSL timesync step described in `RUNLOG.md`). Any trial that
  overlaps a step is discarded and retried — poisoned runs never pollute a score.
- **Serial fallback**: if network namespaces aren't available, it drops to
  `--parallel 1` automatically (correct, just slower). Force it with `--no-netns`.

## Key options

| flag | default | meaning |
|---|---|---|
| `--dir` | `.` | flaky-network project dir |
| `--profiles` | `A,B,C` | names (→ `profiles/<n>.json`) or paths |
| `--seeds` | 3 | relay seeds per validity verdict (worst-case) |
| `--confirm-seeds` | 5 | seeds for the final confirmation |
| `--duration` | 12 | seconds per run (use ≥15 for final numbers) |
| `--parallel` | auto | concurrent trials (auto = cores−4) |
| `--delay-lo/-hi` | 60 / 220 | delay search bounds (ms) |
| `--topk` | 4 | configs carried into the delay search |
| `--rounds` | 1 | local-refinement passes |
| `--prefilter` | off | use `sim.py` surrogate to pre-rank the grid |
| `--resume` | off | reuse `trials.jsonl` cache; only run new work |
| `--build` | off | `make` in `--dir` before searching |

## Outputs (`--workdir`, default `autotune_out/`)

- `BEST.md` — leaderboard + exact reproduce command per profile.
- `best_configs.json` — machine-readable winners (delay, worst miss/overhead, knobs).
- `trials.jsonl` — every trial; the resumable cache.

## A realistic full run

```bash
# rebuild, GPU pre-filter, robust: 3 profiles, 4 seeds, 15s runs, 2 refine rounds
python3 autotune.py --dir . --build --prefilter \
    --profiles A,B,C --seeds 4 --confirm-seeds 6 --duration 15 --rounds 2
```

Expect this to run many real trials; with 20 cores it parallelizes ~12-wide.
Watch progress in the console or tail `autotune_out/trials.jsonl`.
