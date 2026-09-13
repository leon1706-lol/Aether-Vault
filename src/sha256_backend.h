#pragma once
#include <cstdint>

// One 64-byte-block compressor. Mirrors sha256.h's Sha256BlockFn typedef (duplicated here
// as a plain function-pointer type, no C++ class dependency, so backend translation units
// don't need to include sha256.h) -- every backend implements this same signature. This is
// the natural granularity for SHA-NI too (one sha256rnds2/sha256msg1/sha256msg2 sequence
// already processes exactly one block), so dispatching per-block costs nothing extra over a
// hand-inlined loop and keeps every backend independently unit-testable.
using Sha256BlockFn = void (*)(uint32_t state[8], const uint8_t block[64]);

// Always available on every platform: portable, unrolled scalar implementation. This is
// also the reference implementation every other backend's correctness self-test is checked
// against, and the deliberate fallback whenever a faster backend fails detection or its
// self-test.
void sha256_blocks_scalar(uint32_t state[8], const uint8_t block[64]);

// x86(-64) SHA extensions (SHA-NI). Real (non-stub) only when this translation unit is
// compiled for x86/x86-64 -- see sha256_shani.cpp's top-of-file guard. `sha256_shani_supported()`
// checks CPUID for the SHA + SSSE3 + SSE4.1 feature bits this backend's intrinsics require;
// it returns false outright on a non-x86 build or a CPU lacking any of them.
bool sha256_shani_supported();
void sha256_blocks_shani(uint32_t state[8], const uint8_t block[64]);

// ARMv8-A cryptographic extension (SHA2). Real (non-stub) only when compiled for
// aarch64/arm64 -- see sha256_arm.cpp's top-of-file guard.
bool sha256_arm_supported();
void sha256_blocks_arm(uint32_t state[8], const uint8_t block[64]);

// Picks the fastest backend this CPU claims to support AND that passes a correctness
// self-test against known SHA-256 answers (sha256_backend.cpp) before anything is allowed
// to trust it -- belt-and-braces: a CPUID/HWCAP feature bit says a CPU *should* support an
// instruction, but the self-test is what actually proves this specific build+CPU
// combination produces the right digest, which matters most exactly when an accelerated
// path has never been execution-tested on the machine that built it (cross-compiling, or a
// CI/dev box without the target extension -- see src/README.md). A forced backend
// (`forced_name`, from AV_SHA256_BACKEND: "auto"/""/"scalar"/"sha-ni"/"arm-sha2") that is
// unknown, unsupported, or fails its self-test is ignored and detection proceeds normally
// -- a hashing path this central must never hard-fail because of an environment typo.
// Writes the chosen backend's name into `*name_out` ("scalar"/"sha-ni"/"arm-sha2").
Sha256BlockFn sha256_pick_backend(const char* forced_name, const char** name_out);
