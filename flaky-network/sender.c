/* SENDER — Reed-Solomon share diversity (default) + ARQ backstop.
 *
 * The relay draws an INDEPENDENT delay and drop for every packet, and a
 * frame's deadline is fixed at t0+DELAY_MS+i*20ms. The old design sent two
 * full 164B copies (min of 2 draws, miss ~= f^2) but two full copies cost
 * 2.05x, so 1-in-14 frames went out single — and those singles dominated
 * the miss budget, pinning the minimum delay near the network's dmax.
 *
 * RS mode instead splits every frame into K=4 data shares of 40B plus P=3
 * Cauchy parity shares: 7 packets x 43B = 301B/frame = 1.88x, NO skipped
 * frames, and the receiver reconstructs from ANY 4 of the 7. A frame is
 * missed only if >=4 of 7 independent draws fail (miss ~= 35 f^4 vs f^2),
 * which tolerates a much fatter delay tail AND up to 3 packets of a loss
 * burst — pushing the minimum valid delay below dmax on every profile.
 *
 * Wire format to relay (port 47001):
 *   share:      [u16be seq][u8 idx 0..6][40B share]              (43B)
 *   full frame: [u16be seq][0xFF][160B payload]                  (163B)
 * Feedback (port 47004): NACK [0x80][u32be seq][u8 have_mask].
 * The mask says which shares the receiver already holds, so the response is
 * only the (K - have) + 1 cheapest missing shares (43B each, independent
 * draws) instead of full frames — ~5x more recoveries per budget byte, which
 * is what makes ARQ actually work on burst-loss networks.
 *
 * Knobs (searchable by autotune):
 *   FLAKY_MODE  = rs | dup   (dup = legacy two-full-copies baseline)
 *   FLAKY_RS_P  = parity shares 1..3           (default 3)
 *   FLAKY_RS_SPREAD = ticks the shares are spread across (1..3): burst-loss
 *                     episodes are short, so clumps on different ticks cannot all die to one (default 1)
 *   FLAKY_SKIP  = dup mode: 1-in-SKIP frames sent single (default 14)
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
#define RING 4096
#define BUDGET 1.99         /* hard clamp: sent bytes <= BUDGET * frames*160 */
#define NACK_BUDGET 1.95    /* ARQ spends only while TOTAL sent <= 1.95x: the
                               proactive stream (<=1.885x by construction) is
                               never blocked, ARQ gets the real headroom, and
                               1.95 + receiver feedback (<0.05x) stays < 2.0 */

static uint8_t ring_buf[RING][PAYLOAD];
static uint32_t ring_seq[RING];
static int ring_used[RING];

static int out_fd;
static struct sockaddr_in relay;
static uint64_t bytes_sent = 0;
static uint32_t frames_seen = 0;

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void emit_raw(const uint8_t *pkt, size_t len, double cap) {
    if (bytes_sent + len > cap * (double)frames_seen * PAYLOAD) return;
    sendto(out_fd, pkt, len, 0, (struct sockaddr *)&relay, sizeof relay);
    bytes_sent += len;
}

static void emit_share_capped(uint32_t seq, int idx, const uint8_t *share,
                              double cap) {
    uint8_t pkt[3 + RS_SHARE];
    uint16_t be = htons((uint16_t)seq);
    memcpy(pkt, &be, 2);
    pkt[2] = (uint8_t)idx;
    memcpy(pkt + 3, share, RS_SHARE);
    emit_raw(pkt, sizeof pkt, cap);
}

static void emit_share(uint32_t seq, int idx, const uint8_t *share) {
    emit_share_capped(seq, idx, share, BUDGET);
}

