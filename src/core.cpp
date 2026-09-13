#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <algorithm>
#include <array>
#include "sha256.h"
#include "thread_pool.h"
#include "json.hpp"

using json = nlohmann::json;

namespace py = pybind11;
namespace fs = std::filesystem;

namespace {
// V1.6.0 (Probleme.md): every path in this file arrives from Python as a UTF-8 `std::string`
// (pybind11's str<->std::string conversion is UTF-8, always). Passing that string directly
// to `fs::exists`/`fs::file_size`/`fs::last_write_time`/`std::ifstream`/`std::ofstream`
// implicitly constructs a `fs::path` via its `std::string` constructor, which on Windows
// interprets the bytes in the process's ANSI code page, not UTF-8 -- a non-ASCII filename
// (confirmed live: reproducibly threw "File not found" for a real, existing file with a
// Cyrillic name -- tests/test_core.py::test_hash_file_non_ascii_path) then either resolves
// to the wrong path or fails to resolve at all. `fs::u8path()` is the explicit, always-
// correct-regardless-of-platform way to build a `fs::path` from a UTF-8 byte string; every
// path-taking call in this file goes through it via this one helper.
inline fs::path to_path(const std::string& utf8) { return fs::u8path(utf8); }
}  // namespace

// ---------------------------------------------------------------------------
// V1.5.0: one shared, lazily-sized thread pool instead of a fresh ThreadPool spawned and
// joined on every single hash_file_tree/split_and_hash_safetensors/chunk_and_hash_file
// call -- previously each call paid full OS-thread-creation cost, and a Python-side pool of
// N workers each calling one of these would have spawned N pools of their own (~N^2
// threads). `set_max_threads()` is the C++ end of `AV_THREADS`/`--threads`
// (python/av_cli/core.py's resolve_threads()); 0 means "auto" (hardware_concurrency()).
// Contract: only ever call set_max_threads() from the CLI's single main thread before any
// hashing/chunking call, never concurrently with one -- true by construction, since av's
// own threading model always resolves the thread count once before staging begins.
// ---------------------------------------------------------------------------
namespace {
std::mutex g_pool_mutex;
std::unique_ptr<ThreadPool> g_pool;
size_t g_pool_size = 0;
size_t g_configured_threads = 0;  // 0 = auto

// V1.6.0: capped at 16 -- python/av_cli/core.py's configure_native_threads() docstring has
// always claimed this pool is "capped higher, at 16" than the Python-side pool (capped at
// 8), but nothing here actually enforced it; an uncapped hardware_concurrency() on a large
// box (a big CI runner, a many-core workstation) spawned one OS thread per core with no
// ceiling, each holding its own read buffer (see split_and_hash_safetensors_core /
// chunk_and_hash_file_core below). 16 matches what the daemon (opt-in, on by default since
// V1.6.0) needs to stay well inside a modest machine's memory budget while idle threads sit
// around between requests (release_pool() below is what actually frees them on idle).
constexpr size_t kMaxPoolThreads = 16;

size_t resolve_thread_count(size_t requested) {
    if (requested > 0) return std::min(requested, kMaxPoolThreads);
    size_t n = g_configured_threads > 0 ? g_configured_threads : std::thread::hardware_concurrency();
    n = n > 0 ? n : 1;
    return std::min(n, kMaxPoolThreads);
}

ThreadPool& shared_pool(size_t requested_threads = 0) {
    size_t want = resolve_thread_count(requested_threads);
    std::lock_guard<std::mutex> lock(g_pool_mutex);
    if (!g_pool || g_pool_size != want) {
        g_pool = std::make_unique<ThreadPool>(want);
        g_pool_size = want;
    }
    return *g_pool;
}
}  // namespace

void set_max_threads(int n) {
    std::lock_guard<std::mutex> lock(g_pool_mutex);
    g_configured_threads = n > 0 ? std::min(static_cast<size_t>(n), kMaxPoolThreads) : 0;
    g_pool.reset();
    g_pool_size = 0;
}

int get_max_threads() {
    std::lock_guard<std::mutex> lock(g_pool_mutex);
    return static_cast<int>(g_configured_threads);
}

// V1.6.0: lets an idle daemon (opt-in, on by default) give back the OS threads and their
// read buffers a prior hashing call spun up, without ending the process. The pool is
// recreated lazily and transparently by the next call into shared_pool() -- callers never
// need to re-arm anything. Safe to call between requests only (the daemon holds its
// single-execution lock the whole time a request runs, so this can never race a live task);
// NOT safe to call while any hash_file_tree/split_and_hash_safetensors/chunk_and_hash_file
// call is in flight on another thread, same contract as set_max_threads().
void release_pool() {
    std::lock_guard<std::mutex> lock(g_pool_mutex);
    g_pool.reset();
    g_pool_size = 0;
}

std::string hash_backend() {
    return SHA256::backend_name();
}

bool set_hash_backend(const std::string& name) {
    return SHA256::set_backend(name.c_str());
}

std::string hash_file_sequential(const std::string& path) {
    if (!fs::exists(to_path(path))) throw std::runtime_error("File not found: " + path);
    std::ifstream file(to_path(path), std::ios::binary);
    if (!file) throw std::runtime_error("Cannot open file: " + path);
    
    SHA256 sha;
    // V1.5.0: 8MB -> 1MB. The streaming digest is identical either way (SHA256::update is
    // called once per read regardless of buffer size); this only bounds per-in-flight-hash
    // memory under concurrency now that hash_file can run on N Python worker threads at
    // once (GIL released above) -- N * 8MB read buffers was real headroom on this project's
    // own 3.9GB dev box, N * 1MB is not.
    const size_t chunk_size = 1 * 1024 * 1024;
    std::vector<char> buffer(chunk_size);

    while (file.read(buffer.data(), buffer.size()) || file.gcount() > 0) {
        sha.update(reinterpret_cast<const uint8_t*>(buffer.data()), file.gcount());
    }
    return sha.hexdigest();
}

