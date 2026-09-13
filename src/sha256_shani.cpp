// x86(-64) SHA extensions (SHA-NI) backend. Guarded to compile a real implementation only
// on x86/x86-64 targets; on any other architecture this file compiles to a stub that always
// reports "unsupported" so callers never even attempt to invoke it.
//
// CAUTION (read before touching this file): this backend has been reviewed and its
// self-test (sha256_backend.cpp's `verified()`) passes structurally, but it has NOT been
// execution-verified on real SHA-NI hardware in this development session -- the reference
// machine (Intel Sandy Bridge, Family 6 Model 42) predates SHA-NI by several CPU
// generations and cannot execute these instructions at all. Correctness on real hardware
// rests on `sha256_pick_backend()`'s mandatory runtime self-test (two known digests,
// computed independently via Python's hashlib -- see sha256_backend.cpp), which this
// backend can only ever be selected after passing: if the intrinsics below are wrong in any
// way, self-test failure silently falls back to the scalar backend rather than producing a
// wrong content hash. Before trusting this path in production, run
// `tests/test_core.py::test_sha256_scalar_and_accelerated_backends_agree` (and ideally
// `AV_SHA256_BACKEND=sha-ni av doctor`) on a real SHA-NI-capable machine and confirm
// `aether_core.hash_backend() == "sha-ni"` there.
#include "sha256_backend.h"

#if defined(__x86_64__) || defined(__i386__) || defined(_M_X64) || defined(_M_IX86)
#define AV_SHA256_SHANI_TARGET 1
#endif

#if AV_SHA256_SHANI_TARGET

#include <cstring>

#if defined(_MSC_VER)
#include <intrin.h>
#else
#include <cpuid.h>
#include <immintrin.h>
#endif

namespace {

bool cpu_supports_sha_ni() {
#if defined(_MSC_VER)
    int regs1[4] = {0, 0, 0, 0};
    __cpuid(regs1, 1);
    bool sse41 = (regs1[2] & (1 << 19)) != 0;   // ECX bit 19
    bool ssse3 = (regs1[2] & (1 << 9)) != 0;    // ECX bit 9
    int regs7[4] = {0, 0, 0, 0};
    __cpuidex(regs7, 7, 0);
    bool sha = (regs7[1] & (1 << 29)) != 0;     // leaf 7 EBX bit 29
    return sse41 && ssse3 && sha;
#else
    unsigned int eax, ebx, ecx, edx;
    if (!__get_cpuid(1, &eax, &ebx, &ecx, &edx)) return false;
    bool sse41 = (ecx & (1u << 19)) != 0;
    bool ssse3 = (ecx & (1u << 9)) != 0;
    unsigned int eax7 = 0, ebx7 = 0, ecx7 = 0, edx7 = 0;
    if (!__get_cpuid_count(7, 0, &eax7, &ebx7, &ecx7, &edx7)) return false;
    bool sha = (ebx7 & (1u << 29)) != 0;
    return sse41 && ssse3 && sha;
#endif
}

}  // namespace

bool sha256_shani_supported() {
    static const bool supported = cpu_supports_sha_ni();
    return supported;
}

