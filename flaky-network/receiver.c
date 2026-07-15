/* RECEIVER — RS share reassembly + immediate forward + RTT-gated ARQ.
 *
 * The player judges frame i only on ARRIVAL before a FIXED deadline
 * t0+DELAY_MS+i*20ms, so there is no jitter buffer: the instant a frame is
 * reconstructable it is forwarded, exactly once.
 *
 * Wire from the sender (via relay, port 47002):
 *   share:      [u16be seq][u8 idx 0..6][40B share]   -> collect; when any
 *               4 distinct shares of a frame are in, RS-decode and forward
 *   full frame: [u16be seq][0xFF][160B payload]       -> forward directly
 * The 16-bit wire seq is expanded RTP-style against the highest seq seen
 * (reordering is bounded by relay delay <1s = <50 frames << 32768).
 * rs_selftest() runs at startup and aborts on any codec fault, so a decode
 * bug can never silently forward wrong bytes (= scored miss).
 *
 * ARQ backstop (feedback on port 47003): a still-missing frame is NACKed
 * ([0x80][u32be seq]) only while a round trip can still beat its deadline;
 * RTT is a live EWMA, retries are spaced/capped, a token bucket bounds
 * feedback bytes. At tight delays ARQ stays silent and the RS shares carry
 * recovery alone.
 */
#include <arpa/inet.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "rs.h"

#define PAYLOAD 160
#define MAXF (1u << 20)
#define NACK_BUCKET_PER_S 60.0   /* 6B NACKs: worst-case feedback < 0.05x */
#define RB 1024                       /* reassembly ring: 20s of frames */

static uint8_t delivered[MAXF / 8];
static uint8_t nack_tries[MAXF];
static float nack_at[MAXF];
static uint8_t rtt_done[MAXF];

static struct {
    uint32_t seq;
    uint8_t have;                     /* bitmask of share idxs received */
    uint8_t active;
    uint8_t sh[RS_K + RS_PMAX][RS_SHARE];
} slots[RB];

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static int udp_bind(int port) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in a = {0};
    a.sin_family = AF_INET;
    a.sin_port = htons(port);
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    if (bind(fd, (struct sockaddr *)&a, sizeof a) < 0) { perror("bind"); exit(1); }
    return fd;
}

static int out_fd;
static struct sockaddr_in player, feedback;
static double t0, est_rtt = 0.070;

static void deliver(uint32_t seq, const uint8_t *payload, double now) {
    if (seq >= MAXF) return;
    if (nack_at[seq] != 0.0f && !rtt_done[seq]) {
        double s = (now - t0) - nack_at[seq];
        if (s > 0.001 && s < 1.0) est_rtt = 0.875 * est_rtt + 0.125 * s;
        rtt_done[seq] = 1;
    }
    if ((delivered[seq / 8] >> (seq % 8)) & 1) return;
    delivered[seq / 8] |= 1u << (seq % 8);
    if (getenv("FLAKY_DBG") && nack_tries[seq])
        fprintf(stderr, "DBG recov seq=%u tries=%d late=%s\n", seq,
                nack_tries[seq],
                now > t0 + (getenv("DELAY_MS") ? atof(getenv("DELAY_MS")) : 120)
                    / 1000.0 + seq * 0.020 ? "yes" : "no");
    uint8_t pkt[4 + PAYLOAD];
    uint32_t be = htonl(seq);
    memcpy(pkt, &be, 4);
    memcpy(pkt + 4, payload, PAYLOAD);
    sendto(out_fd, pkt, sizeof pkt, 0, (struct sockaddr *)&player, sizeof player);
}