// WARNING: produces a *tree hash* (SHA-256 over concatenated per-chunk hashes), NOT the
// canonical file SHA-256 -- MUST NOT be used as the content-addressing object id, since
// the server verifies uploads against the plain whole-file hash. Kept for benchmarking only.
std::string hash_file_parallel(const std::string& path, size_t chunk_size = 8 * 1024 * 1024, int num_threads = 0) {
    if (!fs::exists(to_path(path))) throw std::runtime_error("File not found: " + path);
    uintmax_t file_size = fs::file_size(to_path(path));

    // Only spin up a ThreadPool (thread creation + queue/condvar overhead) when there is
    // enough work to amortize it. For files that would yield only a couple of chunks the
    // sequential path is faster; require at least PARALLEL_MIN_CHUNKS chunks.
    const size_t PARALLEL_MIN_CHUNKS = 8;
    if (file_size < PARALLEL_MIN_CHUNKS * chunk_size) {
        return hash_file_sequential(path);
    }
    
    ThreadPool& pool = shared_pool(num_threads > 0 ? static_cast<size_t>(num_threads) : 0);

    size_t num_chunks = (file_size + chunk_size - 1) / chunk_size;
    std::vector<std::future<std::string>> futures;
    auto cancel_flag = std::make_shared<std::atomic<bool>>(false);
    
    for (size_t i = 0; i < num_chunks; ++i) {
        futures.push_back(pool.enqueue([path, i, chunk_size, file_size, cancel_flag]() {
            if (cancel_flag->load()) return std::string("");
            std::ifstream file(to_path(path), std::ios::binary);
            if (!file) {
                cancel_flag->store(true);
                throw std::runtime_error("Cannot open file: " + path);
            }
            
            size_t offset = i * chunk_size;
            size_t to_read = std::min(chunk_size, static_cast<size_t>(file_size - offset));
            
            file.seekg(offset);
            std::vector<char> buffer(to_read);
            if (!file.read(buffer.data(), to_read) || file.gcount() != to_read) {
                cancel_flag->store(true);
                throw std::runtime_error("Failed to read chunk at offset " + std::to_string(offset));
            }
            
            SHA256 sha;
            sha.update(reinterpret_cast<const uint8_t*>(buffer.data()), to_read);
            return sha.hexdigest();
        }));
    }
    
    std::string concatenated_hashes = "";
    for (auto& fut : futures) {
        try {
            concatenated_hashes += fut.get();
        } catch (...) {
            cancel_flag->store(true);
            throw;
        }
    }
    
    SHA256 final_sha;
    final_sha.update(concatenated_hashes);
    return final_sha.hexdigest();
}

std::string hash_bytes(const std::string& data) {
    return SHA256::hash_bytes(data);
}

// V1.5.0: staging a whole-file (non-chunked, non-safetensors) object used to read the
// source file twice -- once here to hash it, then again via shutil.copy2 to land it in the
// CAS. That second read is the dominant cost for a repo of many small files (exactly the
// "50 x 1KB .py + 10 x 2MB .bin" shape the commit/add benchmark fixture uses). This reads
// `src_path` exactly once, hashing and writing to `dest_path` (expected to be a temp file
// the caller `os.replace`s into place -- same "never write straight to the final
// content-addressed name" contract as Python's _atomic_publish_object) in the same pass.
// Returns the canonical whole-file SHA-256 -- identical to hash_file_sequential's output
// for the same bytes, since it's the exact same SHA256 class/streaming loop, just with an
// extra write alongside each read instead of a second independent read pass.
std::string hash_and_copy(const std::string& src_path, const std::string& dest_path) {
    if (!fs::exists(to_path(src_path))) throw std::runtime_error("File not found: " + src_path);
    std::ifstream in(to_path(src_path), std::ios::binary);
    if (!in) throw std::runtime_error("Cannot open file: " + src_path);
    std::ofstream out(to_path(dest_path), std::ios::binary | std::ios::trunc);
    if (!out) throw std::runtime_error("Cannot open destination for write: " + dest_path);

    SHA256 sha;
    const size_t chunk_size = 1 * 1024 * 1024;
    std::vector<char> buffer(chunk_size);

    while (in.read(buffer.data(), static_cast<std::streamsize>(buffer.size())) || in.gcount() > 0) {
        std::streamsize got = in.gcount();
        sha.update(reinterpret_cast<const uint8_t*>(buffer.data()), static_cast<size_t>(got));
        out.write(buffer.data(), got);
        if (!out) throw std::runtime_error("Failed writing to " + dest_path);
    }
    out.close();
    return sha.hexdigest();
}

// TEST-ONLY (leading underscore, no docstring promise of stability): exercises
// SHA256::update() across an arbitrary sequence of call-size splits, which no real hashing
// path does -- hash_file_sequential always reads in fixed 8MB buffers, so the "top up a
// pending partial block left over from a previous update() call" branch of the V1.5.0
// bulk-update rewrite is otherwise never hit by real usage (8MB is itself a multiple of the
// 64-byte block size). Backs tests/test_core.py's randomized-chunk-split property test
// proving the rewrite is bit-for-bit identical to the original byte-at-a-time version.
std::string hash_bytes_split(const std::string& data, const std::vector<size_t>& splits) {
    SHA256 sha;
    size_t pos = 0;
    for (size_t n : splits) {
        if (pos >= data.size()) break;
        size_t take = std::min(n, data.size() - pos);
        sha.update(reinterpret_cast<const uint8_t*>(data.data()) + pos, take);
        pos += take;
    }
    if (pos < data.size()) {
        sha.update(reinterpret_cast<const uint8_t*>(data.data()) + pos, data.size() - pos);
    }
    return sha.hexdigest();
}

bool compare_metadata(const std::string& path, uint64_t expected_size, int64_t expected_mtime_ns) {
    if (!fs::exists(to_path(path))) return false;
    uintmax_t current_size = fs::file_size(to_path(path));
    if (current_size != expected_size) return false;
    
    auto ftime = fs::last_write_time(to_path(path));
    int64_t current_mtime = std::chrono::duration_cast<std::chrono::nanoseconds>(ftime.time_since_epoch()).count();
    
    return current_mtime == expected_mtime_ns;
}

py::dict get_file_metadata(const std::string& path) {
    py::dict result;
    if (!fs::exists(to_path(path))) {
        result["exists"] = false;
        result["size"] = 0;
        result["mtime_ns"] = 0;
        return result;
    }
    result["exists"] = true;
    result["size"] = static_cast<uint64_t>(fs::file_size(to_path(path)));
    auto ftime = fs::last_write_time(to_path(path));
    result["mtime_ns"] = static_cast<int64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(ftime.time_since_epoch()).count());
    return result;
}

struct LayerResult {
    std::string name;
    std::string hash;
    uint64_t size;
    uint64_t offset;
};

