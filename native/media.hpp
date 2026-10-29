#pragma once
#include <cerrno>
#include <cstdint>
#include <fstream>
#include <ios>
#include <limits>
#include <ostream>
#include <span>
#include <string>
#include <system_error>

namespace lab {

// Streams keep no error code, but on Linux the failing system call leaves errno set.
inline std::error_code stream_error() noexcept { return {errno != 0 ? errno : EIO, std::generic_category()}; }

// Writes a whole binary file through `writer(std::ostream &)`, reporting open, write and close failures
// alike.
template<typename Writer>
std::error_code write_file(const std::string &path, Writer &&writer) {
    errno = 0;
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    if (!out) return stream_error();
    if (const std::error_code error = writer(out)) return error;
    out.close();
    return out ? std::error_code{} : stream_error();
}

inline void put_le16(std::ostream &out, std::uint16_t value) {
    const unsigned bits = value;
    out.put(static_cast<char>(bits & 0xFFU));
    out.put(static_cast<char>(bits >> 8U));
}
inline void put_le32(std::ostream &out, std::uint32_t value) {
    put_le16(out, static_cast<std::uint16_t>(value & 0xFFFFU));
    put_le16(out, static_cast<std::uint16_t>(value >> 16U));
}

// 48 kHz mono 16-bit PCM.
inline std::error_code write_wav(std::ostream &out, std::span<const std::int16_t> audio) {
    constexpr auto max_samples = (std::numeric_limits<std::uint32_t>::max() - 36U) / 2U;
    if (audio.size() > max_samples) return std::make_error_code(std::errc::file_too_large);
    const auto bytes = static_cast<std::uint32_t>(audio.size() * 2U);
    out << "RIFF";
    put_le32(out, 36U + bytes);
    out << "WAVEfmt ";
    put_le32(out, 16);    // format chunk size
    put_le16(out, 1);     // PCM
    put_le16(out, 1);     // channels
    put_le32(out, 48000); // sample rate
    put_le32(out, 96000); // byte rate
    put_le16(out, 2);     // block alignment
    put_le16(out, 16);    // bits per sample
    out << "data";
    put_le32(out, bytes);
    for (const auto sample : audio) put_le16(out, static_cast<std::uint16_t>(sample));
    return out ? std::error_code{} : stream_error();
}
inline std::error_code write_wav(const std::string &path, std::span<const std::int16_t> audio) {
    return write_file(path, [audio](std::ostream &out) { return write_wav(out, audio); });
}

} // namespace lab
