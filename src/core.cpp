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

size_t resolve_thread_count(size_t requested) {
    if (requested > 0) return requested;
    size_t n = g_configured_threads > 0 ? g_configured_threads : std::thread::hardware_concurrency();
    return n > 0 ? n : 1;
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
    g_configured_threads = n > 0 ? static_cast<size_t>(n) : 0;
    g_pool.reset();
    g_pool_size = 0;
}

int get_max_threads() {
    std::lock_guard<std::mutex> lock(g_pool_mutex);
    return static_cast<int>(g_configured_threads);
}

std::string hash_file_sequential(const std::string& path) {
    if (!fs::exists(path)) throw std::runtime_error("File not found: " + path);
    std::ifstream file(path, std::ios::binary);
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
    if (!fs::exists(path)) throw std::runtime_error("File not found: " + path);
    uintmax_t file_size = fs::file_size(path);

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
            std::ifstream file(path, std::ios::binary);
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
    if (!fs::exists(src_path)) throw std::runtime_error("File not found: " + src_path);
    std::ifstream in(src_path, std::ios::binary);
    if (!in) throw std::runtime_error("Cannot open file: " + src_path);
    std::ofstream out(dest_path, std::ios::binary | std::ios::trunc);
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
    if (!fs::exists(path)) return false;
    uintmax_t current_size = fs::file_size(path);
    if (current_size != expected_size) return false;
    
    auto ftime = fs::last_write_time(path);
    int64_t current_mtime = std::chrono::duration_cast<std::chrono::nanoseconds>(ftime.time_since_epoch()).count();
    
    return current_mtime == expected_mtime_ns;
}

py::dict get_file_metadata(const std::string& path) {
    py::dict result;
    if (!fs::exists(path)) {
        result["exists"] = false;
        result["size"] = 0;
        result["mtime_ns"] = 0;
        return result;
    }
    result["exists"] = true;
    result["size"] = static_cast<uint64_t>(fs::file_size(path));
    auto ftime = fs::last_write_time(path);
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
    if (!fs::exists(path)) throw std::runtime_error("File not found: " + path);
    uint64_t total_size = static_cast<uint64_t>(fs::file_size(path));
    if (total_size < 8) throw std::runtime_error("File too small to be a safetensors file: " + path);

    std::ifstream file(path, std::ios::binary);
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
            std::ifstream f(path, std::ios::binary);
            if (!f) {
                cancel_flag->store(true);
                throw std::runtime_error("Cannot open file: " + path);
            }
            uint64_t absolute_offset = layer.abs_start;
            uint64_t size = layer.size;
            f.seekg(absolute_offset);
            
            SHA256 sha;
            const size_t chunk_size = 8 * 1024 * 1024;
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

// Pure-C++ compute -- see split_and_hash_safetensors_core's comment for why this is split
// out from the pybind11-facing wrapper below (safe GIL release around a non-py::-returning
// function; unsafe to attempt directly on a py::list-returning def).
std::vector<ChunkResult> chunk_and_hash_file_core(const std::string& path,
                             uint64_t min_chunk = 512 * 1024,
                             uint64_t avg_chunk = 2ULL * 1024 * 1024,
                             uint64_t max_chunk = 8ULL * 1024 * 1024) {
    if (!fs::exists(path)) throw std::runtime_error("File not found: " + path);
    if (min_chunk == 0 || avg_chunk < min_chunk || max_chunk < avg_chunk)
        throw std::runtime_error("Invalid chunk sizes: require min <= avg <= max, min > 0");
    uint64_t file_size = static_cast<uint64_t>(fs::file_size(path));

    // Boundary mask: cut when (rolling & mask) == 0. avg_chunk must be a power of two for
    // the mask form; round down to the nearest power of two so callers can pass round MBs.
    uint64_t pow2 = 1;
    while (pow2 * 2 <= avg_chunk) pow2 *= 2;
    uint64_t mask = pow2 - 1;

    static const std::array<uint64_t, 256> GEAR = make_gear_table();
    const size_t WINDOW = 48;

    // Pass 1 (sequential, unavoidable — each boundary depends on all prior bytes): find
    // content-defined cut points with a gear rolling hash. One streaming read of the file.
    std::vector<uint64_t> offsets{0};
    {
        std::ifstream file(path, std::ios::binary);
        if (!file) throw std::runtime_error("Cannot open file: " + path);

        uint64_t hash = 0;
        uint64_t chunk_start = 0;
        uint64_t pos = 0;
        // Last position where a cut still leaves >= min_chunk for the tail; enough_left_after_cut
        // blocks every cut past it. last_chance below fires exactly here to keep max_chunk a hard cap.
        uint64_t last_valid_cut_pos = (file_size > min_chunk) ? file_size - min_chunk : 0;
        std::vector<char> buffer(1024 * 1024);
        while (file) {
            file.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
            std::streamsize got = file.gcount();
            for (std::streamsize i = 0; i < got; ++i) {
                hash = (hash << 1) + GEAR[static_cast<uint8_t>(buffer[static_cast<size_t>(i)])];
                pos++;
                uint64_t size_so_far = pos - chunk_start;
                // A cut needs >= min_chunk left for the tail, else max_chunk is only a soft cap.
                bool enough_left_after_cut = (file_size - pos >= min_chunk);
                bool boundary = ((hash & mask) == 0) &&
                                (size_so_far >= min_chunk) &&
                                (size_so_far < max_chunk);
                bool overflow = size_so_far >= max_chunk;   // hard cap: never exceed max
                // Fires only if skipping it would run past max_chunk to EOF (size_so_far +
                // min_chunk, since no cut is possible after this pos) -- else ordinary
                // files get a needless extra cut.
                bool last_chance = (pos == last_valid_cut_pos) && (size_so_far >= min_chunk) &&
                                   (size_so_far + min_chunk > max_chunk);
                if ((boundary || overflow || last_chance) && enough_left_after_cut && pos < file_size) {
                    offsets.push_back(pos);
                    chunk_start = pos;
                    hash = 0;  // reset the window at the boundary
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
            std::ifstream f(path, std::ios::binary);
            if (!f) {
                cancel_flag->store(true);
                throw std::runtime_error("Cannot open file: " + path);
            }
            f.seekg(static_cast<std::streamoff>(start));
            SHA256 sha;
            const size_t buf_size = 8 * 1024 * 1024;
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
    m.def("get_max_threads", &get_max_threads, "Currently configured thread count (0 = auto).");
}