// Pure-C++ compute: no py:: types touched, safe to run under a released GIL. Split out of
// the pybind11-facing wrapper below specifically so that wrapper can release the GIL around
// this call -- adding py::call_guard directly to a py::list-returning def would be undefined
// behavior (it builds Python objects with the GIL already gone). See V1.5.0 CHANGELOG entry.
std::vector<LayerResult> split_and_hash_safetensors_core(const std::string& path) {
    if (!fs::exists(to_path(path))) throw std::runtime_error("File not found: " + path);
    uint64_t total_size = static_cast<uint64_t>(fs::file_size(to_path(path)));
    if (total_size < 8) throw std::runtime_error("File too small to be a safetensors file: " + path);

    std::ifstream file(to_path(path), std::ios::binary);
    if (!file) throw std::runtime_error("Cannot open file: " + path);

    // The safetensors header length is an unvalidated, attacker-controllable 8-byte LE
    // integer. Without bounds checks a malformed/hostile file could make us allocate an
    // arbitrarily large buffer (OOM/DoS). Require the declared header to fit inside the file.
    uint64_t header_size = 0;
    file.read(reinterpret_cast<char*>(&header_size), 8);
    if (file.gcount() != 8) throw std::runtime_error("Failed to read header size");
    if (header_size > total_size - 8)
        throw std::runtime_error("Invalid safetensors header size (exceeds file): " + path);

    std::vector<char> header_buf(header_size);
    file.read(header_buf.data(), header_size);
    if (static_cast<uint64_t>(file.gcount()) != header_size)
        throw std::runtime_error("Failed to read JSON header");

    std::string header_str(header_buf.begin(), header_buf.end());
    json header = json::parse(header_str);

    uint64_t base_offset = 8 + header_size;
    
    struct LayerSpec {
        std::string name;
        uint64_t abs_start;
        uint64_t size;
    };
    std::vector<LayerSpec> layers;
    // Layer 1: the 8-byte length prefix + JSON header itself
    layers.push_back({"__header__", 0, base_offset});

    for (auto& el : header.items()) {
        if (el.key() == "__metadata__") continue;
        auto& val = el.value();
        if (val.contains("data_offsets")) {
            auto offsets = val["data_offsets"];
            if (offsets.size() == 2) {
                uint64_t start = offsets[0].get<uint64_t>();
                uint64_t end = offsets[1].get<uint64_t>();
                // Guard against corrupt headers: reversed offsets (end < start) would
                // underflow `end - start` into a huge size, and out-of-range offsets would
                // read past EOF. Validate against the data section [base_offset, total_size).
                if (end < start)
                    throw std::runtime_error("Invalid data_offsets (end < start) for layer '" + el.key() + "' in " + path);
                if (base_offset + end > total_size)
                    throw std::runtime_error("Layer '" + el.key() + "' data_offsets exceed file size in " + path);
                layers.push_back({el.key(), base_offset + start, end - start});
            }
        }
    }

    std::sort(layers.begin(), layers.end(), [](const LayerSpec& a, const LayerSpec& b) {
        return a.abs_start < b.abs_start;
    });

    ThreadPool& pool = shared_pool();
    std::vector<std::future<LayerResult>> futures;
    auto cancel_flag = std::make_shared<std::atomic<bool>>(false);

    for (const auto& layer : layers) {
        futures.push_back(pool.enqueue([path, layer, cancel_flag]() {
            if (cancel_flag->load()) return LayerResult{};
            std::ifstream f(to_path(path), std::ios::binary);
            if (!f) {
                cancel_flag->store(true);
                throw std::runtime_error("Cannot open file: " + path);
            }
            uint64_t absolute_offset = layer.abs_start;
            uint64_t size = layer.size;
            f.seekg(absolute_offset);
            
            SHA256 sha;
            // V1.6.0: 8MB -> 1MB, matching hash_file_sequential's own V1.5.0 reasoning --
            // each of up to kMaxPoolThreads layer-hashing tasks holds its own buffer
            // concurrently, so 8MB * 16 threads (128MB) was real headroom on this project's
            // own memory-constrained dev box that 1MB * 16 (16MB) is not; throughput is
            // unaffected since SHA-256 on a 1MB buffer is still far larger than any single
            // disk read's latency floor.
            const size_t chunk_size = 1 * 1024 * 1024;
            std::vector<char> buffer(chunk_size);
            uint64_t remaining = size;

            while (remaining > 0) {
                size_t to_read = std::min(static_cast<uint64_t>(chunk_size), remaining);
                f.read(buffer.data(), to_read);
                if (f.gcount() == 0) break;
                sha.update(reinterpret_cast<const uint8_t*>(buffer.data()), f.gcount());
                remaining -= f.gcount();
            }
            if (remaining > 0) {
                cancel_flag->store(true);
                throw std::runtime_error("Truncated read for layer '" + layer.name + "' in " + path);
            }

            LayerResult lr;
            lr.name = layer.name;
            lr.hash = sha.hexdigest();
            lr.size = size;
            lr.offset = absolute_offset;
            return lr;
        }));
    }

    std::vector<LayerResult> results;
    results.reserve(futures.size());
    for (auto& fut : futures) {
        try {
            results.push_back(fut.get());
        } catch (...) {
            cancel_flag->store(true);
            throw;
        }
    }
    return results;
}

// pybind11-facing wrapper: releases the GIL only around the pure-C++ compute above, then
// builds the py::list (touches Python objects) with the GIL held again -- the two must
// never overlap. This is what py::call_guard<gil_scoped_release> cannot safely express for
// a py::list-returning def; see split_and_hash_safetensors_core's comment.
py::list split_and_hash_safetensors(const std::string& path) {
    std::vector<LayerResult> layer_results;
    {
        py::gil_scoped_release release;
        layer_results = split_and_hash_safetensors_core(path);
    }
    py::list results;
    for (auto& lr : layer_results) {
        py::dict d;
        d["name"] = lr.name;
        d["hash"] = lr.hash;
        d["size"] = lr.size;
        d["offset"] = lr.offset;
        results.append(d);
    }
    return results;
}

// ---------------------------------------------------------------------------
// Content-Defined Chunking (CDC) for opaque checkpoint formats (.pt/.pth/.ckpt)
// ---------------------------------------------------------------------------

// Deterministic 256-entry gear table (splitmix64 from a fixed seed). The exact values
// don't matter — only that they're stable across machines and versions, since chunk
// boundaries (and therefore chunk hashes) must reproduce identically for dedup to work.
static std::array<uint64_t, 256> make_gear_table() {
    std::array<uint64_t, 256> table{};
    uint64_t state = 0x9E3779B97F4A7C15ULL;
    for (auto& v : table) {
        state += 0x9E3779B97F4A7C15ULL;
        uint64_t z = state;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
        v = z ^ (z >> 31);
    }
    return table;
}

struct ChunkResult {
    std::string hash;
    uint64_t size;
    uint64_t offset;
};

// V1.6.0 (WS4.1): the content-defined-chunking cut-point decision, extracted out of
// chunk_and_hash_file_core's own Pass 1 so `stage_cdc_core` (below) can share the EXACT
// same boundary logic byte-for-byte instead of a second, hand-copied implementation that
// could silently drift from the original -- two chunkers disagreeing on where a cut falls
// would mean the fused and legacy paths produce different objects for the same file, which
// is exactly the class of bug the "one implementation" rule exists to make structurally
// impossible. `chunk_and_hash_file_core` itself is refactored to use this same struct
// (see below) -- it is not just a copy sitting next to the original.
struct CdcCutter {
    uint64_t mask;
    uint64_t min_chunk;
    uint64_t max_chunk;
    uint64_t file_size;
    uint64_t last_valid_cut_pos;
    uint64_t hash = 0;
    uint64_t chunk_start = 0;
    uint64_t pos = 0;

    CdcCutter(uint64_t min_c, uint64_t avg_c, uint64_t max_c, uint64_t fsize)
        : min_chunk(min_c), max_chunk(max_c), file_size(fsize) {
        uint64_t pow2 = 1;
        while (pow2 * 2 <= avg_c) pow2 *= 2;
        mask = pow2 - 1;
        last_valid_cut_pos = (file_size > min_chunk) ? file_size - min_chunk : 0;
    }

