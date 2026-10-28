#pragma once
#include <cstdint>
#include <cstdio>
#include <memory>
#include <span>

namespace lab {
struct FileCloser {
    void operator()(FILE *file) const { std::fclose(file); }
};
using File = std::unique_ptr<FILE,FileCloser>;

inline void little16(FILE *file, std::uint16_t value) {
    std::fputc(value & 255,file);
    std::fputc(value >> 8,file);
}
inline void little32(FILE *file, std::uint32_t value) {
    little16(file,static_cast<std::uint16_t>(value));
    little16(file,static_cast<std::uint16_t>(value >> 16));
}
inline void write_wav(FILE *file, std::span<const std::int16_t> audio) {
    const auto bytes = static_cast<std::uint32_t>(audio.size()*2);
    std::fwrite("RIFF",1,4,file); little32(file,36+bytes);
    std::fwrite("WAVEfmt ",1,8,file); little32(file,16);
    little16(file,1); little16(file,1);
    little32(file,48000); little32(file,96000);
    little16(file,2); little16(file,16);
    std::fwrite("data",1,4,file); little32(file,bytes);
    for (auto sample : audio) little16(file,static_cast<std::uint16_t>(sample));
}
inline bool write_wav(const char *path, std::span<const std::int16_t> audio) {
    File file{std::fopen(path,"wb")};
    if (!file) return false;
    write_wav(file.get(),audio);
    const bool good = !std::ferror(file.get());
    return std::fclose(file.release()) == 0 && good;
}
}
