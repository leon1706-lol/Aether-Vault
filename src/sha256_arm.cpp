// ARMv8-A cryptographic extension (SHA2) backend.
//
// STATUS: capability detection is real and functional; the accelerated compression kernel
// itself is deliberately NOT implemented yet. This backend always reports "unsupported"
// (`sha256_arm_supported()` returns false unconditionally), so `sha256_pick_backend()`
// never selects it -- every ARM64 build runs the portable scalar backend, correct but not
// hardware-accelerated, until this is finished.
//
// Why defer rather than ship a best-effort attempt: unlike x86 SHA-NI (also unverified by
// execution on the reference dev machine, but at least compiled and reviewed here), this
// project's toolchain cannot compile a single line of ARM NEON/crypto intrinsics on this
// development machine at all -- there is no way to catch even a syntax error, let alone a
// wrong operand to `vsha256su0q_u32`/`vsha256su1q_u32` (whose two-operand shape differs
// from x86's SHA256MSG1/SHA256MSG2 pairing in a way this session could not work through
// without being able to compile-check any of it). `sha256_pick_backend()`'s mandatory
// self-test would still catch a wrong digest and fall back to scalar safely, but shipping
// 16 groups of genuinely unreviewable intrinsic calls provides no real value over shipping
// nothing until it can be built and checked on real ARM64 hardware or in ARM64 CI (the
// `linux/arm64` Docker image build, or a macOS Apple Silicon cibuildwheel run) -- tracked
// as explicit follow-up work, not silently dropped.
#include "sha256_backend.h"

#if defined(__aarch64__) || defined(_M_ARM64)
#define AV_SHA256_ARM_TARGET 1
#endif

#if AV_SHA256_ARM_TARGET

#if defined(__APPLE__)
#include <sys/sysctl.h>
#else
#include <sys/auxv.h>
#ifndef HWCAP_SHA2
#define HWCAP_SHA2 (1 << 6)
#endif
#endif

namespace {

// Real detection, kept and exercised (it's plain, checkable C++ with no intrinsics) so
// `av doctor`/future work can see "this CPU has the extension" independently of whether
// this backend is wired up to use it yet.
bool cpu_supports_arm_sha2() {
#if defined(__APPLE__)
    int supported = 0;
    size_t size = sizeof(supported);
    if (sysctlbyname("hw.optional.arm.FEAT_SHA256", &supported, &size, nullptr, 0) == 0) {
        return supported != 0;
    }
    return true;  // sysctl itself unavailable (older macOS) -- every shipped M-series has it
#else
    unsigned long hwcap = getauxval(AT_HWCAP);
    return (hwcap & HWCAP_SHA2) != 0;
#endif
}

}  // namespace

bool sha256_arm_supported() {
    (void)cpu_supports_arm_sha2();  // detection wired up; gate stays closed until WS3 follow-up lands the kernel
    return false;
}

void sha256_blocks_arm(uint32_t*, const uint8_t*) {
    // Never called: sha256_arm_supported() always returns false, and sha256_pick_backend()
    // only ever calls this after that check passes.
}

#else  // !AV_SHA256_ARM_TARGET -- non-ARM64 build: safe stubs, never selected.

bool sha256_arm_supported() { return false; }
void sha256_blocks_arm(uint32_t*, const uint8_t*) {}

#endif