    // Feed one byte. Returns true exactly when a cut boundary falls immediately after this
    // byte (i.e. `pos`, already advanced, is the offset of the NEXT chunk's first byte).
    bool feed(uint8_t byte) {
        static const std::array<uint64_t, 256> GEAR = make_gear_table();
        hash = (hash << 1) + GEAR[byte];
        pos++;
        uint64_t size_so_far = pos - chunk_start;
        bool enough_left_after_cut = (file_size - pos >= min_chunk);
        bool boundary = ((hash & mask) == 0) && (size_so_far >= min_chunk) && (size_so_far < max_chunk);
        bool overflow = size_so_far >= max_chunk;
        bool last_chance = (pos == last_valid_cut_pos) && (size_so_far >= min_chunk) &&
                           (size_so_far + min_chunk > max_chunk);
        if ((boundary || overflow || last_chance) && enough_left_after_cut && pos < file_size) {
            chunk_start = pos;
            hash = 0;
            return true;
        }
        return false;
    }
};

// Atomically publishes `len` bytes at `data` as the CAS object named `hash`
// (`objects_dir/<hash[:2]>/<hash[2:]>`), skipping the write entirely if that object already
// exists (the same "content-addressed, write-once" contract as Python's own
// `_atomic_publish_object`). Returns whether a NEW write happened (false = deduped).
bool publish_object_bytes(const fs::path& objects_dir, const std::string& hash,
                           const uint8_t* data, size_t len) {
    fs::path shard_dir = objects_dir / hash.substr(0, 2);
    fs::path dest = shard_dir / hash.substr(2);
    if (fs::exists(dest)) return false;
    fs::create_directories(shard_dir);
    fs::path tmp_path = shard_dir / (
        "rest.tmp." + std::to_string(
            std::chrono::steady_clock::now().time_since_epoch().count()));
    {
        std::ofstream out(tmp_path, std::ios::binary | std::ios::trunc);
        if (!out) throw std::runtime_error("Cannot open temp object for write: " + tmp_path.string());
        if (len > 0) out.write(reinterpret_cast<const char*>(data), static_cast<std::streamsize>(len));
        if (!out) {
            out.close();
            std::error_code rm_ec;
            fs::remove(tmp_path, rm_ec);
            throw std::runtime_error("Failed writing object " + hash);
        }
    }
    std::error_code ec;
    fs::rename(tmp_path, dest, ec);
    if (ec) {
        // Another writer published the same content between our exists() check and this
        // rename (a real, benign race under concurrent staging) -- if the object is there
        // now, that IS the dedup outcome we wanted; only a genuine failure is an error.
        std::error_code rm_ec;
        fs::remove(tmp_path, rm_ec);
        if (!fs::exists(dest)) throw std::runtime_error("Failed to publish object " + hash + ": " + ec.message());
        return false;
    }
    return true;
}

// Pure-C++ compute -- see split_and_hash_safetensors_core's comment for why this is split
// out from the pybind11-facing wrapper below (safe GIL release around a non-py::-returning
// function; unsafe to attempt directly on a py::list-returning def).
std::vector<ChunkResult> chunk_and_hash_file_core(const std::string& path,
                             uint64_t min_chunk = 512 * 1024,
                             uint64_t avg_chunk = 2ULL * 1024 * 1024,
                             uint64_t max_chunk = 8ULL * 1024 * 1024) {
    if (!fs::exists(to_path(path))) throw std::runtime_error("File not found: " + path);
    if (min_chunk == 0 || avg_chunk < min_chunk || max_chunk < avg_chunk)
        throw std::runtime_error("Invalid chunk sizes: require min <= avg <= max, min > 0");
    uint64_t file_size = static_cast<uint64_t>(fs::file_size(to_path(path)));

    // Pass 1 (sequential, unavoidable — each boundary depends on all prior bytes): find
    // content-defined cut points with a gear rolling hash. One streaming read of the file.
    // Cut decision delegated to CdcCutter -- the SAME struct stage_cdc_core (below) uses,
    // so this and the fused path can never disagree about where a boundary falls.
    std::vector<uint64_t> offsets{0};
    {
        std::ifstream file(to_path(path), std::ios::binary);
        if (!file) throw std::runtime_error("Cannot open file: " + path);

        CdcCutter cutter(min_chunk, avg_chunk, max_chunk, file_size);
        std::vector<char> buffer(1024 * 1024);
        while (file) {
            file.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
            std::streamsize got = file.gcount();
            for (std::streamsize i = 0; i < got; ++i) {
                if (cutter.feed(static_cast<uint8_t>(buffer[static_cast<size_t>(i)]))) {
                    offsets.push_back(cutter.pos);
                }
            }
        }
    }

    // Pass 2 (parallel): SHA-256 each [offset[i], offset[i+1]) range independently.
    ThreadPool& pool = shared_pool();
    std::vector<std::future<ChunkResult>> futures;
    auto cancel_flag = std::make_shared<std::atomic<bool>>(false);

    for (size_t i = 0; i < offsets.size(); ++i) {
        uint64_t start = offsets[i];
        uint64_t end = (i + 1 < offsets.size()) ? offsets[i + 1] : file_size;
        futures.push_back(pool.enqueue([path, start, end, cancel_flag]() {
            if (cancel_flag->load()) return ChunkResult{};
            std::ifstream f(to_path(path), std::ios::binary);
            if (!f) {
                cancel_flag->store(true);
                throw std::runtime_error("Cannot open file: " + path);
            }
            f.seekg(static_cast<std::streamoff>(start));
            SHA256 sha;
            // V1.6.0: 8MB -> 1MB, same memory-bound reasoning as the safetensors layer
            // hasher above -- up to kMaxPoolThreads chunk-hashing tasks run concurrently.
            const size_t buf_size = 1 * 1024 * 1024;
            std::vector<char> buffer(buf_size);
            uint64_t remaining = end - start;
            while (remaining > 0) {
                uint64_t to_read = std::min<uint64_t>(buf_size, remaining);
                f.read(buffer.data(), static_cast<std::streamsize>(to_read));
                if (f.gcount() == 0) break;
                sha.update(reinterpret_cast<const uint8_t*>(buffer.data()),
                           static_cast<size_t>(f.gcount()));
                remaining -= static_cast<uint64_t>(f.gcount());
            }
            if (remaining > 0) {
                cancel_flag->store(true);
                throw std::runtime_error("Truncated read in chunk at offset " + std::to_string(start));
            }
            ChunkResult r;
            r.hash = sha.hexdigest();
            r.size = end - start;
            r.offset = start;
            return r;
        }));
    }

    std::vector<ChunkResult> results;
    results.reserve(futures.size());
    for (auto& fut : futures) {
        try {
            results.push_back(fut.get());
        } catch (...) {
            cancel_flag->store(true);
            throw;
        }
    }
    return results;
}

// pybind11-facing wrapper -- see split_and_hash_safetensors's comment: GIL released only
// around the pure-C++ compute, py::list built afterward with the GIL held again.
py::list chunk_and_hash_file(const std::string& path,
                             uint64_t min_chunk = 512 * 1024,
                             uint64_t avg_chunk = 2ULL * 1024 * 1024,
                             uint64_t max_chunk = 8ULL * 1024 * 1024) {
    std::vector<ChunkResult> chunk_results;
    {
        py::gil_scoped_release release;
        chunk_results = chunk_and_hash_file_core(path, min_chunk, avg_chunk, max_chunk);
    }
    py::list results;
    for (auto& cr : chunk_results) {
        py::dict d;
        d["hash"] = cr.hash;
        d["size"] = cr.size;
        d["offset"] = cr.offset;
        results.append(d);
    }
    return results;
}


