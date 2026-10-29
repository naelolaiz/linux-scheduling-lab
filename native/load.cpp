// sched-load: bounded, synthetic background load for the loaded-machine presets. One process runs
// worker threads pinned round-robin to --cpus until SIGTERM/SIGINT or --max-duration-ms, then prints
// what it actually achieved as JSON. RT policies are limited to the duty-cycled "rt" kind.
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <iostream>
#include <latch>
#include <memory>
#include <mutex>
#include <optional>
#include <random>
#include <semaphore>
#include <span>
#include <stop_token>
#include <string>
#include <string_view>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <poll.h>
#include <sched.h>
#include <spawn.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

#include "build_info.hpp"
#include "common.hpp"
#include "json.hpp"
#include "options.hpp"

extern char **environ; // NOLINT(readability-redundant-declaration): POSIX declares it nowhere else.

// POSIX remains where C++ has no equivalent: per-thread affinity, nice and policy, posix_spawn and
// waitpid for process churn, pipes with poll for kernel-mediated IPC, and fsync for durable writes.
namespace {

namespace exit_code = lab::exit_code;
using lab::failure;
using lab::report_error;
using lab::report_skip;
using lab::SchedulingPolicy;
using Clock = std::chrono::steady_clock;

enum class LoadKind { cpu, wakeups, bursts, memory, fork, pipe, futex, io, rt, noop };

std::optional<LoadKind> parse_kind(std::string_view value) {
    if (value == "cpu") return LoadKind::cpu;
    if (value == "wakeups") return LoadKind::wakeups;
    if (value == "bursts") return LoadKind::bursts;
    if (value == "memory") return LoadKind::memory;
    if (value == "fork") return LoadKind::fork;
    if (value == "pipe") return LoadKind::pipe;
    if (value == "futex") return LoadKind::futex;
    if (value == "io") return LoadKind::io;
    if (value == "rt") return LoadKind::rt;
    if (value == "noop") return LoadKind::noop;
    return std::nullopt;
}
std::string_view kind_name(LoadKind kind) {
    switch (kind) {
        case LoadKind::cpu: return "cpu";
        case LoadKind::wakeups: return "wakeups";
        case LoadKind::bursts: return "bursts";
        case LoadKind::memory: return "memory";
        case LoadKind::fork: return "fork";
        case LoadKind::pipe: return "pipe";
        case LoadKind::futex: return "futex";
        case LoadKind::io: return "io";
        case LoadKind::rt: return "rt";
        case LoadKind::noop: return "noop";
    }
    return "unknown";
}
// What one unit of each kind's work counter means.
std::string_view work_unit(LoadKind kind) {
    switch (kind) {
        case LoadKind::cpu: return "passes";
        case LoadKind::wakeups: return "wakeups";
        case LoadKind::bursts: return "bursts";
        case LoadKind::memory: return "bytes_copied";
        case LoadKind::fork: return "processes_spawned";
        case LoadKind::pipe:
        case LoadKind::futex: return "round_trips";
        case LoadKind::io: return "bytes_written";
        case LoadKind::rt: return "periods";
        case LoadKind::noop: return "none";
    }
    return "unknown";
}

struct Options {
    LoadKind kind = LoadKind::cpu;
    std::vector<int> cpus;
    int threads = 0; // 0: a default for the kind and CPU count
    SchedulingPolicy policy = SchedulingPolicy::other;
    int nice_value = 0;
    int priority = 5;
    bool allow_rt = false;
    int max_duration_ms = 10000;
    int memory_mb = 256;
    int io_mb = 64;
    int io_mb_per_s = 32;
    std::string_view output;
    int seed = 1;
    int duty_percent = 50;
};
struct ParseState {
    Options options;
    bool output_set = false;
};
using OptionSpec = lab::options::Spec<ParseState>;
template<auto Member, long Min, long Max>
constexpr auto integer_option = lab::options::integer<ParseState, Member, Min, Max>;
template<auto Member, auto Parse>
constexpr auto choice_option = lab::options::choice<ParseState, Member, Parse>;

bool cpus_option(ParseState &state, std::string_view /*name*/, std::string_view value) {
    auto cpus = lab::options::cpu_list(value);
    if (!cpus) {
        std::cerr << "--cpus must be a list such as 0,2-5\n";
        return false;
    }
    state.options.cpus = std::move(*cpus);
    return true;
}
bool output_option(ParseState &state, std::string_view /*name*/, std::string_view value) {
    state.options.output = value;
    state.output_set = true;
    return true;
}

constexpr std::array option_table{
    OptionSpec{"kind", true, choice_option<&Options::kind, parse_kind>},
    OptionSpec{"cpus", true, cpus_option},
    OptionSpec{"threads", true, integer_option<&Options::threads, 1, 512>},
    OptionSpec{"policy", true, choice_option<&Options::policy, lab::parse_policy>},
    OptionSpec{"nice", true, integer_option<&Options::nice_value, -20, 19>},
    OptionSpec{"priority", true, integer_option<&Options::priority, 1, 20>},
    OptionSpec{"allow-rt", false, lab::options::flag<ParseState, &Options::allow_rt>},
    OptionSpec{"max-duration-ms", true, integer_option<&Options::max_duration_ms, 100, 15000>},
    OptionSpec{"memory-mb", true, integer_option<&Options::memory_mb, 1, 1024>},
    OptionSpec{"io-mb", true, integer_option<&Options::io_mb, 4, 1024>},
    OptionSpec{"io-mb-per-s", true, integer_option<&Options::io_mb_per_s, 1, 1024>},
    OptionSpec{"output", true, output_option},
    OptionSpec{"seed", true, integer_option<&Options::seed, 0, 1000000>},
    OptionSpec{"duty-percent", true, integer_option<&Options::duty_percent, 1, 50>},
};

void usage() {
    std::cout
        << "sched-load --kind cpu|wakeups|bursts|memory|fork|pipe|futex|io|rt --cpus LIST\n"
           "  [--threads 1..512] [--policy other|batch|idle|fifo|rr] [--nice -20..19]\n"
           "  [--priority 1..20] [--allow-rt] [--max-duration-ms 100..15000] [--seed N]\n"
           "  [--memory-mb 1..1024] [--io-mb 4..1024] [--io-mb-per-s 1..1024] [--output PREFIX]\n"
           "  [--duty-percent 1..50]\n"
           "Runs until SIGTERM/SIGINT or the maximum duration, then prints the achieved load.\n"
           "RT policies need --allow-rt, the rt kind, and at least one allowed CPU left without RT load.\n"
           "sched-load --build-info prints the compiler, flags and libraries of this binary.\n"
           "Exit codes: 0 ok, 77 unavailable/permission/safety skip, 2 invalid, 1 failure.\n";
}

int default_threads(LoadKind kind, std::size_t cpus) {
    const int count = static_cast<int>(cpus);
    switch (kind) {
        case LoadKind::wakeups: return std::min(512, 16 * count);
        case LoadKind::pipe:
        case LoadKind::futex: return std::min(512, 2 * count);
        case LoadKind::io: return std::min(4, count);
        default: return count;
    }
}

// Sorted, without repeats.
std::vector<int> distinct_cpus(std::vector<int> cpus) {
    std::ranges::sort(cpus);
    const auto repeats = std::ranges::unique(cpus);
    cpus.erase(repeats.begin(), repeats.end());
    return cpus;
}

// The first error any worker hit; workers record, the main thread reports.
class Errors {
public:
    void record(std::string_view action, int error, bool optional) {
        const std::scoped_lock lock(mutex_);
        if (error_) return;
        action_ = std::string(action);
        error_ = error;
        optional_ = optional;
    }
    [[nodiscard]] bool any() {
        const std::scoped_lock lock(mutex_);
        return error_ != 0;
    }
    int report() {
        const std::scoped_lock lock(mutex_);
        return failure(action_, error_, optional_);
    }

private:
    std::mutex mutex_;
    std::string action_;
    int error_ = 0;
    bool optional_ = false;
};

// Per-thread placement and scheduling, applied by the worker itself so the main thread stays an
// ordinary task that can always stop the others.
bool configure_thread(int cpu, const Options &options, Errors &errors) {
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set)) {
        errors.record("pin load thread", errno, lab::optional_errno(errno));
        return false;
    }
    if (setpriority(PRIO_PROCESS, 0, options.nice_value)) {
        errors.record("set load nice", errno, lab::optional_errno(errno));
        return false;
    }
    sched_param parameters{};
    const int policy = lab::policy_id(options.policy);
    if (policy == SCHED_FIFO || policy == SCHED_RR) parameters.sched_priority = options.priority;
    if (lab::thread_scheduler::set(policy, parameters) || lab::thread_scheduler::policy() != policy) {
        errors.record("set load scheduler", errno ? errno : EINVAL, lab::optional_errno(errno));
        return false;
    }
    return true;
}

