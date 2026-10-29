#pragma once
// Shared by sched-work and sched-load: exit statuses, scheduling policies, monotonic clocks,
// stop signals and the one-line JSON results for skips and errors.
#include <cerrno>
#include <csignal>
#include <cstdint>
#include <ctime>
#include <iostream>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>

#include <sched.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "json.hpp"

#ifndef SCHED_DEADLINE
#define SCHED_DEADLINE 6
#endif

namespace lab {

// Exit statuses shared with the Python runner.
namespace exit_code {
inline constexpr int ok = 0;
inline constexpr int failure = 1;
inline constexpr int invalid = 2;
inline constexpr int skipped = 77;
} // namespace exit_code

enum class SchedulingPolicy { other, batch, idle, fifo, rr, deadline };

inline std::optional<SchedulingPolicy> parse_policy(std::string_view value) {
    if (value == "other") return SchedulingPolicy::other;
    if (value == "batch") return SchedulingPolicy::batch;
    if (value == "idle") return SchedulingPolicy::idle;
    if (value == "fifo") return SchedulingPolicy::fifo;
    if (value == "rr") return SchedulingPolicy::rr;
    if (value == "deadline") return SchedulingPolicy::deadline;
    return std::nullopt;
}
inline int policy_id(SchedulingPolicy policy) {
    switch (policy) {
        case SchedulingPolicy::other: return SCHED_OTHER;
        case SchedulingPolicy::batch: return SCHED_BATCH;
        case SchedulingPolicy::idle: return SCHED_IDLE;
        case SchedulingPolicy::fifo: return SCHED_FIFO;
        case SchedulingPolicy::rr: return SCHED_RR;
        case SchedulingPolicy::deadline: return SCHED_DEADLINE;
    }
    return -1;
}
inline bool is_real_time(SchedulingPolicy policy) {
    return policy == SchedulingPolicy::fifo || policy == SchedulingPolicy::rr ||
           policy == SchedulingPolicy::deadline;
}
inline std::string_view policy_name(SchedulingPolicy policy) {
    switch (policy) {
        case SchedulingPolicy::other: return "other";
        case SchedulingPolicy::batch: return "batch";
        case SchedulingPolicy::idle: return "idle";
        case SchedulingPolicy::fifo: return "fifo";
        case SchedulingPolicy::rr: return "rr";
        case SchedulingPolicy::deadline: return "deadline";
    }
    return "unknown";
}
inline std::string_view policy_name(int policy) {
    switch (policy) {
        case SCHED_OTHER: return "other";
        case SCHED_BATCH: return "batch";
        case SCHED_IDLE: return "idle";
        case SCHED_FIFO: return "fifo";
        case SCHED_RR: return "rr";
        case SCHED_DEADLINE: return "deadline";
        default: return "unknown";
    }
}

// musl leaves the process-oriented sched_* wrappers as ENOSYS stubs.
// These Linux syscalls deliberately address the current thread (TID 0).
namespace thread_scheduler {
inline int policy() { return static_cast<int>(syscall(SYS_sched_getscheduler, 0)); }
inline int parameters(sched_param &param) { return static_cast<int>(syscall(SYS_sched_getparam, 0, &param)); }
inline int set(int policy, const sched_param &param) {
    return static_cast<int>(syscall(SYS_sched_setscheduler, 0, policy, &param));
}
} // namespace thread_scheduler

// Set by SIGTERM/SIGINT; loops and sleeps check it.
inline volatile std::sig_atomic_t interrupted = 0;
inline void handle_signal(int signal_number) { interrupted = signal_number; }
inline int install_stop_handlers() {
    struct sigaction action{};
    action.sa_handler = handle_signal;
    if (sigemptyset(&action.sa_mask) || sigaction(SIGTERM, &action, nullptr) ||
        sigaction(SIGINT, &action, nullptr))
        return -1;
    return 0;
}

// POSIX clocks rather than std::chrono: CLOCK_MONOTONIC values are shared with the runner's
// time.monotonic_ns(), and CLOCK_THREAD_CPUTIME_ID has no std::chrono equivalent.
struct ClockReading {
    std::uint64_t nanoseconds = 0;
    int error = 0;
    explicit operator bool() const noexcept { return error == 0; }
};
inline ClockReading read_clock(clockid_t clock_id) noexcept {
    timespec timestamp{};
    if (clock_gettime(clock_id, &timestamp)) return {.error = errno};
    return {.nanoseconds = static_cast<std::uint64_t>(timestamp.tv_sec) * 1000000000ULL +
                           static_cast<std::uint64_t>(timestamp.tv_nsec)};
}
// An absolute release time, so periodic jobs never accumulate drift.
inline int sleep_until(std::uint64_t ns) {
    const timespec ts = {static_cast<std::time_t>(ns / 1000000000ULL), static_cast<long>(ns % 1000000000ULL)};
    int rc;
    do {
        rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, nullptr);
    } while (rc == EINTR && !interrupted);
    return rc;
}

// The one-line JSON result for runs that produce no measurements.
inline int report(std::string_view state, std::string_view reason, int code) {
    json::Object(std::cout).field("schema_version", 1).field("status", state).field("reason", reason);
    std::cout << '\n';
    return code;
}
inline int report_error(std::string_view reason) { return report("error", reason, exit_code::failure); }
inline int report_skip(std::string_view reason) { return report("skipped", reason, exit_code::skipped); }
// A failed system call: unavailable optional capabilities are skips, everything else is an error.
inline int failure(std::string_view action, int error, bool optional) {
    const std::string reason = std::string(action) + ": " + std::generic_category().message(error) + " (" +
                               std::to_string(error) + ")";
    return optional ? report_skip(reason) : report_error(reason);
}
// Permission, missing kernel support or a busy resource: the capability is unavailable, not broken.
inline bool optional_errno(int e) {
    return e == EPERM || e == EACCES || e == ENOSYS || e == EBUSY || e == EINVAL || e == ENOTSUP;
}

// Reports output errors, including earlier ones the stream has already recorded. std::cout stays
// synchronized with stdio, so it buffers nothing of its own across fork() and errno reflects the
// failed write.
inline int flush_stdout(int result) {
    errno = 0;
    std::cout.flush();
    if (std::cout) return result;
    const int output_error = errno ? errno : EIO;
    std::cerr << "write standard output: " << std::generic_category().message(output_error) << " ("
              << output_error << ")\n";
    return result == exit_code::ok ? exit_code::failure : result;
}

} // namespace lab