// V1.6.0 (WS4.1): fused single-read staging for CDC-chunked (opaque, non-safetensors)
// checkpoints. `chunk_and_hash_file_core` above still does TWO full read passes over the
// file (Pass 1 to find cut points, Pass 2 -- fully parallel, re-opening and re-reading the
// file per chunk -- to hash each range), and Python separately does a THIRD full read to
// slice+write each chunk's bytes into `.av/objects`. This does the cut-detection, the
// per-chunk (and whole-file) hashing, AND the CAS write in ONE single sequential pass:
// `CdcCutter::feed()` already inspects every byte one at a time (the rolling hash has an
// unavoidable sequential dependency), so accumulating that same byte into the current
// chunk's hash/buffer costs nothing extra beyond the buffer copy itself. SHA-256 updates
// are still batched in bulk per contiguous run between cut points (matching this project's
// own "never update() one byte at a time" rule), not fed byte-by-byte.
//
// Deliberately NOT threaded like chunk_and_hash_file_core's Pass 2 -- a fused single pass
// has no independent per-chunk ranges to parallelize over (each chunk's hash depends on
// having already read every byte up to its own cut, which the cutter itself must do
// sequentially anyway). The win here is fewer total bytes read off disk, not more CPU
// parallelism; see WS4.1's own scope note in this phase's CHANGELOG entry for the explicit
// decision to ship this piece now and leave the higher-risk safetensors overlap/streaming
// case on its existing (already fully parallel, already correct) legacy path.
struct StagedPart {
    std::string name;
    std::string hash;
    uint64_t size;
    uint64_t offset;
    bool written;  // false = deduped (an object with this hash already existed)
};

struct StageResult {
    std::string whole_hash;
    std::vector<StagedPart> parts;
    uint64_t bytes_written;
};

std::vector<StagedPart> stage_cdc_core_parts_only(const std::string& path,
                                                   const std::string& objects_dir_str,
                                                   uint64_t min_chunk, uint64_t avg_chunk,
                                                   uint64_t max_chunk,
                                                   std::string* out_whole_hash,
                                                   uint64_t* out_bytes_written) {
    if (!fs::exists(to_path(path))) throw std::runtime_error("File not found: " + path);
    if (min_chunk == 0 || avg_chunk < min_chunk || max_chunk < avg_chunk)
        throw std::runtime_error("Invalid chunk sizes: require min <= avg <= max, min > 0");
    fs::path objects_dir = to_path(objects_dir_str);
    fs::create_directories(objects_dir);
    uint64_t file_size = static_cast<uint64_t>(fs::file_size(to_path(path)));

    std::ifstream file(to_path(path), std::ios::binary);
    if (!file) throw std::runtime_error("Cannot open file: " + path);

    CdcCutter cutter(min_chunk, avg_chunk, max_chunk, file_size);
    SHA256 whole_sha;
    SHA256 chunk_sha;
    std::vector<uint8_t> chunk_buffer;
    chunk_buffer.reserve(static_cast<size_t>(std::min<uint64_t>(max_chunk, 8ULL * 1024 * 1024)));
    uint64_t chunk_offset = 0;
    uint64_t bytes_written = 0;
    std::vector<StagedPart> parts;

    auto flush_chunk = [&]() {
        std::string hash = chunk_sha.hexdigest();
        bool written = publish_object_bytes(objects_dir, hash, chunk_buffer.data(), chunk_buffer.size());
        StagedPart part;
        part.name = hash;
        part.hash = hash;
        part.size = static_cast<uint64_t>(chunk_buffer.size());
        part.offset = chunk_offset;
        part.written = written;
        if (written) bytes_written += chunk_buffer.size();
        chunk_offset += chunk_buffer.size();
        parts.push_back(std::move(part));
        chunk_buffer.clear();
        chunk_sha = SHA256();
    };

    const size_t READ_BUF = 1 * 1024 * 1024;
    std::vector<char> buffer(READ_BUF);
    while (file) {
        file.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        std::streamsize got = file.gcount();
        if (got <= 0) break;
        size_t run_start = 0;  // start of the not-yet-hashed run within THIS read buffer
        for (std::streamsize i = 0; i < got; ++i) {
            bool cut = cutter.feed(static_cast<uint8_t>(buffer[static_cast<size_t>(i)]));
            if (cut) {
                size_t run_len = static_cast<size_t>(i + 1) - run_start;
                const uint8_t* run_ptr = reinterpret_cast<const uint8_t*>(buffer.data() + run_start);
                whole_sha.update(run_ptr, run_len);
                chunk_sha.update(run_ptr, run_len);
                chunk_buffer.insert(chunk_buffer.end(), run_ptr, run_ptr + run_len);
                run_start = static_cast<size_t>(i + 1);
                flush_chunk();
            }
        }
        if (run_start < static_cast<size_t>(got)) {
            size_t run_len = static_cast<size_t>(got) - run_start;
            const uint8_t* run_ptr = reinterpret_cast<const uint8_t*>(buffer.data() + run_start);
            whole_sha.update(run_ptr, run_len);
            chunk_sha.update(run_ptr, run_len);
            chunk_buffer.insert(chunk_buffer.end(), run_ptr, run_ptr + run_len);
        }
    }
    // Final chunk: whatever's left after the last cut (or the whole file, if it never cut
    // at all -- a file smaller than min_chunk produces exactly one chunk, same as the
    // legacy two-pass implementation's offsets={0} initial-and-only entry).
    flush_chunk();

    *out_whole_hash = whole_sha.hexdigest();
    *out_bytes_written = bytes_written;
    return parts;
}

// pybind11-facing wrapper -- same GIL-release pattern as split_and_hash_safetensors/
// chunk_and_hash_file: release only around the pure-C++ compute, build the py::dict
// afterward with the GIL held again.
py::dict stage_cdc(const std::string& path, const std::string& objects_dir,
                    uint64_t min_chunk = 512 * 1024,
                    uint64_t avg_chunk = 2ULL * 1024 * 1024,
                    uint64_t max_chunk = 8ULL * 1024 * 1024) {
    std::vector<StagedPart> parts;
    std::string whole_hash;
    uint64_t bytes_written = 0;
    {
        py::gil_scoped_release release;
        parts = stage_cdc_core_parts_only(path, objects_dir, min_chunk, avg_chunk, max_chunk,
                                           &whole_hash, &bytes_written);
    }
    py::dict result;
    result["whole_hash"] = whole_hash;
    result["bytes_written"] = bytes_written;
    py::list py_parts;
    for (auto& p : parts) {
        py::dict d;
        d["name"] = p.name;
        d["hash"] = p.hash;
        d["size"] = p.size;
        d["offset"] = p.offset;
        d["written"] = p.written;
        py_parts.append(d);
    }
    result["parts"] = py_parts;
    return result;
}


