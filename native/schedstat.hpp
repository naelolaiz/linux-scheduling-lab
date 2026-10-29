#pragma once
// Contention around a measurement window, from procfs:
// - /proc/thread-self/schedstat: the measured thread's own run time and runqueue wait (ns, exact;
//   the thread sleeps when the window opens and runs when it closes, so no interval is split);
// - /proc/stat: busy time of the measured CPU and of its SMT siblings, sampled at the scheduler tick
//   (USER_HZ), which stays accurate over windows of hundreds of milliseconds.
#include <array>
#include <cstdint>
#include <fstream>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include <unistd.h>

#include "json.hpp"
#include "options.hpp"

namespace lab::schedstat {

struct ThreadCounters {
    std::uint64_t run_ns = 0;  // time spent running
    std::uint64_t wait_ns = 0; // time spent runnable but waiting on a runqueue
};

inline std::optional<ThreadCounters> thread() {
    std::ifstream file("/proc/thread-self/schedstat");
    ThreadCounters counters;
    if (file >> counters.run_ns >> counters.wait_ns) return counters;
    return std::nullopt;
}

// Busy ticks of one CPU: user, nice, system, irq, softirq and steal (guest time is inside user).
inline std::optional<std::uint64_t> busy_ticks(int cpu) {
    std::ifstream file("/proc/stat");
    const std::string wanted = "cpu" + std::to_string(cpu);
    std::string line;
    while (std::getline(file, line)) {
        std::istringstream fields(line);
        std::string name;
        if (!(fields >> name) || name != wanted) continue;
        // user nice system idle iowait irq softirq steal
        std::array<std::uint64_t, 8> ticks{};
        for (std::uint64_t &value : ticks)
            if (!(fields >> value)) return std::nullopt;
        const auto [user, nice, system, idle, iowait, irq, softirq, steal] = ticks;
        return user + nice + system + irq + softirq + steal;
    }
    return std::nullopt;
}

// Other hardware threads of `cpu`'s core.
inline std::vector<int> smt_siblings(int cpu) {
    std::ifstream file("/sys/devices/system/cpu/cpu" + std::to_string(cpu) +
                       "/topology/thread_siblings_list");
    std::string text;
    std::vector<int> siblings;
    if (!std::getline(file, text)) return siblings;
    for (const int other : lab::options::cpu_list(text).value_or(std::vector<int>{}))
        if (other != cpu) siblings.push_back(other);
    return siblings;
}

// A measurement window: counters are read when it opens and when `close` is called, outside the
// measured jobs, and `write` reports the differences as "contention".
class Window {
public:
    Window(int cpu, std::uint64_t start_ns) : cpu_(cpu), start_ns_(start_ns), thread_start_(thread()) {
        if (cpu_ < 0) return;
        cpus_.push_back(cpu_);
        for (const int sibling : smt_siblings(cpu_)) cpus_.push_back(sibling);
        for (const int each : cpus_) busy_start_.push_back(busy_ticks(each));
    }

    void close(std::uint64_t end_ns) {
        end_ns_ = end_ns;
        thread_end_ = thread();
        for (const int each : cpus_) busy_end_.push_back(busy_ticks(each));
    }

    void write(json::Object &result) const {
        auto section = result.object("contention");
        if (!thread_start_ || !thread_end_) {
            section.field("status", "unavailable").field("reason", "no /proc/thread-self/schedstat");
            return;
        }
        const std::uint64_t window_ns = end_ns_ - start_ns_;
        const std::uint64_t own_run_ns = thread_end_->run_ns - thread_start_->run_ns;
        section.field("status", "ok")
            .field("window_ns", window_ns)
            .field("run_ns", own_run_ns)
            .field("runqueue_wait_ns", thread_end_->wait_ns - thread_start_->wait_ns);
        const auto busy_ns = [this](std::size_t index) -> std::optional<std::uint64_t> {
            if (index >= busy_start_.size() || index >= busy_end_.size() || !busy_start_[index] ||
                !busy_end_[index])
                return std::nullopt;
            return (*busy_end_[index] - *busy_start_[index]) * tick_ns();
        };
        const auto own_cpu = busy_ns(0);
        section.field("cpu", cpu_ >= 0 ? std::optional<int>(cpu_) : std::nullopt)
            .field("tick_ns", tick_ns())
            .field("cpu_busy_ns", own_cpu)
            .field("cpu_others_run_ns",
                   own_cpu ? std::optional<std::uint64_t>(*own_cpu > own_run_ns ? *own_cpu - own_run_ns : 0)
                           : std::nullopt);
        std::vector<int> sibling_cpus;
        std::vector<std::optional<std::uint64_t>> sibling_busy;
        for (std::size_t index = 1; index < cpus_.size(); index++) {
            sibling_cpus.push_back(cpus_[index]);
            sibling_busy.push_back(busy_ns(index));
        }
        section.field("smt_siblings", sibling_cpus).field("smt_siblings_busy_ns", sibling_busy);
    }

private:
    static std::uint64_t tick_ns() {
        const long ticks_per_second = sysconf(_SC_CLK_TCK);
        return ticks_per_second > 0 ? 1000000000ULL / static_cast<std::uint64_t>(ticks_per_second)
                                    : 10000000ULL;
    }

    int cpu_;
    std::uint64_t start_ns_;
    std::uint64_t end_ns_ = 0;
    std::optional<ThreadCounters> thread_start_;
    std::optional<ThreadCounters> thread_end_;
    std::vector<int> cpus_; // the measured CPU first, then its SMT siblings
    std::vector<std::optional<std::uint64_t>> busy_start_;
    std::vector<std::optional<std::uint64_t>> busy_end_;
};

} // namespace lab::schedstat