// A small integer workload; each pass is the unit of the cpu kind's work counter.
std::uint32_t mix_pass(std::span<std::uint32_t> buffer, std::uint32_t state) {
    for (auto &value : buffer) {
        state = state * 1664525U + 1013904223U;
        value ^= state >> 7U;
    }
    return state;
}
void spin_for(std::chrono::nanoseconds duration, std::span<std::uint32_t> buffer, std::uint32_t &state,
              const std::stop_token &stop) {
    const auto until = Clock::now() + duration;
    while (Clock::now() < until && !stop.stop_requested()) state = mix_pass(buffer, state);
}
// Sleeps for `duration` unless a stop is requested first.
void pause_for(std::chrono::nanoseconds duration, const std::stop_token &stop) {
    std::mutex mutex;
    std::condition_variable_any wake;
    std::unique_lock lock(mutex);
    wake.wait_for(lock, stop, duration, [] { return false; });
}

class FileDescriptor {
public:
    explicit FileDescriptor(int fd = -1) : fd_(fd) {}
    FileDescriptor(const FileDescriptor &) = delete;
    FileDescriptor &operator=(const FileDescriptor &) = delete;
    FileDescriptor(FileDescriptor &&other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
    FileDescriptor &operator=(FileDescriptor &&other) noexcept {
        if (this != &other) {
            reset();
            fd_ = std::exchange(other.fd_, -1);
        }
        return *this;
    }
    ~FileDescriptor() { reset(); }
    [[nodiscard]] int get() const { return fd_; }

private:
    void reset() noexcept {
        if (fd_ >= 0) close(fd_);
        fd_ = -1;
    }
    int fd_;
};
struct PipePair {
    std::array<FileDescriptor, 2> ping; // [read, write]
    std::array<FileDescriptor, 2> pong;
};
std::unique_ptr<PipePair> make_pipe_pair() {
    std::array<int, 2> ping{};
    std::array<int, 2> pong{};
    if (pipe(ping.data())) return nullptr;
    if (pipe(pong.data())) {
        close(ping[0]);
        close(ping[1]);
        return nullptr;
    }
    auto pair = std::make_unique<PipePair>();
    pair->ping = {FileDescriptor(ping[0]), FileDescriptor(ping[1])};
    pair->pong = {FileDescriptor(pong[0]), FileDescriptor(pong[1])};
    return pair;
}
// Waits up to 20 ms for one byte, so a stop request is noticed even while the peer is gone.
bool receive_byte(int fd, const std::stop_token &stop) {
    pollfd waiting{.fd = fd, .events = POLLIN, .revents = 0};
    while (!stop.stop_requested()) {
        const int ready = poll(&waiting, 1, 20);
        if (ready < 0 && errno != EINTR) return false;
        if (ready > 0) {
            char byte = 0;
            return read(fd, &byte, 1) == 1;
        }
    }
    return false;
}
bool send_byte(int fd) {
    const char byte = 1;
    return write(fd, &byte, 1) == 1;
}
struct SemaphorePair {
    std::binary_semaphore ping{0};
    std::binary_semaphore pong{0};
};

struct Worker {
    int cpu = 0;
    std::uint64_t work = 0;
    std::uint64_t busy_ns = 0;
    std::uint64_t fsync_ns = 0;
    std::uint64_t fsyncs = 0;
};

void cpu_load(Worker &worker, const std::stop_token &stop) {
    std::array<std::uint32_t, 4096> buffer{};
    std::uint32_t state = static_cast<std::uint32_t>(worker.cpu) + 1U;
    while (!stop.stop_requested()) {
        state = mix_pass(buffer, state);
        worker.work++;
    }
}
// Many tasks that mostly sleep: ~100 us of work after 1-20 ms asleep, like services and desktop helpers.
void wakeup_load(Worker &worker, int seed, const std::stop_token &stop) {
    std::array<std::uint32_t, 256> buffer{};
    std::uint32_t state = 1;
    std::mt19937 random(static_cast<std::uint32_t>(seed));
    std::uniform_int_distribution<int> sleep_us(1000, 20000);
    while (!stop.stop_requested()) {
        pause_for(std::chrono::microseconds(sleep_us(random)), stop);
        spin_for(std::chrono::microseconds(100), buffer, state, stop);
        worker.work++;
    }
}
// Busy 20-100 ms, idle 100-500 ms: interactive bursts such as page loads or UI redraws.
void burst_load(Worker &worker, int seed, const std::stop_token &stop) {
    std::array<std::uint32_t, 4096> buffer{};
    std::uint32_t state = 1;
    std::mt19937 random(static_cast<std::uint32_t>(seed));
    std::uniform_int_distribution<int> busy_ms(20, 100);
    std::uniform_int_distribution<int> idle_ms(100, 500);
    while (!stop.stop_requested()) {
        const auto busy = std::chrono::milliseconds(busy_ms(random));
        const auto begin = Clock::now();
        spin_for(busy, buffer, state, stop);
        worker.busy_ns += static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - begin).count());
        worker.work++;
        pause_for(std::chrono::milliseconds(idle_ms(random)), stop);
    }
}
// Streams through a private buffer larger than the caches: shared L3 and DRAM bandwidth.
void memory_load(Worker &worker, std::span<std::byte> buffer, const std::stop_token &stop) {
    const auto half = buffer.size() / 2;
    const auto first = buffer.first(half);
    const auto second = buffer.subspan(half, half);
    bool forward = true;
    while (!stop.stop_requested()) {
        if (forward)
            std::ranges::copy(first, second.begin());
        else
            std::ranges::copy(second, first.begin());
        forward = !forward;
        worker.work += half;
    }
}
// Short-lived processes, as a build system spawns compilers: fork/exec/exit/wait.
void fork_load(Worker &worker, Errors &errors, const std::stop_token &stop) {
    std::array<char, 16> name{"sched-load"};
    std::array<char, 8> kind_flag{"--kind"};
    std::array<char, 8> noop{"noop"};
    std::array<char *, 4> argv{name.data(), kind_flag.data(), noop.data(), nullptr};
    while (!stop.stop_requested()) {
        pid_t child = 0;
        const int rc = posix_spawn(&child, "/proc/self/exe", nullptr, nullptr, argv.data(), environ);
        if (rc) {
            errors.record("spawn process", rc, lab::optional_errno(rc));
            return;
        }
        int status = 0;
        while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
        worker.work++;
    }
}
// Kernel-mediated IPC: one byte back and forth over two pipes.
void pipe_load(Worker &worker, const PipePair &pair, bool initiator, const std::stop_token &stop) {
    while (!stop.stop_requested()) {
        if (initiator) {
            if (!send_byte(pair.ping[1].get()) || !receive_byte(pair.pong[0].get(), stop)) return;
            worker.work++;
        } else {
            if (!receive_byte(pair.ping[0].get(), stop) || !send_byte(pair.pong[1].get())) return;
        }
    }
}
// The same exchange through futex-based semaphores, mostly in user space when uncontended.
void futex_load(Worker &worker, SemaphorePair &pair, bool initiator, const std::stop_token &stop) {
    constexpr auto patience = std::chrono::milliseconds(20);
    while (!stop.stop_requested()) {
        if (initiator) {
            pair.ping.release();
            while (!pair.pong.try_acquire_for(patience))
                if (stop.stop_requested()) return;
            worker.work++;
        } else {
            while (!pair.ping.try_acquire_for(patience))
                if (stop.stop_requested()) return;
            pair.pong.release();
        }
    }
}
// Durable writes at a capped rate, like package installs or logging rather than a disk benchmark:
// 1 MiB blocks, fsync every 4 MiB, rewriting a bounded file that is removed at the end.
void io_load(Worker &worker, const std::string &path, std::size_t limit, std::uint64_t bytes_per_second,
             Errors &errors, const std::stop_token &stop) {
    constexpr std::size_t block = std::size_t{1} << 20U;
    const FileDescriptor file(open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600));
    if (file.get() < 0) {
        errors.record("open load file", errno, false);
        return;
    }
    const std::vector<char> data(block, 'x');
    std::size_t offset = 0;
    std::size_t unsynced = 0;
    const auto begin = Clock::now();
    while (!stop.stop_requested()) {
        const auto due = begin + std::chrono::nanoseconds(worker.work * 1000000000ULL / bytes_per_second);
        if (const auto now = Clock::now(); due > now) {
            pause_for(due - now, stop);
            continue;
        }
        if (offset >= limit) {
            if (lseek(file.get(), 0, SEEK_SET) < 0) break;
            offset = 0;
        }
        const ssize_t written = write(file.get(), data.data(), data.size());
        if (written < 0) {
            if (errno == EINTR) continue;
            errors.record("write load file", errno, false);
            break;
        }
        offset += static_cast<std::size_t>(written);
        unsynced += static_cast<std::size_t>(written);
        worker.work += static_cast<std::uint64_t>(written);
        if (unsynced >= 4 * block) {
            const auto sync_start = Clock::now();
            if (fsync(file.get())) {
                errors.record("fsync load file", errno, false);
                break;
            }
            worker.fsync_ns += static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - sync_start).count());
            worker.fsyncs++;
            unsynced = 0;
        }
    }
    std::error_code ignored;
    std::filesystem::remove(path, ignored);
}
// Duty-cycled RT work: busy for duty% of each 10 ms period, then blocked until the next release.
void rt_load(Worker &worker, int duty_percent, const std::stop_token &stop) {
    constexpr std::uint64_t period_ns = 10000000;
    std::array<std::uint32_t, 1024> buffer{};
    std::uint32_t state = 1;
    const auto now = lab::read_clock(CLOCK_MONOTONIC);
    std::uint64_t release = now.nanoseconds + period_ns;
    while (!stop.stop_requested()) {
        if (lab::sleep_until(release)) return;
        spin_for(std::chrono::nanoseconds(period_ns * static_cast<std::uint64_t>(duty_percent) / 100), buffer,
                 state, stop);
        release += period_ns;
        worker.work++;
    }
}

