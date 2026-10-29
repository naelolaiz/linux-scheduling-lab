cmake_minimum_required(VERSION 3.21)
project(schedlab DESCRIPTION "Measured Linux scheduling lab: native C++20 workloads" LANGUAGES CXX)

# ISO C++20 (-std=c++20, not gnu++20) for every compiler and cross toolchain.
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
# The presets choose RelWithDebInfo (-O2 -g); keep that for a bare `cmake -B DIR`.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "Build type" FORCE)
endif()

option(SCHEDLAB_WERROR "Treat compiler warnings as errors" ON)
set(SCHEDLAB_SANITIZE "" CACHE STRING
    "-fsanitize= list, e.g. address,undefined or thread (sanitized timings are not measurements)")

set(THREADS_PREFER_PTHREAD_FLAG ON)
find_package(Threads REQUIRED)

# Build provenance compiled into sched-work; a flag or compiler change regenerates it.
string(TOUPPER "${CMAKE_BUILD_TYPE}" build_type_upper)
string(STRIP "${CMAKE_CXX_FLAGS} ${CMAKE_CXX_FLAGS_${build_type_upper}}" SCHEDLAB_CXX_FLAGS)
if(CMAKE_CROSSCOMPILING)
  set(SCHEDLAB_CROSS_COMPILED true)
else()
  set(SCHEDLAB_CROSS_COMPILED false)
endif()
configure_file(native/build_config.hpp.in generated/build_config.hpp @ONLY)

add_executable(sched-work native/work.cpp)
add_executable(thread-pipeline native/pipeline.cpp)
foreach(program IN ITEMS sched-work thread-pipeline)
  target_compile_options(${program} PRIVATE -Wall -Wextra -Wpedantic
                         $<$<BOOL:${SCHEDLAB_WERROR}>:-Werror>)
  target_link_libraries(${program} PRIVATE Threads::Threads m)
  target_include_directories(${program} PRIVATE ${CMAKE_CURRENT_BINARY_DIR}/generated)
  if(SCHEDLAB_SANITIZE)
    target_sources(${program} PRIVATE native/sanitizer_options.cpp)
    target_compile_options(${program} PRIVATE -fsanitize=${SCHEDLAB_SANITIZE}
                           -fno-sanitize-recover=all -fno-omit-frame-pointer)
    target_link_options(${program} PRIVATE -fsanitize=${SCHEDLAB_SANITIZE})
  endif()
endforeach()
