#include "sha256.h"
#include "sha256_backend.h"
#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <mutex>
#include <sstream>

namespace {
// Resolved once per process (first SHA256 construction), or re-resolved on demand via
// SHA256::set_backend() -- see sha256_backend.h's sha256_pick_backend() for the
// capability-check + mandatory correctness self-test this goes through before anything is
// allowed to trust an accelerated backend.
std::mutex g_backend_mutex;
Sha256BlockFn g_backend_fn = nullptr;
std::string g_backend_name = "scalar";
bool g_backend_ready = false;

void ensure_backend_ready() {
    std::lock_guard<std::mutex> lock(g_backend_mutex);
    if (g_backend_ready) return;
    const char* env = std::getenv("AV_SHA256_BACKEND");
    const char* name = nullptr;
    g_backend_fn = sha256_pick_backend(env, &name);
    g_backend_name = name ? name : "scalar";
    g_backend_ready = true;
}
}  // namespace

SHA256::SHA256() {
    datalen = 0;
    bitlen = 0;
    state[0] = 0x6a09e667;
    state[1] = 0xbb67ae85;
    state[2] = 0x3c6ef372;
    state[3] = 0xa54ff53a;
    state[4] = 0x510e527f;
    state[5] = 0x9b05688c;
    state[6] = 0x1f83d9ab;
    state[7] = 0x5be0cd19;
    ensure_backend_ready();
    std::lock_guard<std::mutex> lock(g_backend_mutex);
    block_fn = g_backend_fn;
}

void SHA256::transform() {
    block_fn(state, data);
}

std::string SHA256::backend_name() {
    ensure_backend_ready();
    std::lock_guard<std::mutex> lock(g_backend_mutex);
    return g_backend_name;
}

bool SHA256::set_backend(const char* name) {
    const char* resolved = nullptr;
    Sha256BlockFn fn = sha256_pick_backend(name, &resolved);
    std::string want = name ? name : "";
    // sha256_pick_backend() silently falls back on an unknown/unsupported/failing name
    // rather than erroring (a real hashing path must never hard-fail on an environment
    // typo) -- to make this diagnostic entry point observably report that fallback as a
    // failure, compare what was asked for against what was actually resolved.
    bool ok = want.empty() || want == "auto" || want == resolved;
    std::lock_guard<std::mutex> lock(g_backend_mutex);
    g_backend_fn = fn;
    g_backend_name = resolved;
    g_backend_ready = true;
    return ok;
}

void SHA256::update(const uint8_t * data_in, size_t length) {
    // V1.5.0 perf work: this used to copy one byte at a time (a branch + a store per byte)
    // -- the single largest pure-CPU cost in the whole C++ core, since every hash in the
    // system (whole-file, per-chunk, per-layer) flows through here. Same three-phase shape
    // as any streaming hash's bulk update: top up a pending partial block, then memcpy+
    // transform whole 64-byte blocks straight out of the caller's buffer, then buffer
    // whatever's left over. `transform()` itself (the actual SHA-256 compression function)
    // and its output are completely unchanged -- this only changes how bytes get into
    // `data[]` before it, so the digest for any given input is bit-for-bit identical to
    // before (see tests/test_core.py's randomized-chunk-split property test).
    size_t i = 0;
    if (datalen > 0) {
        size_t need = 64 - datalen;
        size_t take = std::min(need, length);
        memcpy(data + datalen, data_in, take);
        datalen += static_cast<uint32_t>(take);
        i += take;
        if (datalen == 64) {
            transform();
            bitlen += 512;
            datalen = 0;
        }
    }
    while (length - i >= 64) {
        memcpy(data, data_in + i, 64);
        transform();
        bitlen += 512;
        i += 64;
    }
    if (i < length) {
        memcpy(data, data_in + i, length - i);
        datalen = static_cast<uint32_t>(length - i);
    }
}

void SHA256::update(const std::string &data) {
    update(reinterpret_cast<const uint8_t*>(data.c_str()), data.length());
}

uint8_t* SHA256::digest() {
    uint32_t i = datalen;
    if (datalen < 56) {
        data[i++] = 0x80;
        while (i < 56) data[i++] = 0x00;
    } else {
        data[i++] = 0x80;
        while (i < 64) data[i++] = 0x00;
        transform();
        memset(data, 0, 56);
    }
    bitlen += datalen * 8;
    data[63] = bitlen; data[62] = bitlen >> 8; data[61] = bitlen >> 16; data[60] = bitlen >> 24;
    data[59] = bitlen >> 32; data[58] = bitlen >> 40; data[57] = bitlen >> 48; data[56] = bitlen >> 56;
    transform();
    for (i = 0; i < 4; ++i) {
        m_hash[i]      = (state[0] >> (24 - i * 8)) & 0x000000ff;
        m_hash[i + 4]  = (state[1] >> (24 - i * 8)) & 0x000000ff;
        m_hash[i + 8]  = (state[2] >> (24 - i * 8)) & 0x000000ff;
        m_hash[i + 12] = (state[3] >> (24 - i * 8)) & 0x000000ff;
        m_hash[i + 16] = (state[4] >> (24 - i * 8)) & 0x000000ff;
        m_hash[i + 20] = (state[5] >> (24 - i * 8)) & 0x000000ff;
        m_hash[i + 24] = (state[6] >> (24 - i * 8)) & 0x000000ff;
        m_hash[i + 28] = (state[7] >> (24 - i * 8)) & 0x000000ff;
    }
    return m_hash;
}

std::string SHA256::toString(const uint8_t * digest) {
    std::stringstream s;
    s << std::setfill('0') << std::hex;
    for (int i = 0; i < 32; i++) s << std::setw(2) << (int)digest[i];
    return s.str();
}

std::string SHA256::hexdigest() {
    return toString(digest());
}

std::string SHA256::hash_bytes(const uint8_t * data, size_t length) {
    SHA256 sha;
    sha.update(data, length);
    return sha.hexdigest();
}

std::string SHA256::hash_bytes(const std::string &data) {
    return hash_bytes(reinterpret_cast<const uint8_t*>(data.c_str()), data.length());
}
