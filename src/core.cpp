#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include "sha256.h"
#include "thread_pool.h"

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
    for (auto& fut : futures) {
        concatenated_hashes += fut.get();
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

PYBIND11_MODULE(aether_core, m) {
    m.doc() = "Aether-Vault C++ performance core";
    m.def("hash_file", &hash_file_parallel, py::arg("path"), py::arg("chunk_size") = 8388608, py::arg("num_threads") = 0, "Compute parallel chunked SHA-256 tree hash of a file");
    m.def("hash_file_sequential", &hash_file_sequential, py::arg("path"), "Compute standard sequential SHA-256 hash of a file");
    m.def("hash_bytes", &hash_bytes, py::arg("data"), "Compute SHA-256 hash of byte string");
    m.def("compare_metadata", &compare_metadata, py::arg("path"), py::arg("expected_size"), py::arg("expected_mtime_ns"), "Fast comparison of file size and modification time");
    m.def("get_file_metadata", &get_file_metadata, py::arg("path"), "Get file size and modification time (nanoseconds)");
}
