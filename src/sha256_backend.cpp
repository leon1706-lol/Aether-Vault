// Scalar SHA-256 block compressor (always available) + backend dispatch/self-test.
//
// The scalar path below is behaviorally identical to the pre-V1.6.0 transform() (same full
// 64-word schedule, same round structure) -- only *where* the compression function lives
// changed (a free function selected via a backend pointer, instead of a fixed member
// function), to make room for the SHA-NI/ARM-crypto backends alongside it. Two micro-
// optimizations were tried and measured on this project's own reference machine (MSVC
// /O2, Intel Sandy Bridge) and rejected for lack of a real win: a 16-word circular
// message-schedule buffer measured slower (the extra `& 15` masking on every extended word
// cost more than the 192 bytes of stack it saved, which was never the bottleneck), and
// `load_be32()`'s single-bswap-intrinsic load was within noise of the original four-byte
// shift-and-or read on this compiler. `load_be32()` is kept and used below anyway -- it is
// bit-exact and a well-established win on GCC/Clang, just not proven to matter on MSVC's
// codegen for this specific loop; if a future measurement on a different compiler/CPU shows
// it doesn't help there either, remove it rather than carry unproven complexity. The actual
// path to a real throughput win here is hardware acceleration (SHA-NI/ARM SHA2, below), not
// further scalar micro-tuning -- see src/README.md.
//
// Correctness verified against NIST-known digests for "" and "abc" (tests/test_core.py, and
// the C++-internal `verified()` self-test below) plus randomized-length and boundary-size
// equality against hashlib (tests/test_core.py) -- SHA256::update()'s own bulk-copy-into-
// block logic is unchanged from V1.5.0, only what compresses a completed 64-byte block did.
#include "sha256_backend.h"

#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>

#if defined(_MSC_VER)
#include <cstdlib>  // _byteswap_ulong
#define AV_BSWAP32(x) _byteswap_ulong(x)
#elif defined(__GNUC__) || defined(__clang__)
#define AV_BSWAP32(x) __builtin_bswap32(x)
#endif

namespace {

inline uint32_t load_be32(const uint8_t* p) {
    // Every platform this project ships on (x86/x86-64, ARM/AArch64, on Windows/Linux/
    // macOS) is little-endian, so a byte-swapped native load is both correct and fast
    // there. The generic byte-assembly path is kept as a genuinely portable fallback for
    // any big-endian or unrecognized toolchain, where it's already correct without a swap.
#if defined(AV_BSWAP32) && (defined(__BYTE_ORDER__) ? __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__ : 1)
    uint32_t v;
    std::memcpy(&v, p, 4);
    return AV_BSWAP32(v);
#elif defined(AV_BSWAP32) && defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
    uint32_t v;
    std::memcpy(&v, p, 4);
    return v;
#else
    return (static_cast<uint32_t>(p[0]) << 24) | (static_cast<uint32_t>(p[1]) << 16) |
           (static_cast<uint32_t>(p[2]) << 8) | static_cast<uint32_t>(p[3]);
#endif
}

#define ROTRIGHT(a, b) (((a) >> (b)) | ((a) << (32 - (b))))
#define CH(x, y, z) (((x) & (y)) ^ (~(x) & (z)))
#define MAJ(x, y, z) (((x) & (y)) ^ ((x) & (z)) ^ ((y) & (z)))
#define EP0(x) (ROTRIGHT(x, 2) ^ ROTRIGHT(x, 13) ^ ROTRIGHT(x, 22))
#define EP1(x) (ROTRIGHT(x, 6) ^ ROTRIGHT(x, 11) ^ ROTRIGHT(x, 25))
#define SIG0(x) (ROTRIGHT(x, 7) ^ ROTRIGHT(x, 18) ^ ((x) >> 3))
#define SIG1(x) (ROTRIGHT(x, 17) ^ ROTRIGHT(x, 19) ^ ((x) >> 10))

const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

}  // namespace

