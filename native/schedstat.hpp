#pragma once
// Scheduler accounting from procfs (CONFIG_SCHED_INFO): how long a thread waited for a CPU, and how
// much task run time a CPU served in total. Both are cumulative nanosecond counters.
#include <cstdint>
#include <fstream>
#include <optional>
#include <sstream>
#include <string>

#include "json.hpp"

namespace lab::schedstat {

struct Counters {
    std::uint64_t run_ns = 0;  // time spent running
    std::uint64_t wait_ns = 0; // time spent runnable but waiting on a runqueue
};

// The calling thread: "run_ns wait_ns timeslices".
inline std::optional<Counters> thread() {
    std::ifstream file("/proc/thread-self/schedstat");
    Counters counters;
    if (file >> counters.run_ns >> counters.wait_ns) return counters;
    return std::nullopt;
}

// One CPU's line in /proc/schedstat (version 15 and later):
// "cpuN yld_count legacy sched_count sched_goidle ttwu_count ttwu_local rq_cpu_time run_delay pcount".
inline std::optional<Counters> cpu(int number) {
    std::ifstream file("/proc/schedstat");
    const std::string wanted = "cpu" + std::to_string(number);
    std::string line;
    while (std::getline(file, line)) {
        std::istringstream fields(line);
        std::string name;
        if (!(fields >> name) || name != wanted) continue;
        std::uint64_t skipped = 0;
        for (int index = 0; index < 6; index++) fields >> skipped;
        Counters counters;
        if (fields >> counters.run_ns >> counters.wait_ns) return counters;
        return std::nullopt;
    }
    return std::nullopt;
}

// A measurement window: counters are read when it opens and when `close` is called, outside the
// measured jobs, and `write` reports the difference.
class Window {
public:
    Window(int cpu, std::uint64_t start_ns)
        : cpu_(cpu), start_ns_(start_ns), thread_start_(thread()),
          cpu_start_(cpu >= 0 ? schedstat::cpu(cpu) : std::nullopt) {}

    void close(std::uint64_t end_ns) {
        end_ns_ = end_ns;
        thread_end_ = thread();
        cpu_end_ = cpu_ >= 0 ? schedstat::cpu(cpu_) : std::nullopt;
    }

    // "contention": the measured thread's runqueue wait and, when it is pinned to one CPU, the run time
    // that every other task received on that CPU during the window.
    void write(json::Object &result) const {
        auto section = result.object("contention");
        if (!thread_start_ || !thread_end_) {
            section.field("status", "unavailable").field("reason", "no /proc/thread-self/schedstat");
            return;
        }
        const std::uint64_t own_run = thread_end_->run_ns - thread_start_->run_ns;
        section.field("status", "ok")
            .field("window_ns", end_ns_ - start_ns_)
            .field("runqueue_wait_ns", thread_end_->wait_ns - thread_start_->wait_ns);
        if (cpu_start_ && cpu_end_) {
            const std::uint64_t cpu_run = cpu_end_->run_ns - cpu_start_->run_ns;
            section.field("cpu", cpu_).field("cpu_others_run_ns", cpu_run > own_run ? cpu_run - own_run : 0);
        } else {
            section.field("cpu", nullptr).field("cpu_others_run_ns", nullptr);
        }
    }

private:
    int cpu_;
    std::uint64_t start_ns_;
    std::uint64_t end_ns_ = 0;
    std::optional<Counters> thread_start_;
    std::optional<Counters> cpu_start_;
    std::optional<Counters> thread_end_;
    std::optional<Counters> cpu_end_;
};

} // namespace lab::schedstat
