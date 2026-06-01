#pragma once
#include <cstdint>
#include <string>
#include <vector>

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

private:
    uint8_t data[64];
    uint32_t datalen;
    unsigned long long bitlen;
    uint32_t state[8];
    uint8_t m_hash[32];
    void transform();
};