std::uint64_t rusage_ns(int who) {
    rusage usage{};
    if (getrusage(who, &usage)) return 0;
    const auto to_ns = [](const timeval &time) {
        return static_cast<std::uint64_t>(time.tv_sec) * 1000000000ULL +
               static_cast<std::uint64_t>(time.tv_usec) * 1000ULL;
    };
    return to_ns(usage.ru_utime) + to_ns(usage.ru_stime);
}

int validate(const Options &options, const cpu_set_t &allowed) {
    if (options.cpus.empty()) {
        std::cerr << "--cpus is required\n";
        return exit_code::invalid;
    }
    for (const int cpu : options.cpus) {
        if (!CPU_ISSET(cpu, &allowed)) {
            std::cerr << "CPU " << cpu << " is not in the effective allowed mask\n";
            return exit_code::invalid;
        }
    }
    if (options.policy == SchedulingPolicy::deadline) {
        std::cerr << "load generators do not use SCHED_DEADLINE\n";
        return exit_code::invalid;
    }
    const bool real_time = lab::is_real_time(options.policy);
    if (real_time != (options.kind == LoadKind::rt)) {
        std::cerr << "RT policies are only for the duty-cycled rt kind, which requires one\n";
        return exit_code::invalid;
    }
    return 0;
}