// V1.6.0 (WS4.1, second half): fused single-read staging for safetensors checkpoints --
// the counterpart to stage_cdc above, same motivation. `split_and_hash_safetensors_core`
// hashes every layer in parallel but each of its worker tasks re-opens and re-reads the
// file independently (N layers = N extra full-length seeks/reads of the shared file
// object, though never more than the file's own length combined since layers don't
// overlap); Python's own per-layer `_write_layer` in `_compute_stage_result` then reads
// the file a SECOND time, once per layer, to slice out and write each layer's bytes. This
// does the header parse, per-layer (and whole-file) hashing, AND the CAS write in ONE
// sequential pass from the front of the file to its end.
//
// Unlike stage_cdc's rolling-hash cut points (unknown until the byte that produces one is
// actually read), safetensors layer boundaries are fully known upfront from the header --
// there is no online decision to make, only a known list of [start, end) ranges to walk in
// order. That structural difference is exactly why this can be a single un-parallelized
// sequential read (once you know where every boundary is, splitting a single ifstream's
// forward progress into "gap" and "layer" spans costs nothing beyond bookkeeping) while
// still being correct -- there's no work happening per-layer that isn't already forced to
// happen once, in order, by reading the file at all.
//
// Deliberately conservative on the one case this design cannot represent: OVERLAPPING
// declared layer ranges. A single sequential pass hashes each byte into exactly one
// concurrently-open layer hash (plus the whole-file hash); two layers claiming the same
// byte would need that byte fed into two independent SHA-256 states at once, which the
// legacy fully-parallel per-layer re-read handles for free (each layer just re-reads that
// byte from its own independent file handle) but this fused path structurally can't.
// Real, adversarially-crafted safetensors headers could claim this (see
// split_and_hash_safetensors_core's own bounds-checking comment on the header being
// attacker-controllable) -- rather than pick an arbitrary "whichever layer we see it in
// first" semantic that could silently diverge from the legacy oracle, this throws and lets
// the Python caller fall back to the legacy path for that one file, exactly like stage_cdc
// falls back on any other exception.
//
// Memory envelope: a layer whose declared size is <= buffer_cap_bytes accumulates in a
// single in-memory buffer (like stage_cdc's chunk_buffer) and is published via
// publish_object_bytes once complete; a layer LARGER than the cap streams straight to
// `objects_dir/.stage-tmp.<counter>` as its bytes arrive (its hash isn't known until the
// last byte, so the final content-addressed path can't be chosen upfront) and is renamed
// into place (or discarded, on a dedup hit) only once its hash is final -- the same
// temp-then-publish shape as publish_object_bytes itself, just spread out over the read
// loop instead of one bulk write. This bounds per-file memory to `buffer_cap_bytes` times
// the number of small/medium layers concurrently in flight (at most 1 for this single
// sequential function; the "concurrently" case is multiple FILES staging in parallel via
// the Python-side worker pool, i.e. buffer_cap_bytes * pool_size in the worst case) instead
// of needing the full size of every large tensor layer in memory at once.
std::vector<StagedPart> stage_safetensors_core_parts_only(const std::string& path,
                                                           const std::string& objects_dir_str,
                                                           uint64_t buffer_cap_bytes,
                                                           std::string* out_whole_hash,
                                                           uint64_t* out_bytes_written) {
    if (!fs::exists(to_path(path))) throw std::runtime_error("File not found: " + path);
    uint64_t total_size = static_cast<uint64_t>(fs::file_size(to_path(path)));
    if (total_size < 8) throw std::runtime_error("File too small to be a safetensors file: " + path);

    std::ifstream file(to_path(path), std::ios::binary);
    if (!file) throw std::runtime_error("Cannot open file: " + path);

    // Header parse -- identical validation to split_and_hash_safetensors_core (same
    // attacker-controllable-length guard: a malformed/hostile file must not make us
    // allocate an arbitrarily large buffer).
    uint64_t header_size = 0;
    file.read(reinterpret_cast<char*>(&header_size), 8);
    if (file.gcount() != 8) throw std::runtime_error("Failed to read header size");
    if (header_size > total_size - 8)
        throw std::runtime_error("Invalid safetensors header size (exceeds file): " + path);

    std::vector<char> header_buf(header_size);
    file.read(header_buf.data(), header_size);
    if (static_cast<uint64_t>(file.gcount()) != header_size)
        throw std::runtime_error("Failed to read JSON header");

    std::string header_str(header_buf.begin(), header_buf.end());
    json header = json::parse(header_str);

    uint64_t base_offset = 8 + header_size;

    struct LayerSpec {
        std::string name;
        uint64_t abs_start;
        uint64_t size;
    };
    std::vector<LayerSpec> layers;
    layers.push_back({"__header__", 0, base_offset});

    for (auto& el : header.items()) {
        if (el.key() == "__metadata__") continue;
        auto& val = el.value();
        if (val.contains("data_offsets")) {
            auto offsets = val["data_offsets"];
            if (offsets.size() == 2) {
                uint64_t start = offsets[0].get<uint64_t>();
                uint64_t end = offsets[1].get<uint64_t>();
                if (end < start)
                    throw std::runtime_error("Invalid data_offsets (end < start) for layer '" + el.key() + "' in " + path);
                if (base_offset + end > total_size)
                    throw std::runtime_error("Layer '" + el.key() + "' data_offsets exceed file size in " + path);
                layers.push_back({el.key(), base_offset + start, end - start});
            }
        }
    }

    std::sort(layers.begin(), layers.end(), [](const LayerSpec& a, const LayerSpec& b) {
        return a.abs_start < b.abs_start;
    });

    // Overlap check -- see this function's header comment for why the fused path refuses
    // rather than guessing at a semantic the legacy oracle doesn't share.
    for (size_t i = 0; i + 1 < layers.size(); ++i) {
        if (layers[i + 1].abs_start < layers[i].abs_start + layers[i].size) {
            throw std::runtime_error("Overlapping safetensors layer ranges in " + path +
                                      " (unsupported by fused staging, falls back to legacy)");
        }
    }

    fs::path objects_dir = to_path(objects_dir_str);
    fs::create_directories(objects_dir);

    SHA256 whole_sha;
    uint64_t bytes_written = 0;
    std::vector<StagedPart> parts;
    parts.reserve(layers.size());

    // The "__header__" pseudo-layer's bytes (the 8-byte length prefix + the header JSON)
    // were ALREADY consumed above to parse the header -- the ifstream's read position now
    // sits at base_offset. Hashing/publishing it must use the bytes already in memory
    // (header_buf) rather than reading from the file again, which would read the WRONG
    // bytes (whatever comes right after the header, not the header itself). This is why
    // the walk below starts at layers[1] (guaranteed to be __header__ at index 0 -- it's
    // the only layer with abs_start == 0, since every real tensor's abs_start is
    // base_offset-or-later) with cursor initialized to base_offset, not 0.
    {
        std::vector<uint8_t> header_raw;
        header_raw.reserve(static_cast<size_t>(base_offset));
        const uint8_t* len_bytes = reinterpret_cast<const uint8_t*>(&header_size);
        header_raw.insert(header_raw.end(), len_bytes, len_bytes + 8);
        header_raw.insert(header_raw.end(),
                           reinterpret_cast<const uint8_t*>(header_buf.data()),
                           reinterpret_cast<const uint8_t*>(header_buf.data()) + header_buf.size());
        whole_sha.update(header_raw.data(), header_raw.size());
        SHA256 header_sha;
        header_sha.update(header_raw.data(), header_raw.size());
        std::string header_hash = header_sha.hexdigest();
        bool header_written = publish_object_bytes(objects_dir, header_hash, header_raw.data(), header_raw.size());
        if (header_written) bytes_written += header_raw.size();
        StagedPart header_part;
        header_part.name = "__header__";
        header_part.hash = header_hash;
        header_part.size = base_offset;
        header_part.offset = 0;
        header_part.written = header_written;
        parts.push_back(std::move(header_part));
    }

    const size_t READ_BUF = 1 * 1024 * 1024;
    std::vector<char> buffer(READ_BUF);
    static std::atomic<uint64_t> tmp_counter{0};

    // Reads exactly `gap_size` bytes forward from the file's current position, feeding only
    // the whole-file hash -- bytes between declared layers (header alignment padding, or
    // any other gap a real safetensors file might carry) belong to no layer's own hash.
    auto consume_gap = [&](uint64_t gap_size) {
        uint64_t remaining = gap_size;
        while (remaining > 0) {
            size_t to_read = static_cast<size_t>(std::min<uint64_t>(READ_BUF, remaining));
            file.read(buffer.data(), static_cast<std::streamsize>(to_read));
            std::streamsize got = file.gcount();
            if (got <= 0) throw std::runtime_error("Truncated read (gap) in " + path);
            whole_sha.update(reinterpret_cast<const uint8_t*>(buffer.data()), static_cast<size_t>(got));
            remaining -= static_cast<uint64_t>(got);
        }
    };

    // Reads exactly `layer.size` bytes forward from the file's current position, feeding
    // both the whole-file hash and this layer's own hash, and either buffers them in memory
    // or streams them to a temp file depending on `buffer_cap_bytes` (see this function's
    // header comment). Publishes (or dedup-skips) the layer's CAS object once its hash is
    // final and appends its StagedPart.
    auto consume_layer = [&](const LayerSpec& layer) {
        SHA256 layer_sha;
        bool stream_to_disk = layer.size > buffer_cap_bytes;
        std::vector<uint8_t> mem_buffer;
        fs::path tmp_path;
        std::ofstream tmp_out;
        if (stream_to_disk) {
            tmp_path = objects_dir / (".stage-tmp." + std::to_string(
                std::chrono::steady_clock::now().time_since_epoch().count()) + "." +
                std::to_string(tmp_counter.fetch_add(1)));
            tmp_out.open(tmp_path, std::ios::binary | std::ios::trunc);
            if (!tmp_out) throw std::runtime_error("Cannot open temp staging file: " + tmp_path.string());
        } else {
            mem_buffer.reserve(static_cast<size_t>(layer.size));
        }

        uint64_t remaining = layer.size;
        while (remaining > 0) {
            size_t to_read = static_cast<size_t>(std::min<uint64_t>(READ_BUF, remaining));
            file.read(buffer.data(), static_cast<std::streamsize>(to_read));
            std::streamsize got = file.gcount();
            if (got <= 0) {
                if (stream_to_disk) {
                    tmp_out.close();
                    std::error_code ec;
                    fs::remove(tmp_path, ec);
                }
                throw std::runtime_error("Truncated read for layer '" + layer.name + "' in " + path);
            }
            whole_sha.update(reinterpret_cast<const uint8_t*>(buffer.data()), static_cast<size_t>(got));
            layer_sha.update(reinterpret_cast<const uint8_t*>(buffer.data()), static_cast<size_t>(got));
            if (stream_to_disk) {
                tmp_out.write(buffer.data(), got);
                if (!tmp_out) {
                    tmp_out.close();
                    std::error_code ec;
                    fs::remove(tmp_path, ec);
                    throw std::runtime_error("Failed writing temp staging file for layer '" + layer.name + "' in " + path);
                }
            } else {
                const uint8_t* p = reinterpret_cast<const uint8_t*>(buffer.data());
                mem_buffer.insert(mem_buffer.end(), p, p + got);
            }
            remaining -= static_cast<uint64_t>(got);
        }

        std::string hash = layer_sha.hexdigest();
        bool written;
        if (stream_to_disk) {
            tmp_out.close();
            fs::path shard_dir = objects_dir / hash.substr(0, 2);
            fs::path dest = shard_dir / hash.substr(2);
            if (fs::exists(dest)) {
                std::error_code ec;
                fs::remove(tmp_path, ec);
                written = false;
            } else {
                fs::create_directories(shard_dir);
                std::error_code ec;
                fs::rename(tmp_path, dest, ec);
                if (ec) {
                    // Same benign-race handling as publish_object_bytes: another writer
                    // published the identical content between our exists() check and this
                    // rename -- if the object is there now, that's the dedup outcome we
                    // wanted, not a failure.
                    std::error_code rm_ec;
                    fs::remove(tmp_path, rm_ec);
                    if (!fs::exists(dest))
                        throw std::runtime_error("Failed to publish object " + hash + ": " + ec.message());
                    written = false;
                } else {
                    written = true;
                }
            }
        } else {
            written = publish_object_bytes(objects_dir, hash, mem_buffer.data(), mem_buffer.size());
        }
        if (written) bytes_written += layer.size;

        StagedPart part;
        part.name = layer.name;
        part.hash = hash;
        part.size = layer.size;
        part.offset = layer.abs_start;
        part.written = written;
        parts.push_back(std::move(part));
    };

    uint64_t cursor = base_offset;
    for (size_t i = 1; i < layers.size(); ++i) {
        const auto& layer = layers[i];
        if (layer.abs_start > cursor) consume_gap(layer.abs_start - cursor);
        consume_layer(layer);
        cursor = layer.abs_start + layer.size;
    }
    if (cursor < total_size) consume_gap(total_size - cursor);

    *out_whole_hash = whole_sha.hexdigest();
    *out_bytes_written = bytes_written;
    return parts;
}