static void emit_full(uint32_t seq, const uint8_t *payload, double cap) {
    uint8_t pkt[3 + PAYLOAD];
    uint16_t be = htons((uint16_t)seq);
    memcpy(pkt, &be, 2);
    pkt[2] = 0xFF;
    memcpy(pkt + 3, payload, PAYLOAD);
    emit_raw(pkt, sizeof pkt, cap);
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

/* shares scheduled for later ticks (FLAKY_RS_SPREAD): a burst-loss episode
 * is only a few packets long, so shares split across ticks can't all die to
 * one episode */
#define NPEND 64
static struct { double due; uint32_t seq; int idx; uint8_t sh[RS_SHARE]; }
    pend[NPEND];
static int npend = 0;

static void flush_due(double now) {
    int w = 0;
    for (int i = 0; i < npend; i++) {
        if (pend[i].due <= now) {
            emit_share(pend[i].seq, pend[i].idx, pend[i].sh);
        } else {
            if (w != i) pend[w] = pend[i];
            w++;
        }
    }
    npend = w;
}

int main(void) {
    rs_init();
    if (rs_selftest(4) != 0) { fprintf(stderr, "rs selftest FAILED\n"); return 1; }

    const char *mode = getenv("FLAKY_MODE") ? getenv("FLAKY_MODE") : "rs";
    int rs_mode = strcmp(mode, "dup") != 0;
    int P = getenv("FLAKY_RS_P") ? atoi(getenv("FLAKY_RS_P")) : 3;
    if (P < 1) P = 1;
    if (P > RS_PMAX) P = RS_PMAX;
    int SPREAD = getenv("FLAKY_RS_SPREAD") ? atoi(getenv("FLAKY_RS_SPREAD")) : 1;
    if (SPREAD < 1) SPREAD = 1;
    if (SPREAD > 3) SPREAD = 3;
    int SKIP = getenv("FLAKY_SKIP") ? atoi(getenv("FLAKY_SKIP")) : 14;
    if (SKIP < 2) SKIP = 2;

    int src_fd = udp_bind(47010);
    int fb_fd = udp_bind(47004);
    out_fd = socket(AF_INET, SOCK_DGRAM, 0);
    relay.sin_family = AF_INET;
    relay.sin_port = htons(47001);
    relay.sin_addr.s_addr = inet_addr("127.0.0.1");

    struct pollfd pfd[2] = {{src_fd, POLLIN, 0}, {fb_fd, POLLIN, 0}};
    uint8_t in[2048];

    for (;;) {
        int r = poll(pfd, 2, 2);
        if (npend) flush_due(now_s());
        if (r <= 0) continue;

        if (pfd[0].revents & POLLIN) {
            ssize_t n = recvfrom(src_fd, in, sizeof in, 0, NULL, NULL);
            if (n == 4 + PAYLOAD) {
                uint32_t seq;
                memcpy(&seq, in, 4);
                seq = ntohl(seq);
                if (seq + 1 > frames_seen) frames_seen = seq + 1;
                int slot = seq % RING;
                memcpy(ring_buf[slot], in + 4, PAYLOAD);
                ring_seq[slot] = seq;
                ring_used[slot] = 1;

                if (rs_mode) {
                    uint8_t parity[RS_PMAX][RS_SHARE];
                    rs_encode(in + 4, P, parity);
                    int total = RS_K + P;
                    double now = now_s();
                    for (int idx = 0; idx < total; idx++) {
                        const uint8_t *sh = idx < RS_K
                            ? in + 4 + idx * RS_SHARE : parity[idx - RS_K];
                        int tick = idx * SPREAD / total;  /* 0..SPREAD-1 */
                        if (tick == 0) {
                            emit_share(seq, idx, sh);
                        } else if (npend < NPEND) {
                            pend[npend].due = now + tick * 0.020;
                            pend[npend].seq = seq;
                            pend[npend].idx = idx;
                            memcpy(pend[npend].sh, sh, RS_SHARE);
                            npend++;
                        }
                    }
                } else {
                    emit_full(seq, in + 4, BUDGET);
                    if ((seq % SKIP) != (uint32_t)(SKIP - 1))
                        emit_full(seq, in + 4, BUDGET);
                }
            }
        }

        if (pfd[1].revents & POLLIN) {
            ssize_t n = recvfrom(fb_fd, in, sizeof in, 0, NULL, NULL);
            if ((n == 5 || n == 6) && in[0] == 0x80) {
                uint32_t seq;
                memcpy(&seq, in + 1, 4);
                seq = ntohl(seq);
                int slot = seq % RING;
                if (ring_used[slot] && ring_seq[slot] == seq) {
                    uint8_t have = n == 6 ? in[5] : 0;
                    /* all parities, incl. ones the proactive path never sent:
                     * a share the relay has not seen is a fresh draw */
                    uint8_t parity[RS_PMAX][RS_SHARE];
                    rs_encode(ring_buf[slot], RS_PMAX, parity);
                    int hn = __builtin_popcount(have & 0x7F);
                    int need = hn < RS_K ? RS_K - hn + 1 : 1;
                    for (int idx = 0; idx < RS_K + RS_PMAX && need > 0; idx++) {
                        if (have & (1u << idx)) continue;
                        const uint8_t *sh = idx < RS_K
                            ? ring_buf[slot] + idx * RS_SHARE
                            : parity[idx - RS_K];
                        uint64_t before = bytes_sent;
                        emit_share_capped(seq, idx, sh, NACK_BUDGET);
                        if (bytes_sent != before) need--;
                    }
                }
            }
        }
    }
    return 0;
}
