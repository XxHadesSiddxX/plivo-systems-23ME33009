/* rs.h — GF(256) Cauchy Reed-Solomon erasure code shared by sender/receiver.
 *
 * A 160B frame is split into K=4 data shares of 40B; up to P=3 parity shares
 * are Cauchy-matrix combinations of the data shares. The 4+P shares form a
 * systematic MDS code: ANY 4 distinct shares reconstruct the frame (every
 * square submatrix of a Cauchy matrix is invertible, so every 4-row selection
 * of [I4 ; C] is invertible). rs_selftest() proves this exhaustively at
 * startup — all subsets, all P — so a codec bug aborts instead of ever
 * forwarding wrong bytes to the player.
 */
#ifndef RS_H
#define RS_H
#include <stdint.h>
#include <string.h>

#define RS_K 4
#define RS_PMAX 3
#define RS_SHARE 40
#define RS_PAYLOAD 160

static uint8_t gf_exp_[512], gf_log_[256];

static void rs_init(void) {
    int x = 1;
    for (int i = 0; i < 255; i++) {
        gf_exp_[i] = (uint8_t)x;
        gf_log_[x] = (uint8_t)i;
        x <<= 1;
        if (x & 0x100) x ^= 0x11D;
    }
    for (int i = 255; i < 512; i++) gf_exp_[i] = gf_exp_[i - 255];
}

static inline uint8_t gf_mul(uint8_t a, uint8_t b) {
    if (!a || !b) return 0;
    return gf_exp_[gf_log_[a] + gf_log_[b]];
}

static inline uint8_t gf_inv(uint8_t a) {
    return gf_exp_[255 - gf_log_[a]];
}

/* Cauchy coefficient for parity row r (0..P-1), data column j (0..3):
 * 1/(x_r + y_j) over GF(256) with x_r = r+4, y_j = j (disjoint, so never 0). */
static inline uint8_t rs_coef(int r, int j) {
    return gf_inv((uint8_t)((r + 4) ^ j));
}

/* payload[160] -> parity[r][40] for r in 0..P-1 */
static void rs_encode(const uint8_t *payload, int P,
                      uint8_t parity[RS_PMAX][RS_SHARE]) {
    for (int r = 0; r < P; r++)
        for (int b = 0; b < RS_SHARE; b++) {
            uint8_t v = 0;
            for (int j = 0; j < RS_K; j++)
                v ^= gf_mul(rs_coef(r, j), payload[j * RS_SHARE + b]);
            parity[r][b] = v;
        }
}

/* sh[idx][40] for idx 0..6 (0..3 data, 4..6 parity), `have` = bitmask of
 * received idxs. Reconstructs payload[160] from the first 4 available shares
 * by Gauss-Jordan over GF(256). Returns 0 on success, -1 if <4 shares. */
static int rs_decode(uint8_t sh[][RS_SHARE], unsigned have, uint8_t *payload) {
    int sel[RS_K], ns = 0;
    for (int i = 0; i < RS_K + RS_PMAX && ns < RS_K; i++)
        if (have & (1u << i)) sel[ns++] = i;
    if (ns < RS_K) return -1;

    uint8_t m[RS_K][RS_K], buf[RS_K][RS_SHARE];
    for (int r = 0; r < RS_K; r++) {
        memcpy(buf[r], sh[sel[r]], RS_SHARE);
        for (int j = 0; j < RS_K; j++)
            m[r][j] = sel[r] < RS_K ? (uint8_t)(sel[r] == j)
                                    : rs_coef(sel[r] - RS_K, j);
    }
    for (int c = 0; c < RS_K; c++) {
        int p = -1;
        for (int r = c; r < RS_K; r++)
            if (m[r][c]) { p = r; break; }
        if (p < 0) return -1;
        if (p != c) {
            uint8_t tm[RS_K], tb[RS_SHARE];
            memcpy(tm, m[p], RS_K); memcpy(m[p], m[c], RS_K); memcpy(m[c], tm, RS_K);
            memcpy(tb, buf[p], RS_SHARE); memcpy(buf[p], buf[c], RS_SHARE);
            memcpy(buf[c], tb, RS_SHARE);
        }
        uint8_t iv = gf_inv(m[c][c]);
        for (int j = 0; j < RS_K; j++) m[c][j] = gf_mul(m[c][j], iv);
        for (int b = 0; b < RS_SHARE; b++) buf[c][b] = gf_mul(buf[c][b], iv);
        for (int r = 0; r < RS_K; r++) {
            if (r == c || !m[r][c]) continue;
            uint8_t f = m[r][c];
            for (int j = 0; j < RS_K; j++) m[r][j] ^= gf_mul(f, m[c][j]);
            for (int b = 0; b < RS_SHARE; b++) buf[r][b] ^= gf_mul(f, buf[c][b]);
        }
    }
    for (int j = 0; j < RS_K; j++)
        memcpy(payload + j * RS_SHARE, buf[j], RS_SHARE);
    return 0;
}

/* Expand a 16-bit wire seq to 32 bits, RTP-style: pick the value congruent
 * mod 2^16 that is nearest the highest seq seen. Reordering here is bounded
 * by relay delay (<1s = <50 frames), far inside the ±32768 window. */
static uint32_t rs_expand_seq(uint16_t s, uint32_t *base, int *have_any) {
    if (!*have_any) { *have_any = 1; *base = s; return s; }
    uint32_t e = (*base & 0xFFFF0000u) | s;
    if (e + 0x8000u < *base) e += 0x10000u;
    else if (e > *base + 0x8000u && e >= 0x10000u) e -= 0x10000u;
    if (e > *base) *base = e;
    return e;
}

/* Exhaustive proof: every subset of >=4 shares, for every P, reconstructs
 * every one of `reps` pseudo-random payloads exactly. Returns 0 iff perfect. */
static int rs_selftest(int reps) {
    uint32_t rng = 0x12345678u;
    for (int rep = 0; rep < reps; rep++) {
        uint8_t pay[RS_PAYLOAD], out[RS_PAYLOAD];
        uint8_t sh[RS_K + RS_PMAX][RS_SHARE];
        for (int i = 0; i < RS_PAYLOAD; i++) {
            rng = rng * 1664525u + 1013904223u;
            pay[i] = (uint8_t)(rng >> 24);
        }
        uint8_t parity[RS_PMAX][RS_SHARE];
        rs_encode(pay, RS_PMAX, parity);
        for (int j = 0; j < RS_K; j++)
            memcpy(sh[j], pay + j * RS_SHARE, RS_SHARE);
        for (int r = 0; r < RS_PMAX; r++)
            memcpy(sh[RS_K + r], parity[r], RS_SHARE);
        for (int P = 0; P <= RS_PMAX; P++) {
            int n = RS_K + P;
            for (unsigned mask = 0; mask < (1u << n); mask++) {
                if (__builtin_popcount(mask) < RS_K) continue;
                memset(out, 0, sizeof out);
                if (rs_decode(sh, mask, out) != 0) return -1;
                if (memcmp(out, pay, RS_PAYLOAD) != 0) return -1;
            }
        }
    }
    return 0;
}

#endif /* RS_H */
