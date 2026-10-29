#pragma once
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <limits>
#include <memory>
#include <span>

namespace lab {
struct FileCloser {
    void operator()(FILE *file) const noexcept {
        if (file) std::fclose(file);
    }
};
using File = std::unique_ptr<FILE,FileCloser>;

inline bool close(File &file) noexcept {
    FILE *handle = file.release();
    return !handle || std::fclose(handle) == 0;
}

inline bool little16(FILE &file, std::uint16_t value) noexcept {
    return std::fputc(value & 255,&file) != EOF &&
           std::fputc(value >> 8,&file) != EOF;
}
inline bool little32(FILE &file, std::uint32_t value) noexcept {
    return little16(file,static_cast<std::uint16_t>(value)) &&
           little16(file,static_cast<std::uint16_t>(value >> 16));
}

inline bool write_wav(FILE &file, std::span<const std::int16_t> audio) noexcept {
    constexpr auto max_samples = (std::numeric_limits<std::uint32_t>::max()-36U)/2U;
    if (audio.size() > max_samples) {
        errno = EFBIG;
        return false;
    }
    const auto bytes = static_cast<std::uint32_t>(audio.size()*2U);
    if (std::fwrite("RIFF",1,4,&file) != 4 || !little32(file,36U+bytes) ||
        std::fwrite("WAVEfmt ",1,8,&file) != 8 || !little32(file,16) ||
        !little16(file,1) || !little16(file,1) ||
        !little32(file,48000) || !little32(file,96000) ||
        !little16(file,2) || !little16(file,16) ||
        std::fwrite("data",1,4,&file) != 4 || !little32(file,bytes)) return false;
    for (const auto sample : audio)
        if (!little16(file,static_cast<std::uint16_t>(sample))) return false;
    return true;
}
inline bool write_wav(const char *path, std::span<const std::int16_t> audio) noexcept {
    File file{std::fopen(path,"wb")};
    if (!file) return false;
    const bool written = write_wav(*file,audio);
    const int write_error = errno;
    const bool closed = close(file);
    if (!written) errno = write_error;
    return written && closed;
}
}