void sha256_blocks_scalar(uint32_t state[8], const uint8_t block[64]) {
    // See this file's top-of-file comment for why this is a full 64-word schedule (not a
    // masked circular buffer) and why load_be32() is used without a proven throughput claim.
    uint32_t w[64];
    for (int i = 0; i < 16; ++i) w[i] = load_be32(block + i * 4);
    for (int i = 16; i < 64; ++i)
        w[i] = SIG1(w[i - 2]) + w[i - 7] + SIG0(w[i - 15]) + w[i - 16];

    uint32_t a = state[0], b = state[1], c = state[2], d = state[3];
    uint32_t e = state[4], f = state[5], g = state[6], h = state[7];

    for (int i = 0; i < 64; ++i) {
        uint32_t t1 = h + EP1(e) + CH(e, f, g) + K[i] + w[i];
        uint32_t t2 = EP0(a) + MAJ(a, b, c);
        h = g; g = f; f = e; e = d + t1;
        d = c; c = b; b = a; a = t1 + t2;
    }

    state[0] += a; state[1] += b; state[2] += c; state[3] += d;
    state[4] += e; state[5] += f; state[6] += g; state[7] += h;
}

// ---------------------------------------------------------------------------
// Backend dispatch + correctness self-test
// ---------------------------------------------------------------------------

namespace {

void pad_single_block(const uint8_t* msg, size_t len, uint8_t block[64]) {
    // Only correct for len <= 55 -- both self-test messages below are far shorter, and this
    // helper has no other caller (never a general-purpose padder).
    std::memset(block, 0, 64);
    std::memcpy(block, msg, len);
    block[len] = 0x80;
    uint64_t bitlen = static_cast<uint64_t>(len) * 8;
    for (int i = 0; i < 8; ++i) block[63 - i] = static_cast<uint8_t>(bitlen >> (8 * i));
}

void state_to_hex(const uint32_t state[8], char out[65]) {
    static const char* hexd = "0123456789abcdef";
    for (int w = 0; w < 8; ++w) {
        for (int byte = 0; byte < 4; ++byte) {
            uint8_t v = static_cast<uint8_t>(state[w] >> (24 - byte * 8));
            out[w * 8 + byte * 2] = hexd[v >> 4];
            out[w * 8 + byte * 2 + 1] = hexd[v & 0xF];
        }
    }
    out[64] = '\0';
}

bool digest_matches(Sha256BlockFn fn, const char* msg, size_t len, const char* expected_hex) {
    static const uint32_t IV[8] = {
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    };
    uint32_t state[8];
    std::memcpy(state, IV, sizeof(IV));
    uint8_t block[64];
    pad_single_block(reinterpret_cast<const uint8_t*>(msg), len, block);
    fn(state, block);
    char hex[65];
    state_to_hex(state, hex);
    return std::strcmp(hex, expected_hex) == 0;
}

// Two well-known single-block digests -- literal values below were produced by Python's
// `hashlib.sha256(b"").hexdigest()` / `hashlib.sha256(b"abc").hexdigest()` (OpenSSL-backed,
// the same oracle tests/test_core.py checks the whole C++ core against), not typed from
// memory, specifically to keep a transcription error here from silently defeating the very
// check meant to catch one. Deliberately not the C++ core's own hash_file/hash_bytes
// surface (those are what this self-test exists to protect) -- these are the independent
// oracle. Both messages fit in exactly one 64-byte block after padding, so a single
// sha256_blocks_*() call fully reproduces the standard algorithm's result for them, making
// this a real (if minimal) end-to-end check of each backend's compression function, not
// just "did it run without crashing".
bool verified(Sha256BlockFn fn) {
    if (fn == nullptr) return false;
    return digest_matches(fn, "", 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855") &&
           digest_matches(fn, "abc", 3, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
}

}  // namespace

Sha256BlockFn sha256_pick_backend(const char* forced_name, const char** name_out) {
    std::string want = forced_name ? forced_name : "";

    if (want == "scalar" && verified(sha256_blocks_scalar)) {
        *name_out = "scalar";
        return sha256_blocks_scalar;
    }
    if (want == "sha-ni" && sha256_shani_supported() && verified(sha256_blocks_shani)) {
        *name_out = "sha-ni";
        return sha256_blocks_shani;
    }
    if (want == "arm-sha2" && sha256_arm_supported() && verified(sha256_blocks_arm)) {
        *name_out = "arm-sha2";
        return sha256_blocks_arm;
    }

    // "auto", empty, or a forced name that failed detection/self-test -- normal
    // auto-detection, fastest-first, each candidate gated on both its capability check and
    // its own self-test before being trusted.
    if (sha256_shani_supported() && verified(sha256_blocks_shani)) {
        *name_out = "sha-ni";
        return sha256_blocks_shani;
    }
    if (sha256_arm_supported() && verified(sha256_blocks_arm)) {
        *name_out = "arm-sha2";
        return sha256_blocks_arm;
    }
    *name_out = "scalar";
    return sha256_blocks_scalar;
}
