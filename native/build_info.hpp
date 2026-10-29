#pragma once
// How a native program was built, so results from different compilers or flags are never conflated.
#include <iostream>
#include <string>
#include <string_view>

#include "common.hpp"
#include "json.hpp"

#if __has_include("build_config.hpp")
#include "build_config.hpp"
#else
// Built without CMake: the compiler and library macros below still identify the toolchain.
namespace lab::build_config {
inline constexpr std::string_view compiler_id = "unknown (not built with CMake)";
inline constexpr std::string_view compiler_version = __VERSION__;
inline constexpr std::string_view build_type = "unknown";
inline constexpr std::string_view cxx_flags = "unknown";
inline constexpr std::string_view linker_flags = "unknown";
inline constexpr std::string_view sanitize = "unknown";
inline constexpr std::string_view target_processor = "unknown";
inline constexpr bool cross_compiled = false;
} // namespace lab::build_config
#endif

namespace lab {

inline std::string standard_library() {
#ifdef _LIBCPP_VERSION
    return "libc++ " + std::to_string(_LIBCPP_VERSION);
#elif defined(__GLIBCXX__)
    return "libstdc++ " + std::to_string(_GLIBCXX_RELEASE) + " (" + std::to_string(__GLIBCXX__) + ")";
#else
    return "unknown";
#endif
}
inline std::string c_library() {
#ifdef __GLIBC__
    return "glibc " + std::to_string(__GLIBC__) + "." + std::to_string(__GLIBC_MINOR__);
#else
    return "unknown (not glibc; musl defines no version macro)";
#endif
}
inline void write_build(json::Object &result) {
    result.object("build")
        .field("cxx_standard", __cplusplus)
        .field("compiler", __VERSION__)
        .field("compiler_id", build_config::compiler_id)
        .field("compiler_version", build_config::compiler_version)
        .field("build_type", build_config::build_type)
        .field("cxx_flags", build_config::cxx_flags)
        .field("linker_flags", build_config::linker_flags)
        .field("sanitize", build_config::sanitize)
        .field("target_processor", build_config::target_processor)
        .field("cross_compiled", build_config::cross_compiled)
        .field("standard_library", standard_library())
        .field("libc", c_library());
}
// `--build-info`: the build section alone, without running anything.
inline int print_build_info() {
    {
        json::Object result(std::cout);
        result.field("schema_version", 1).field("status", "ok");
        write_build(result);
    }
    std::cout << '\n';
    return flush_stdout(exit_code::ok);
}

} // namespace lab