int run(const Options &options) {
    cpu_set_t allowed;
    CPU_ZERO(&allowed);
    if (sched_getaffinity(0, sizeof(allowed), &allowed)) return failure("read allowed CPUs", errno, false);
    if (const int invalid = validate(options, allowed)) return invalid;
    if (options.kind == LoadKind::io && options.output.empty()) {
        std::cerr << "the io kind requires --output PREFIX for its temporary files\n";
        return exit_code::invalid;
    }
    if (options.kind == LoadKind::rt) {
        if (!options.allow_rt) return report_skip("RT load requires explicit --allow-rt");
        if (static_cast<int>(distinct_cpus(options.cpus).size()) >= CPU_COUNT(&allowed))
            return report_skip("RT load must leave at least one allowed CPU without RT load");
        rlimit limit = {200000, 500000};
        rlimit old{};
        if (getrlimit(RLIMIT_RTTIME, &old)) return failure("get RLIMIT_RTTIME", errno, false);
        limit.rlim_max = std::min(limit.rlim_max, old.rlim_max);
        limit.rlim_cur = std::min(limit.rlim_cur, limit.rlim_max);
        if (setrlimit(RLIMIT_RTTIME, &limit)) return failure("set RLIMIT_RTTIME", errno, false);
    }
    if (lab::install_stop_handlers()) return failure("install stop handlers", errno, false);

    int threads = options.threads ? options.threads : default_threads(options.kind, options.cpus.size());
    if ((options.kind == LoadKind::pipe || options.kind == LoadKind::futex) && threads % 2) threads++;
    if (threads < 1) return report_error("no load threads to start");
    const auto count = static_cast<std::size_t>(threads);
    std::vector<Worker> workers(count);
    for (std::size_t index = 0; index < count; index++)
        workers[index].cpu = options.cpus[index % options.cpus.size()];

    // Shared resources are created before any thread starts and outlive all of them.
    std::vector<std::unique_ptr<PipePair>> pipes;
    std::vector<std::unique_ptr<SemaphorePair>> semaphores;
    std::vector<std::vector<std::byte>> memory;
    for (std::size_t pair = 0; options.kind == LoadKind::pipe && pair < count / 2; pair++) {
        pipes.push_back(make_pipe_pair());
        if (!pipes.back()) return failure("create pipes", errno, false);
    }
    for (std::size_t pair = 0; options.kind == LoadKind::futex && pair < count / 2; pair++)
        semaphores.push_back(std::make_unique<SemaphorePair>());
    if (options.kind == LoadKind::memory) {
        const std::size_t bytes = std::max<std::size_t>(
            std::size_t{1} << 20U, (static_cast<std::size_t>(options.memory_mb) << 20U) / count);
        for (std::size_t index = 0; index < count; index++) memory.emplace_back(bytes, std::byte{1});
    }
    const std::size_t io_limit = std::max<std::size_t>(
        std::size_t{4} << 20U, (static_cast<std::size_t>(options.io_mb) << 20U) / count);
    const std::uint64_t io_rate = std::max<std::uint64_t>(
        1, (static_cast<std::uint64_t>(options.io_mb_per_s) << 20U) / static_cast<std::uint64_t>(count));

    Errors errors;
    std::latch configured(threads);
    const auto start = Clock::now();
    const std::uint64_t self_before = rusage_ns(RUSAGE_SELF);
    const std::uint64_t children_before = rusage_ns(RUSAGE_CHILDREN);
    {
        std::vector<std::jthread> pool;
        pool.reserve(count);
        try {
            for (std::size_t index = 0; index < count; index++) {
                pool.emplace_back([&, index](const std::stop_token &stop) {
                    Worker &worker = workers[index];
                    const bool ready = configure_thread(worker.cpu, options, errors);
                    configured.count_down();
                    if (!ready) return;
                    const int seed = options.seed + static_cast<int>(index);
                    switch (options.kind) {
                        case LoadKind::cpu: cpu_load(worker, stop); break;
                        case LoadKind::wakeups: wakeup_load(worker, seed, stop); break;
                        case LoadKind::bursts: burst_load(worker, seed, stop); break;
                        case LoadKind::memory: memory_load(worker, memory[index], stop); break;
                        case LoadKind::fork: fork_load(worker, errors, stop); break;
                        case LoadKind::pipe:
                            pipe_load(worker, *pipes[index / 2], index % 2 == 0, stop);
                            break;
                        case LoadKind::futex:
                            futex_load(worker, *semaphores[index / 2], index % 2 == 0, stop);
                            break;
                        case LoadKind::io:
                            io_load(worker,
                                    std::string(options.output) + ".load-io-" + std::to_string(index) +
                                        ".tmp",
                                    io_limit, io_rate, errors, stop);
                            break;
                        case LoadKind::rt: rt_load(worker, options.duty_percent, stop); break;
                        case LoadKind::noop: break;
                    }
                });
            }
        } catch (const std::system_error &error) {
            for (std::size_t unstarted = pool.size(); unstarted < count; unstarted++) configured.count_down();
            errors.record("start load thread", error.code().value(), false);
        }
        configured.wait();
        const auto deadline = start + std::chrono::milliseconds(options.max_duration_ms);
        while (!lab::interrupted && !errors.any() && Clock::now() < deadline)
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        for (auto &thread : pool) thread.request_stop();
    } // joins every worker
    const auto elapsed = std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - start);
    if (errors.any()) return errors.report();

    const std::uint64_t self_ns = rusage_ns(RUSAGE_SELF) - self_before;
    const std::uint64_t children_ns = rusage_ns(RUSAGE_CHILDREN) - children_before;
    const auto elapsed_ns = static_cast<std::uint64_t>(elapsed.count());
    std::uint64_t work = 0;
    std::uint64_t busy_ns = 0;
    std::uint64_t fsync_ns = 0;
    std::uint64_t fsyncs = 0;
    for (const auto &worker : workers) {
        work += worker.work;
        busy_ns += worker.busy_ns;
        fsync_ns += worker.fsync_ns;
        fsyncs += worker.fsyncs;
    }
    const std::vector<int> distinct = distinct_cpus(options.cpus);
    {
        lab::json::Object result(std::cout);
        result.field("schema_version", 1).field("status", "ok").field("kind", kind_name(options.kind));
        lab::write_build(result);
        result.field("threads", threads).field("cpus", distinct);
        result.object("requested")
            .field("policy", lab::policy_name(options.policy))
            .field("nice", options.nice_value)
            .field("priority", lab::is_real_time(options.policy) ? options.priority : 0);
        result.field("elapsed_ns", elapsed_ns).field("cpu_ns", self_ns).field("children_cpu_ns", children_ns);
        auto work_section = result.object("work");
        work_section.field("unit", work_unit(options.kind)).field("count", work);
        if (options.kind == LoadKind::bursts) work_section.field("busy_ns", busy_ns);
        if (options.kind == LoadKind::io) work_section.field("fsyncs", fsyncs).field("fsync_ns", fsync_ns);
    }
    std::cout << '\n';
    return exit_code::ok;
}

int guarded_run(std::span<char *const> args) {
    ParseState state;
    const auto status = lab::options::parse(args, option_table, state);
    if (status == lab::options::Status::help) {
        usage();
        return lab::flush_stdout(exit_code::ok);
    }
    if (status == lab::options::Status::build_info) return lab::print_build_info();
    if (status == lab::options::Status::invalid) return exit_code::invalid;
    if (state.options.kind == LoadKind::noop) return exit_code::ok; // the fork kind's short-lived child
    return lab::flush_stdout(run(state.options));
}

} // namespace

int main(int argc, char **argv) {
    try {
        return guarded_run(std::span<char *const>(argv, static_cast<std::size_t>(argc)));
    } catch (const std::exception &error) {
        std::cerr << "sched-load: " << error.what() << '\n';
        return exit_code::failure;
    }
}
