#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include "sha256.h"
#include "thread_pool.h"
#include "json.hpp"

using json = nlohmann::json;

namespace py = pybind11;
namespace fs = std::filesystem;

std::string hash_file_sequential(const std::string& path) {
    if (!fs::exists(path)) throw std::runtime_error("File not found: " + path);
    std::ifstream file(path, std::ios::binary);
    if (!file) throw std::runtime_error("Cannot open file: " + path);
    
    SHA256 sha;
    const size_t chunk_size = 8 * 1024 * 1024;
    std::vector<char> buffer(chunk_size);
    
    while (file.read(buffer.data(), buffer.size()) || file.gcount() > 0) {
        sha.update(reinterpret_cast<const uint8_t*>(buffer.data()), file.gcount());
    }
    return sha.hexdigest();
}

std::string hash_file_parallel(const std::string& path, size_t chunk_size = 8 * 1024 * 1024, int num_threads = 0) {
    if (!fs::exists(path)) throw std::runtime_error("File not found: " + path);
    uintmax_t file_size = fs::file_size(path);
    
    if (file_size < 2 * chunk_size) {
        return hash_file_sequential(path);
    }
    
    size_t threads_to_use = num_threads > 0 ? num_threads : std::thread::hardware_concurrency();
    ThreadPool pool(threads_to_use);
    
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
            if (!file.read(buffer.data(), to_read) && file.gcount() != to_read) {
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

py::list split_and_hash_safetensors(const std::string& path) {
    if (!fs::exists(path)) throw std::runtime_error("File not found: " + path);
    std::ifstream file(path, std::ios::binary);
    if (!file) throw std::runtime_error("Cannot open file: " + path);

    uint64_t header_size = 0;
    file.read(reinterpret_cast<char*>(&header_size), 8);
    if (file.gcount() != 8) throw std::runtime_error("Failed to read header size");

    std::vector<char> header_buf(header_size);
    file.read(header_buf.data(), header_size);
    if (file.gcount() != header_size) throw std::runtime_error("Failed to read JSON header");

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
                layers.push_back({el.key(), base_offset + start, end - start});
            }
        }
    }

    size_t threads_to_use = std::thread::hardware_concurrency();
    ThreadPool pool(threads_to_use);
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
            
            LayerResult lr;
            lr.name = layer.name;
            lr.hash = sha.hexdigest();
            lr.size = size;
            lr.offset = absolute_offset;
            return lr;
        }));
    }

    py::list results;
    for (auto& fut : futures) {
        try {
            auto lr = fut.get();
            py::dict d;
            d["name"] = lr.name;
            d["hash"] = lr.hash;
            d["size"] = lr.size;
            d["offset"] = lr.offset;
            results.append(d);
        } catch (...) {
            cancel_flag->store(true);
            throw;
        }
    }
    return results;
}


PYBIND11_MODULE(aether_core, m) {
    m.doc() = "Aether-Vault C++ performance core";
    m.def("hash_file", &hash_file_parallel, py::arg("path"), py::arg("chunk_size") = 8388608, py::arg("num_threads") = 0, "Compute parallel chunked SHA-256 tree hash of a file");
    m.def("hash_file_sequential", &hash_file_sequential, py::arg("path"), "Compute standard sequential SHA-256 hash of a file");
    m.def("hash_bytes", &hash_bytes, py::arg("data"), "Compute SHA-256 hash of byte string");
    m.def("compare_metadata", &compare_metadata, py::arg("path"), py::arg("expected_size"), py::arg("expected_mtime_ns"), "Fast comparison of file size and modification time");
    m.def("get_file_metadata", &get_file_metadata, py::arg("path"), "Get file size and modification time (nanoseconds)");
    m.def("split_and_hash_safetensors", &split_and_hash_safetensors, py::arg("path"), "Parse and hash Safetensors layers");
}