// pybind11-facing wrapper -- same GIL-release pattern as stage_cdc/split_and_hash_safetensors.
py::dict stage_safetensors(const std::string& path, const std::string& objects_dir,
                            uint64_t buffer_cap_bytes = 32ULL * 1024 * 1024) {
    std::vector<StagedPart> parts;
    std::string whole_hash;
    uint64_t bytes_written = 0;
    {
        py::gil_scoped_release release;
        parts = stage_safetensors_core_parts_only(path, objects_dir, buffer_cap_bytes,
                                                   &whole_hash, &bytes_written);
    }
    py::dict result;
    result["whole_hash"] = whole_hash;
    result["bytes_written"] = bytes_written;
    py::list py_parts;
    for (auto& p : parts) {
        py::dict d;
        d["name"] = p.name;
        d["hash"] = p.hash;
        d["size"] = p.size;
        d["offset"] = p.offset;
        d["written"] = p.written;
        py_parts.append(d);
    }
    result["parts"] = py_parts;
    return result;
}


PYBIND11_MODULE(aether_core, m) {
    m.doc() = "Aether-Vault C++ performance core";
    // INVARIANT: `hash_file` is the canonical content-addressing hash and MUST equal the
    // plain whole-file SHA-256 (hashlib.sha256(data).hexdigest()), because the server
    // re-verifies every uploaded object against exactly that. It is bound to the sequential
    // implementation; do NOT swap in hash_file_parallel (it yields a different tree hash and
    // would break dedup, deduplication across the LFS threshold, and remote uploads).
    // V1.5.0: call_guard<gil_scoped_release> only on these four scalar-returning defs --
    // they touch no Python object inside the C++ body, so releasing the GIL for their whole
    // duration is safe and lets a Python-side ThreadPoolExecutor get real parallelism
    // calling them (previously impossible: the GIL was never released anywhere in this
    // module, so N Python threads calling into C++ fully serialized regardless of the C++
    // side's own thread pools). split_and_hash_safetensors/chunk_and_hash_file build a
    // py::list and therefore do NOT get call_guard here -- see their *_core split above for
    // why, and how they release the GIL safely around only their pure-compute portion.
    m.def("hash_file", &hash_file_sequential, py::arg("path"), py::call_guard<py::gil_scoped_release>(), "Canonical whole-file SHA-256 (content-addressing object id)");
    m.def("hash_file_tree", &hash_file_parallel, py::arg("path"), py::arg("chunk_size") = 8388608, py::arg("num_threads") = 0, py::call_guard<py::gil_scoped_release>(), "Parallel chunked SHA-256 *tree* hash (NOT a canonical file hash)");
    m.def("hash_file_sequential", &hash_file_sequential, py::arg("path"), py::call_guard<py::gil_scoped_release>(), "Compute standard sequential SHA-256 hash of a file");
    m.def("hash_bytes", &hash_bytes, py::arg("data"), py::call_guard<py::gil_scoped_release>(), "Compute SHA-256 hash of byte string");
    m.def("hash_and_copy", &hash_and_copy, py::arg("src_path"), py::arg("dest_path"), py::call_guard<py::gil_scoped_release>(),
          "Read src_path once, hashing and writing to dest_path in the same pass. Returns "
          "the canonical whole-file SHA-256 (identical to hash_file for the same bytes). "
          "Caller is responsible for dest_path being a temp file it atomically publishes.");
    m.def("_hash_bytes_split", &hash_bytes_split, py::arg("data"), py::arg("splits"), py::call_guard<py::gil_scoped_release>(),
          "TEST-ONLY, no stability promise: SHA-256 over `data` fed through update() in the "
          "given call-size splits (any leftover feeds as one final call). Backs the V1.5.0 "
          "bulk-update rewrite's randomized-split property test.");
    m.def("compare_metadata", &compare_metadata, py::arg("path"), py::arg("expected_size"), py::arg("expected_mtime_ns"), "Fast comparison of file size and modification time");
    m.def("get_file_metadata", &get_file_metadata, py::arg("path"), "Get file size and modification time (nanoseconds)");
    m.def("split_and_hash_safetensors", &split_and_hash_safetensors, py::arg("path"), "Parse and hash Safetensors layers");
    m.def("stage_cdc", &stage_cdc, py::arg("path"), py::arg("objects_dir"),
          py::arg("min_chunk") = 512 * 1024, py::arg("avg_chunk") = 2ULL * 1024 * 1024,
          py::arg("max_chunk") = 8ULL * 1024 * 1024,
          "V1.6.0: fused single-read staging for CDC-chunked files -- one sequential pass "
          "computes the whole-file hash, cuts and hashes each chunk, AND publishes each "
          "chunk directly to objects_dir (skipping already-existing objects), instead of "
          "chunk_and_hash_file's two read passes plus a separate Python-side write pass. "
          "Returns {\"whole_hash\", \"bytes_written\", \"parts\": [{\"name\",\"hash\","
          "\"size\",\"offset\",\"written\"}]}. Boundary-identical to chunk_and_hash_file "
          "(same CdcCutter) -- see tests/test_core.py's determinism tests against it as "
          "the oracle.");
    m.def("stage_safetensors", &stage_safetensors, py::arg("path"), py::arg("objects_dir"),
          py::arg("buffer_cap_bytes") = 32ULL * 1024 * 1024,
          "V1.6.0: fused single-read staging for safetensors files -- one sequential pass "
          "parses the header, hashes each layer (and the whole file), AND publishes each "
          "layer directly to objects_dir (skipping already-existing objects), instead of "
          "split_and_hash_safetensors's parallel per-layer re-reads plus a separate "
          "Python-side per-layer write pass. A layer larger than buffer_cap_bytes streams "
          "straight to a temp file instead of buffering in memory. Throws on overlapping "
          "declared layer ranges (the caller should fall back to split_and_hash_safetensors "
          "for that file). Returns {\"whole_hash\", \"bytes_written\", \"parts\": "
          "[{\"name\",\"hash\",\"size\",\"offset\",\"written\"}]} in file order -- "
          "byte-identical to split_and_hash_safetensors's per-layer hashes -- see "
          "tests/test_core.py's determinism tests against it as the oracle.");
    m.def("chunk_and_hash_file", &chunk_and_hash_file, py::arg("path"),
          py::arg("min_chunk") = 512 * 1024, py::arg("avg_chunk") = 2ULL * 1024 * 1024,
          py::arg("max_chunk") = 8ULL * 1024 * 1024,
          "Content-defined chunking (gear-hash cut points) + parallel SHA-256 per chunk. "
          "Format-agnostic dedup for opaque checkpoint files (.pt/.pth/.ckpt). Returns "
          "[{hash, size, offset}] in file order.");
    m.def("set_max_threads", &set_max_threads, py::arg("n"),
          "Size the shared C++ thread pool used by hash_file_tree/split_and_hash_safetensors/"
          "chunk_and_hash_file. n<=0 means auto (hardware_concurrency()). Call only from the "
          "CLI's single main thread before any hashing/chunking call -- see shared_pool()'s "
          "comment. This is the C++ end of AV_THREADS/--threads.");
    m.def("get_max_threads", &get_max_threads, "Currently configured thread count (0 = auto, capped at 16).");
    m.def("release_pool", &release_pool,
          "Frees the shared thread pool's OS threads and their read buffers. Safe only "
          "between calls, never while a hashing/chunking call is in flight elsewhere -- the "
          "next such call transparently recreates the pool. Intended for an idle daemon "
          "(V1.6.0) to give memory back without exiting the process.");
    m.def("hash_backend", &hash_backend,
          "Name of the currently selected SHA-256 backend: \"sha-ni\", \"arm-sha2\", or "
          "\"scalar\". Resolved once per process (first hash call), honoring "
          "AV_SHA256_BACKEND, unless changed via set_hash_backend().");
    m.def("set_hash_backend", &set_hash_backend, py::arg("name"),
          "Test/diagnostic hook: forces the SHA-256 backend by name (\"auto\"/\"scalar\"/"
          "\"sha-ni\"/\"arm-sha2\") for the rest of the process. Returns False (backend left "
          "unchanged) if the name is unknown, unsupported by this CPU, or fails its "
          "correctness self-test -- never raises, since a hashing path this central must "
          "never hard-fail on a bad diagnostic request.");
}
