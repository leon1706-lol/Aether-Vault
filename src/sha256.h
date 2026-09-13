#pragma once
#include <cstdint>
#include <string>
#include <vector>

// Compresses exactly one 64-byte SHA-256 block into `state`. Every backend (scalar,
// x86 SHA-NI, ARMv8 crypto) implements this same one-block-at-a-time signature -- it is
// the natural granularity for SHA-NI too (one `sha256rnds2`/`sha256msg1`/`sha256msg2`
// sequence already processes one block), so dispatching per-block costs nothing extra
// over a hand-inlined loop and keeps every backend interchangeable and unit-testable in
// isolation. See sha256_backend.h for the dispatch/selection logic.
using Sha256BlockFn = void (*)(uint32_t state[8], const uint8_t block[64]);

class SHA256 {
public:
    SHA256();
    void update(const uint8_t * data, size_t length);
    void update(const std::string &data);
    uint8_t* digest();
    static std::string toString(const uint8_t * digest);
    std::string hexdigest();
    static std::string hash_bytes(const uint8_t * data, size_t length);
    static std::string hash_bytes(const std::string &data);

    // Name of the currently selected backend ("scalar", "sha-ni", "arm-sha2"). Resolved
    // once per process (see sha256.cpp's ensure_backend_ready()) unless overridden.
    static std::string backend_name();
    // Forces a specific backend by name for the rest of the process; returns false (and
    // leaves the current backend unchanged) if the name is unknown or unsupported/failed
    // its correctness self-test on this machine. "auto" reverts to normal detection.
    // Test/diagnostic hook (AV_SHA256_BACKEND) -- never called from a real hashing path.
    static bool set_backend(const char* name);

private:
    uint8_t data[64];
    uint32_t datalen;
    unsigned long long bitlen;
    uint32_t state[8];
    uint8_t m_hash[32];
    Sha256BlockFn block_fn;
    void transform();
};