int main(void) {
    rs_init();
    if (rs_selftest(4) != 0) { fprintf(stderr, "rs selftest FAILED\n"); return 1; }

    double delay_ms = getenv("DELAY_MS") ? atof(getenv("DELAY_MS")) : 120;
    int nack_max = getenv("FLAKY_NACK_TRIES") ? atoi(getenv("FLAKY_NACK_TRIES")) : 4;
    double respace = (getenv("FLAKY_NACK_RESPACE_MS")
                      ? atof(getenv("FLAKY_NACK_RESPACE_MS")) : 40.0) / 1000.0;
    /* frame i left the source at exactly t0+i*20ms, so "still missing at
     * t_i + nack_delay" is a clock decision — no need to wait for later
     * frames to arrive through the jitter to infer a gap */
    double nack_delay = (getenv("FLAKY_NACK_DELAY_MS")
                         ? atof(getenv("FLAKY_NACK_DELAY_MS")) : 85.0) / 1000.0;
    t0 = getenv("T0") ? atof(getenv("T0")) : now_s();
    double delay_s = delay_ms / 1000.0;
    if (delay_s < est_rtt) est_rtt = delay_s * 0.6;

    int in_fd = udp_bind(47002);
    out_fd = socket(AF_INET, SOCK_DGRAM, 0);
    player.sin_family = AF_INET; player.sin_port = htons(47020);
    player.sin_addr.s_addr = inet_addr("127.0.0.1");
    feedback.sin_family = AF_INET; feedback.sin_port = htons(47003);
    feedback.sin_addr.s_addr = inet_addr("127.0.0.1");

    uint32_t max_seq = 0, scan_lo = 0, seq_base = 0;
    int any = 0;
    double bucket = 20.0, bucket_t = now_s();

    struct pollfd pfd = {in_fd, POLLIN, 0};
    uint8_t in[2048];

    for (;;) {
        int r = poll(&pfd, 1, 5);
        double now = now_s();

        if (r > 0 && (pfd.revents & POLLIN)) {
            ssize_t n = recvfrom(in_fd, in, sizeof in, 0, NULL, NULL);
            uint32_t seq = 0;
            int got = 0;
            if (n >= 3) {
                uint16_t s16;
                memcpy(&s16, in, 2);
                seq = rs_expand_seq(ntohs(s16), &seq_base, &any);
            }
            if (n == 3 + PAYLOAD && in[2] == 0xFF) {         /* full frame */
                deliver(seq, in + 3, now);
                got = 1;
            } else if (n == 3 + RS_SHARE && in[2] < RS_K + RS_PMAX) {
                int idx = in[2];
                int sl = seq % RB;
                if (!slots[sl].active || slots[sl].seq != seq) {
                    slots[sl].seq = seq;
                    slots[sl].have = 0;
                    slots[sl].active = 1;
                }
                memcpy(slots[sl].sh[idx], in + 3, RS_SHARE);
                slots[sl].have |= 1u << idx;
                got = 1;
                if (__builtin_popcount(slots[sl].have) >= RS_K &&
                    seq < MAXF && !((delivered[seq / 8] >> (seq % 8)) & 1)) {
                    uint8_t pay[PAYLOAD];
                    if (rs_decode(slots[sl].sh, slots[sl].have, pay) == 0)
                        deliver(seq, pay, now);
                }
            }
            if (got && seq > max_seq) max_seq = seq;
        }

        if (!any) continue;
        bucket += (now - bucket_t) * NACK_BUCKET_PER_S;
        if (bucket > 40.0) bucket = 40.0;
        bucket_t = now;

        for (uint32_t i = scan_lo;
             t0 + i * 0.020 + nack_delay <= now && i + 1 < MAXF; i++) {
            double deadline = t0 + delay_s + i * 0.020;
            int done = ((delivered[i / 8] >> (i % 8)) & 1) || now > deadline;
            if (done) { if (i == scan_lo) scan_lo++; continue; }
            if (deadline - now < est_rtt * 1.15 + 0.004) continue;
            if (nack_tries[i] >= nack_max) continue;
            if (nack_at[i] != 0.0f && (now - t0) - nack_at[i] < respace) continue;
            if (bucket < 1.0) break;
            bucket -= 1.0;
            /* tell the sender which shares we already hold so it resends
             * only the missing ones */
            int sl = i % RB;
            uint8_t have = (slots[sl].active && slots[sl].seq == i)
                               ? slots[sl].have : 0;
            uint8_t nk[6] = {0x80};
            uint32_t be = htonl(i);
            memcpy(nk + 1, &be, 4);
            nk[5] = have;
            sendto(out_fd, nk, sizeof nk, 0, (struct sockaddr *)&feedback,
                   sizeof feedback);
            nack_tries[i]++;
            nack_at[i] = (float)(now - t0);
            if (getenv("FLAKY_DBG"))
                fprintf(stderr, "DBG nack seq=%u try=%d have=%02x headroom=%.0fms rtt=%.0fms\n",
                        i, nack_tries[i], have, (deadline - now) * 1000,
                        est_rtt * 1000);
        }
    }
    return 0;
}
