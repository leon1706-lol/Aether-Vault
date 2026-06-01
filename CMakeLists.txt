cmake_minimum_required(VERSION 3.16)
project(aether_core)

find_package(pybind11 REQUIRED)

pybind11_add_module(aether_core src/core.cpp src/sha256.cpp)

target_include_directories(aether_core PRIVATE src/)
target_compile_features(aether_core PRIVATE cxx_std_17)

find_package(Threads REQUIRED)
target_link_libraries(aether_core PRIVATE Threads::Threads)
