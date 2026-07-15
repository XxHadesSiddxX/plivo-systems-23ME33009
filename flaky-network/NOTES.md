# NOTES

The sender splits every 160 B frame into 4 data shares + 3 Cauchy Reed-Solomon
parity shares and sends all 7 (43 B each, 1.88x) at the same instant; because
the relay impairs each packet independently, the receiver — which RS-decodes
from ANY 4 shares and forwards immediately, no jitter buffer — only misses a
frame when ≥4 of 7 independent draws fail (miss ≈ 35·f⁴ vs f² for a duplicate),
so the minimum valid delay drops *below* the network's max one-way delay.
Every frame is protected (no 2.05x full-copy skip compromise), a 3-packet loss
burst is absorbed by construction, and both binaries prove the codec at startup
(exhaustive all-subset self-test, abort on fault) so a decode bug can never
forward wrong bytes. A clock-triggered ARQ backstop NACKs a frame still missing
at t_i+85 ms with a bitmask of held shares, and the sender returns only the
missing shares (fresh independent draws) while a live RTT estimate says a round
trip still beats the deadline; retransmits cap at 1.95x total and feedback is
token-bucketed, so worst-case bytes stay under 2.0x. Confirmed floors
(worst seed across 5, 15 s runs, parallel harness): profile A VALID at
**38 ms** (0.27% miss, 1.88x), profile B VALID at **77 ms**.
**Grade at `--delay_ms 77`**; on milder (A-like) networks the same binary is
valid down to ≈38 ms. What breaks it: sustained heavy burst loss (~15%
effective, Gilbert-Elliott) exceeds 1% at any delay — share-spreading
(`FLAKY_RS_SPREAD`) plus mask-ARQ cut a synthetic such profile from 5–9% to
~2.5%, but under 1% needs more than the 2x budget — a budget ceiling, not a
delay ceiling.