#if defined(_MSC_VER)
// MSVC exposes the SHA/SSE4.1/SSSE3 intrinsics unconditionally via <intrin.h> -- no
// per-function or per-TU compile flag needed; the CPUID check above is what keeps this from
// ever running on a CPU that can't execute the instructions.
void sha256_blocks_shani(uint32_t state[8], const uint8_t block[64]) {
#else
// GCC/Clang: rather than requiring this whole translation unit be compiled with -msha
// (which would make the resulting object illegal to even load on a non-SHA CPU on some
// toolchains), gate the intrinsics on this one function via a target attribute -- the rest
// of the binary, including this file's own cpu_supports_sha_ni() above, stays baseline
// x86-64. GCC >= 5 and Clang >= 3.8 support function-level `target("sha,sse4.1,ssse3")`.
__attribute__((target("sha,sse4.1,ssse3")))
void sha256_blocks_shani(uint32_t state[8], const uint8_t block[64]) {
#endif
    // Intel's public-domain single-block SHA-256 transform (the reference implementation
    // distributed in Intel's "sha256_ni.asm"/whitepaper and widely reused, e.g.
    // noloader/SHA-Intrinsics on GitHub) rewritten against this project's one-block
    // Sha256BlockFn signature. STATE0/STATE1 pack the 8 state words as {A,B,E,F}/{C,D,G,H}
    // in the specific shuffled lane order SHA256RNDS2 expects; MASK byte-swaps each loaded
    // message dword from little-endian memory order into the big-endian order SHA-256's
    // message schedule requires.
    const __m128i MASK = _mm_set_epi64x(0x0c0d0e0f08090a0bULL, 0x0405060700010203ULL);

    __m128i STATE0 = _mm_loadu_si128(reinterpret_cast<const __m128i*>(&state[0]));
    __m128i STATE1 = _mm_loadu_si128(reinterpret_cast<const __m128i*>(&state[4]));
    __m128i TMP = _mm_shuffle_epi32(STATE0, 0xB1);       // CDAB
    STATE1 = _mm_shuffle_epi32(STATE1, 0x1B);            // EFGH
    STATE0 = _mm_alignr_epi8(TMP, STATE1, 8);            // ABEF
    STATE1 = _mm_blend_epi16(STATE1, TMP, 0xF0);         // CDGH
    const __m128i ABEF_SAVE = STATE0;
    const __m128i CDGH_SAVE = STATE1;

    static const uint32_t Kx[64] = {
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    };

    __m128i MSG0 = _mm_shuffle_epi8(_mm_loadu_si128(reinterpret_cast<const __m128i*>(block + 0)), MASK);
    __m128i MSG1 = _mm_shuffle_epi8(_mm_loadu_si128(reinterpret_cast<const __m128i*>(block + 16)), MASK);
    __m128i MSG2 = _mm_shuffle_epi8(_mm_loadu_si128(reinterpret_cast<const __m128i*>(block + 32)), MASK);
    __m128i MSG3 = _mm_shuffle_epi8(_mm_loadu_si128(reinterpret_cast<const __m128i*>(block + 48)), MASK);

    __m128i MSG, TMP2;

    // Every group of 4 rounds does the same two things: (1) two SHA256RNDS2 calls advancing
    // STATE0/STATE1 using the K-adjusted message word whose index is `group % 4`, and, for
    // every group except the last time each word index is "current" (i.e. every group
    // except the last 3), (2) extends the message word 4 groups ahead (index `(group+1)%4`)
    // via alignr+add+SHA256MSG2, then folds the just-consumed word into a partial extension
    // via SHA256MSG1 for its own future extension 4 groups after that. Written out fully
    // explicitly (not as a parameterized loop) specifically because getting this index
    // cycling wrong is easy and, unlike almost everything else in this codebase, NOT
    // something a first attempt here could be execution-verified against on the reference
    // development machine (see this file's top-of-file caution) -- explicit code is
    // grep-able and diffable against the reference algorithm one group at a time.
#define AV_SHA_RND(msgword, k_off) \
    MSG = _mm_add_epi32((msgword), _mm_loadu_si128(reinterpret_cast<const __m128i*>(&Kx[(k_off)]))); \
    STATE1 = _mm_sha256rnds2_epu32(STATE1, STATE0, MSG); \
    MSG = _mm_shuffle_epi32(MSG, 0x0E); \
    STATE0 = _mm_sha256rnds2_epu32(STATE0, STATE1, MSG)
#define AV_SHA_EXTEND(dst, prev, cur) \
    TMP2 = _mm_alignr_epi8((cur), (prev), 4); \
    (dst) = _mm_add_epi32((dst), TMP2); \
    (dst) = _mm_sha256msg2_epu32((dst), (cur))

    // group 0 (rounds 0-3): current = MSG0
    AV_SHA_RND(MSG0, 0);
    // group 1 (rounds 4-7): current = MSG1
    AV_SHA_RND(MSG1, 4);
    MSG0 = _mm_sha256msg1_epu32(MSG0, MSG1);
    // group 2 (rounds 8-11): current = MSG2
    AV_SHA_RND(MSG2, 8);
    MSG1 = _mm_sha256msg1_epu32(MSG1, MSG2);
    // group 3 (rounds 12-15): current = MSG3; extend MSG0 (next current in 4 groups)
    AV_SHA_EXTEND(MSG0, MSG2, MSG3);
    AV_SHA_RND(MSG3, 12);
    MSG2 = _mm_sha256msg1_epu32(MSG2, MSG3);
    // group 4 (rounds 16-19): current = MSG0; extend MSG1
    AV_SHA_EXTEND(MSG1, MSG3, MSG0);
    AV_SHA_RND(MSG0, 16);
    MSG3 = _mm_sha256msg1_epu32(MSG3, MSG0);
    // group 5 (rounds 20-23): current = MSG1; extend MSG2
    AV_SHA_EXTEND(MSG2, MSG0, MSG1);
    AV_SHA_RND(MSG1, 20);
    MSG0 = _mm_sha256msg1_epu32(MSG0, MSG1);
    // group 6 (rounds 24-27): current = MSG2; extend MSG3
    AV_SHA_EXTEND(MSG3, MSG1, MSG2);
    AV_SHA_RND(MSG2, 24);
    MSG1 = _mm_sha256msg1_epu32(MSG1, MSG2);
    // group 7 (rounds 28-31): current = MSG3; extend MSG0
    AV_SHA_EXTEND(MSG0, MSG2, MSG3);
    AV_SHA_RND(MSG3, 28);
    MSG2 = _mm_sha256msg1_epu32(MSG2, MSG3);
    // group 8 (rounds 32-35): current = MSG0; extend MSG1
    AV_SHA_EXTEND(MSG1, MSG3, MSG0);
    AV_SHA_RND(MSG0, 32);
    MSG3 = _mm_sha256msg1_epu32(MSG3, MSG0);
    // group 9 (rounds 36-39): current = MSG1; extend MSG2
    AV_SHA_EXTEND(MSG2, MSG0, MSG1);
    AV_SHA_RND(MSG1, 36);
    MSG0 = _mm_sha256msg1_epu32(MSG0, MSG1);
    // group 10 (rounds 40-43): current = MSG2; extend MSG3
    AV_SHA_EXTEND(MSG3, MSG1, MSG2);
    AV_SHA_RND(MSG2, 40);
    MSG1 = _mm_sha256msg1_epu32(MSG1, MSG2);
    // group 11 (rounds 44-47): current = MSG3; extend MSG0
    AV_SHA_EXTEND(MSG0, MSG2, MSG3);
    AV_SHA_RND(MSG3, 44);
    MSG2 = _mm_sha256msg1_epu32(MSG2, MSG3);
    // group 12 (rounds 48-51): current = MSG0; extend MSG1 (its LAST extension -- MSG1 is
    // current one final time at group 13, then never again, so no further msg1 folding of
    // MSG0 is needed after this).
    AV_SHA_EXTEND(MSG1, MSG3, MSG0);
    AV_SHA_RND(MSG0, 48);
    MSG3 = _mm_sha256msg1_epu32(MSG3, MSG0);
    // group 13 (rounds 52-55): current = MSG1; extend MSG2 (its last extension)
    AV_SHA_EXTEND(MSG2, MSG0, MSG1);
    AV_SHA_RND(MSG1, 52);
    // group 14 (rounds 56-59): current = MSG2; extend MSG3 (its last extension)
    AV_SHA_EXTEND(MSG3, MSG1, MSG2);
    AV_SHA_RND(MSG2, 56);
    // group 15 (rounds 60-63): current = MSG3; no further extension needed -- this is the
    // last group, nothing will ever read a "next" message word again.
    AV_SHA_RND(MSG3, 60);

#undef AV_SHA_RND
#undef AV_SHA_EXTEND

    STATE0 = _mm_add_epi32(STATE0, ABEF_SAVE);
    STATE1 = _mm_add_epi32(STATE1, CDGH_SAVE);

    TMP = _mm_shuffle_epi32(STATE0, 0x1B);        // FEBA
    STATE1 = _mm_shuffle_epi32(STATE1, 0xB1);     // DCHG
    STATE0 = _mm_blend_epi16(TMP, STATE1, 0xF0);  // DCBA
    STATE1 = _mm_alignr_epi8(STATE1, TMP, 8);     // ABEF -> HGFE via alignment

    _mm_storeu_si128(reinterpret_cast<__m128i*>(&state[0]), STATE0);
    _mm_storeu_si128(reinterpret_cast<__m128i*>(&state[4]), STATE1);
}

#else  // !AV_SHA256_SHANI_TARGET -- non-x86 build: safe stubs, never selected.

bool sha256_shani_supported() { return false; }
void sha256_blocks_shani(uint32_t*, const uint8_t*) {}

#endif
