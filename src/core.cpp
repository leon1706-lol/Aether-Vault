#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <vector>
#include <string>
#include <map>
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>
#include "sha256.h"
#include "thread_pool.h"

namespace py = pybind11;
namespace fs = std::filesystem;

/**
 * Optimized Safetensors Header Parser
 * Dynamically extracts layer names and their relative byte offsets.
 */
std::map<std::string, std::pair<uint64_t, uint64_t>> parse_safetensors_header(const char* header_data, uint64_t header_size) {
    std::map<std::string, std::pair<uint64_t, uint64_t>> layers;
    std::string json_str(header_data, header_size);
    
    size_t pos = 0;
    while ((pos = json_str.find("\"data_offsets\"", pos)) != std::string::npos) {
        size_t key_end = json_str.rfind("\"", pos - 3);
        size_t key_start = json_str.rfind("\"", key_end - 1);
        std::string layer_name = json_str.substr(key_start + 1, key_end - key_start - 1);
        
        size_t start_bracket = json_str.find("[", pos);
        size_t comma = json_str.find(",", start_bracket);
        size_t end_bracket = json_str.find("]", comma);
        
        uint64_t start = std::stoull(json_str.substr(start_bracket + 1, comma - start_bracket - 1));
        uint64_t end = std::stoull(json_str.substr(comma + 1, end_bracket - comma - 1));
        
        layers[layer_name] = {start, end};
        pos = end_bracket;
    }
    return layers;
}

struct LayerHashResult {
    std::string name;
    std::string hash;
    uint64_t size;
    uint64_t offset;
};

/**
 * Displacement-Resistant Layer Hashing
 * Uses mmap to isolate layer bytes and compute hashes based ONLY on layer content.
 */
std::vector<LayerHashResult> split_and_hash_safetensors(const std::string& path, int num_threads = 0) {
    int fd = open(path.c_str(), O_RDONLY);
    if (fd == -1) throw std::runtime_error("Cannot open file: " + path);
    
    uintmax_t file_size = fs::file_size(path);
    void* addr = mmap(NULL, file_size, PROT_READ, MAP_PRIVATE, fd, 0);
    if (addr == MAP_FAILED) {
        close(fd);
        throw std::runtime_error("mmap failed for: " + path);
    }
    
    const char* data = static_cast<const char*>(addr);
    uint64_t header_size = *reinterpret_cast<const uint64_t*>(data);
    const char* header_ptr = data + 8;
    
    auto layer_offsets = parse_safetensors_header(header_ptr, header_size);
    uint64_t data_start_offset = 8 + header_size;
    
    size_t threads_to_use = num_threads > 0 ? num_threads : std::thread::hardware_concurrency();
    ThreadPool pool(threads_to_use);
    std::vector<std::future<LayerHashResult>> futures;
    
    for (auto const& [name, offsets] : layer_offsets) {
        futures.push_back(pool.enqueue([name, offsets, data, data_start_offset]() {
            uint64_t start = data_start_offset + offsets.first;
            uint64_t end = data_start_offset + offsets.second;
            uint64_t size = end - start;
            
            // The hash is computed only on the slice, making it independent of its global offset
            SHA256 sha;
            sha.update(reinterpret_cast<const uint8_t*>(data + start), size);
            return LayerHashResult{name, sha.hexdigest(), size, start};
        }));
    }
    
    std::vector<LayerHashResult> results;
    for (auto& fut : futures) {
        results.push_back(fut.get());
    }
    
    munmap(addr, file_size);
    close(fd);
    return results;
}

py::list py_split_and_hash_safetensors(const std::string& path, int num_threads = 0) {
    auto results = split_and_hash_safetensors(path, num_threads);
    py::list py_results;
    for (const auto& res : results) {
        py::dict d;
        d["name"] = res.name;
        d["hash"] = res.hash;
        d["size"] = res.size;
        d["offset"] = res.offset;
        py_results.append(d);
    }
    return py_results;
}

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
    if (file_size < 2 * chunk_size) return hash_file_sequential(path);
    size_t threads_to_use = num_threads > 0 ? num_threads : std::thread::hardware_concurrency();
    ThreadPool pool(threads_to_use);
    size_t num_chunks = (file_size + chunk_size - 1) / chunk_size;
    std::vector<std::future<std::string>> futures;
    for (size_t i = 0; i < num_chunks; ++i) {
        futures.push_back(pool.enqueue([path, i, chunk_size, file_size]() {
            std::ifstream file(path, std::ios::binary);
            if (!file) throw std::runtime_error("Cannot open file: " + path);
            size_t offset = i * chunk_size;
            size_t to_read = std::min(chunk_size, static_cast<size_t>(file_size - offset));
            file.seekg(offset);
            std::vector<char> buffer(to_read);
            file.read(buffer.data(), to_read);
            SHA256 sha;
            sha.update(reinterpret_cast<const uint8_t*>(buffer.data()), to_read);
            return sha.hexdigest();
        }));
    }
    std::string concatenated_hashes = "";
    for (auto& fut : futures) concatenated_hashes += fut.get();
    SHA256 final_sha;
    final_sha.update(concatenated_hashes);
    return final_sha.hexdigest();
}

PYBIND11_MODULE(aether_core, m) {
    m.doc() = "Aether-Vault C++ performance core with Reshaping-Resistant Layer Hashing";
    m.def("hash_file", &hash_file_parallel, py::arg("path"), py::arg("chunk_size") = 8388608, py::arg("num_threads") = 0);
    m.def("split_and_hash_safetensors", &py_split_and_hash_safetensors, py::arg("path"), py::arg("num_threads") = 0);
    m.def("get_file_metadata", [](const std::string& path) {
        py::dict result;
        if (!fs::exists(path)) { result["exists"] = false; return result; }
        result["exists"] = true;
        result["size"] = static_cast<uint64_t>(fs::file_size(path));
        auto ftime = fs::last_write_time(path);
        result["mtime_ns"] = static_cast<int64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(ftime.time_since_epoch()).count());
        return result;
    });
}
