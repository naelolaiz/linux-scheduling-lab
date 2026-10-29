#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <ctime>
#include <exception>
#include <iostream>
#include <new>
#include <optional>
#include <ostream>
#include <semaphore>
#include <span>
#include <string>
#include <string_view>
#include <system_error>
#include <thread>
#include <vector>

#include <pthread.h>
#include <sched.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include "build_info.hpp"
#include "common.hpp"
#include "json.hpp"
#include "media.hpp"
#include "options.hpp"
#include "schedstat.hpp"

// POSIX remains where C++ has no equivalent: CLOCK_MONOTONIC/CLOCK_THREAD_CPUTIME_ID readings and
// absolute clock_nanosleep releases (shared with the runner's time.monotonic_ns()), fork/waitpid
// supervision, scheduler and affinity syscalls, mmap/mlock page probes, and pthreads created with
// explicit FIFO attributes around a priority-inheritance mutex (std::thread/std::mutex cannot).
namespace {

namespace exit_code = lab::exit_code;
using lab::failure;
using lab::flush_stdout;
using lab::interrupted;
using lab::optional_errno;
using lab::policy_id;
using lab::policy_name;
using lab::read_clock;
using lab::report_error;
using lab::report_skip;
using lab::SchedulingPolicy;
using lab::sleep_until;
namespace thread_scheduler = lab::thread_scheduler;

// Keeps the prewarm loop's result observable so the compiler cannot drop the work.
volatile std::uint32_t prewarm_sink = 0;

constexpr int WIDTH = 160;
constexpr int HEIGHT = 90;
constexpr int PIXELS = WIDTH * HEIGHT;
constexpr int MAX_SAMPLES = 20000;
constexpr std::size_t MEMORY_BYTES = std::size_t{16} * 1024 * 1024;

enum class WorkloadMode { offline, audio, video, lock };
enum class MemoryStrategy { demand, prefault, locked };
enum class LockProtocol { none, inherit };

struct Options {
    WorkloadMode mode = WorkloadMode::offline;
    SchedulingPolicy policy = SchedulingPolicy::other;
    std::string_view output;
    MemoryStrategy memory = MemoryStrategy::prefault;
    LockProtocol protocol = LockProtocol::none;
    int jobs = 40;
    int work = 4;
    int cpu = -1;
    int nice_value = 0;
    int priority = 10;
    bool allow_rt = false;
    int warmup = 2;
    int period_us = 10000;
    int runtime_us = 1000;
    int deadline_us = 10000;
    int duration_ms = 0;
    int timeout_ms = 8000;
    int prewarm_ms = 0;
    std::uint64_t start_ns = 0;
};
struct Sample {
    std::uint64_t release = 0;
    std::uint64_t start = 0;
    std::uint64_t finish = 0;
    std::uint64_t cpu = 0;
};
// Linux's version-0 sched_setattr ABI, independent of libc header version.
struct lab_sched_attr {
    std::uint32_t size;
    std::uint32_t policy;
    std::uint64_t flags;
    std::int32_t nice_value;
    std::uint32_t priority;
    std::uint64_t runtime;
    std::uint64_t deadline;
    std::uint64_t period;
};
class Mapping {
    void *address_ = MAP_FAILED;

public:
    Mapping() = default;
    Mapping(const Mapping &) = delete;
    Mapping &operator=(const Mapping &) = delete;
    ~Mapping() {
        if (address_ != MAP_FAILED) munmap(address_, MEMORY_BYTES);
    }
    bool allocate() {
        address_ = mmap(nullptr, MEMORY_BYTES, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        return address_ != MAP_FAILED;
    }
    [[nodiscard]] void *data() const { return address_; }
    volatile unsigned char &operator[](std::size_t offset) {
        return static_cast<volatile unsigned char *>(address_)[offset];
    }
};
struct buffers {
    std::array<std::vector<std::uint8_t>, 2> planes;
    std::span<std::uint8_t> image, scratch;
    std::vector<std::int16_t> audio;
    Mapping pages;
    std::vector<Sample> samples;
    std::size_t frames = 0, page_size = 0;
    std::array<double, 128> filter{};
    std::uint64_t checksum = 0;
};
struct Effective {
    int policy = SCHED_OTHER;
    int nice_value = 0;
    sched_param param{};
    cpu_set_t cpus{};
    lab_sched_attr attr{};
    std::uint64_t rr_quantum_ns = 0;
};
std::optional<WorkloadMode> parse_mode(std::string_view value) {
    if (value == "offline") return WorkloadMode::offline;
    if (value == "audio") return WorkloadMode::audio;
    if (value == "video") return WorkloadMode::video;
    if (value == "lock") return WorkloadMode::lock;
    return std::nullopt;
}
std::optional<MemoryStrategy> parse_memory(std::string_view value) {
    if (value == "demand") return MemoryStrategy::demand;
    if (value == "prefault") return MemoryStrategy::prefault;
    if (value == "mlock") return MemoryStrategy::locked;
    return std::nullopt;
}
std::optional<LockProtocol> parse_protocol(std::string_view value) {
    if (value == "none") return LockProtocol::none;
    if (value == "inherit") return LockProtocol::inherit;
    return std::nullopt;
}
std::string_view mode_name(WorkloadMode mode) {
    switch (mode) {
        case WorkloadMode::offline: return "offline";
        case WorkloadMode::audio: return "audio";
        case WorkloadMode::video: return "video";
        case WorkloadMode::lock: return "lock";
    }
    return "unknown";
}
std::string_view memory_name(MemoryStrategy memory) {
    switch (memory) {
        case MemoryStrategy::demand: return "demand";
        case MemoryStrategy::prefault: return "prefault";
        case MemoryStrategy::locked: return "mlock";
    }
    return "unknown";
}
std::string_view protocol_name(LockProtocol protocol) {
    return protocol == LockProtocol::inherit ? "inherit" : "none";
}
void usage() {
    std::cout << "sched-work --output PREFIX [--mode offline|audio|video|lock]\n"
                 "  --policy other|batch|idle|fifo|rr|deadline --nice -20..19\n"
                 "  --cpu N --jobs 1..20000 --work 1..128 --warmup 0..100\n"
                 "  --period-us 1000..100000 --duration-ms 0..10000\n"
                 "  --start-ns MONOTONIC_NS --timeout-ms 100..15000\n"
                 "  --allow-rt --priority 1..20 --runtime-us N --deadline-us N\n"
                 "  --memory demand|prefault|mlock --protocol none|inherit\n"
                 "  --prewarm-ms 0..1000 (busy the CPU before the first release, so it starts at speed)\n"
                 "sched-work --build-info prints the compiler, flags and libraries of this binary.\n"
                 "Values follow the option (--jobs 40) or an equals sign (--jobs=40).\n"
                 "Default: 40 jobs, 4 filter passes, 10ms audio / 30ms video period.\n"
                 "Exit codes: 0 ok, 77 unavailable/permission/safety skip, 2 invalid, 1 failure.\n";
}

using ParseStatus = lab::options::Status;
struct ParseResult {
    Options options;
    ParseStatus status = ParseStatus::invalid;
};
struct ParseState {
    Options options;
    bool output_set = false;
    bool period_set = false;
};
using OptionSpec = lab::options::Spec<ParseState>;
template<auto Member, long Min, long Max>
constexpr auto integer_option = lab::options::integer<ParseState, Member, Min, Max>;
template<auto Member, auto Parse>
constexpr auto choice_option = lab::options::choice<ParseState, Member, Parse>;

bool output_option(ParseState &state, std::string_view /*name*/, std::string_view value) {
    state.options.output = value;
    state.output_set = true;
    return true;
}
bool period_option(ParseState &state, std::string_view name, std::string_view value) {
    if (!integer_option<&Options::period_us, 1000, 100000>(state, name, value)) return false;
    state.period_set = true;
    return true;
}
bool start_option(ParseState &state, std::string_view /*name*/, std::string_view value) {
    const auto [end, error] =
        std::from_chars(value.data(), value.data() + value.size(), state.options.start_ns);
    if (error == std::errc{} && end == value.data() + value.size()) return true;
    std::cerr << "start-ns must be an unsigned integer\n";
    return false;
}

constexpr std::array option_table{
    OptionSpec{"mode", true, choice_option<&Options::mode, parse_mode>},
    OptionSpec{"policy", true, choice_option<&Options::policy, lab::parse_policy>},
    OptionSpec{"output", true, output_option},
    OptionSpec{"memory", true, choice_option<&Options::memory, parse_memory>},
    OptionSpec{"protocol", true, choice_option<&Options::protocol, parse_protocol>},
    OptionSpec{"jobs", true, integer_option<&Options::jobs, 1, MAX_SAMPLES>},
    OptionSpec{"work", true, integer_option<&Options::work, 1, 128>},
    OptionSpec{"cpu", true, integer_option<&Options::cpu, 0, CPU_SETSIZE - 1>},
    OptionSpec{"nice", true, integer_option<&Options::nice_value, -20, 19>},
    OptionSpec{"priority", true, integer_option<&Options::priority, 1, 20>},
    OptionSpec{"allow-rt", false, lab::options::flag<ParseState, &Options::allow_rt>},
    OptionSpec{"warmup", true, integer_option<&Options::warmup, 0, 100>},
    OptionSpec{"period-us", true, period_option},
    OptionSpec{"runtime-us", true, integer_option<&Options::runtime_us, 100, 100000>},
    OptionSpec{"deadline-us", true, integer_option<&Options::deadline_us, 1000, 100000>},
    OptionSpec{"duration-ms", true, integer_option<&Options::duration_ms, 0, 10000>},
    OptionSpec{"timeout-ms", true, integer_option<&Options::timeout_ms, 100, 15000>},
    OptionSpec{"prewarm-ms", true, integer_option<&Options::prewarm_ms, 0, 1000>},
    OptionSpec{"start-ns", true, start_option},
};

ParseResult parse(std::span<char *const> args) {
    ParseState state;
    const ParseResult invalid{state.options, ParseStatus::invalid};
    if (const auto status = lab::options::parse(args, option_table, state); status != ParseStatus::run)
        return {state.options, status};
    Options &options = state.options;
    if (!state.period_set && options.mode == WorkloadMode::video) options.period_us = 30000;
    if (!state.output_set) {
        std::cerr << "missing --output\n";
        return invalid;
    }
    if (options.duration_ms && options.mode != WorkloadMode::offline) {
        std::cerr << "--duration-ms is only valid for offline mode\n";
        return invalid;
    }
    if (options.policy == SchedulingPolicy::deadline &&
        (options.runtime_us > options.deadline_us || options.deadline_us > options.period_us ||
         options.runtime_us * 5 > options.period_us || options.cpu >= 0 ||
         options.mode == WorkloadMode::offline || options.mode == WorkloadMode::lock)) {
        std::cerr << "DEADLINE requires periodic mode, no --cpu, runtime <= deadline <= period, "
                     "utilization <= 20%\n";
        return invalid;
    }
    if (options.mode == WorkloadMode::lock && options.policy != SchedulingPolicy::fifo) {
        std::cerr << "lock mode requires --policy fifo\n";
        return invalid;
    }
    if (options.mode == WorkloadMode::lock && options.priority > 18) {
        std::cerr << "lock base priority must be <= 18 (high thread is base + 2)\n";
        return invalid;
    }
    if (options.mode == WorkloadMode::audio &&
        static_cast<std::uint64_t>(options.jobs) * options.period_us * 48000 / 1000000 > 10000000) {
        std::cerr << "audio output is limited to 10 million samples; reduce jobs/period\n";
        return invalid;
    }
    return {options, ParseStatus::run};
}
int set_policy(const Options &options) {
    const int policy = policy_id(options.policy);
    if (setpriority(PRIO_PROCESS, 0, options.nice_value)) return -1;
    if (policy == SCHED_DEADLINE) {
#ifdef SYS_sched_setattr
        lab_sched_attr attributes{};
        attributes.size = sizeof(attributes);
        attributes.policy = SCHED_DEADLINE;
        attributes.runtime = static_cast<std::uint64_t>(options.runtime_us) * 1000;
        attributes.deadline = static_cast<std::uint64_t>(options.deadline_us) * 1000;
        attributes.period = static_cast<std::uint64_t>(options.period_us) * 1000;
        return static_cast<int>(syscall(SYS_sched_setattr, 0, &attributes, 0));
#else
        errno = ENOSYS;
        return -1;
#endif
    }
    sched_param parameters{};
    if (policy == SCHED_FIFO || policy == SCHED_RR) parameters.sched_priority = options.priority;
    return thread_scheduler::set(policy, parameters);
}
int allocate(buffers &storage, const Options &options) {
    storage.frames = static_cast<std::size_t>(options.period_us) * 48000 / 1000000;
    errno = 0;
    const long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) return failure("read page size", errno ? errno : EINVAL, false);
    storage.page_size = static_cast<std::size_t>(page_size);
    const std::size_t audio_samples =
        options.mode == WorkloadMode::audio ? static_cast<std::size_t>(options.jobs) * storage.frames : 1;
    try {
        storage.samples.resize(MAX_SAMPLES);
        for (auto &plane : storage.planes) plane.resize(PIXELS);
        storage.audio.resize(audio_samples);
    } catch (const std::bad_alloc &) { return failure("preallocate buffers", ENOMEM, false); }
    storage.image = storage.planes[0];
    storage.scratch = storage.planes[1];
    if (!storage.pages.allocate()) return failure("map page probe", errno, false);
    // Commit output buffers before changing policy; the separate page probe is intentional.
    std::ranges::fill(storage.samples, Sample{1, 1, 1, 1});
    std::ranges::fill(storage.audio, 1);
    if (options.memory != MemoryStrategy::demand)
        for (std::size_t n = 0; n < MEMORY_BYTES; n += storage.page_size) storage.pages[n] = 1;
    if (options.memory == MemoryStrategy::locked && mlock(storage.pages.data(), MEMORY_BYTES))
        return failure("mlock page probe", errno, errno == EPERM || errno == ENOMEM || errno == EAGAIN);
    return 0;
}
void video_work(buffers &storage, int job, int work) {
    for (int y = 0; y < HEIGHT; y++)
        for (int x = 0; x < WIDTH; x++)
            storage.image[y * WIDTH + x] = static_cast<std::uint8_t>(x * 3 + y * 5 + job * 7); // mod 256
    // Both ping-pong buffers must retain this frame's border, not the previous frame's.
    std::ranges::copy(storage.image, storage.scratch.begin());
    for (int p = 0; p < work; p++) {
        for (int y = 1; y < HEIGHT - 1; y++) {
            for (int x = 1; x < WIDTH - 1; x++) {
                unsigned sum = 0;
                for (int dy = -1; dy <= 1; dy++)
                    for (int dx = -1; dx <= 1; dx++) sum += storage.image[(y + dy) * WIDTH + x + dx];
                storage.scratch[y * WIDTH + x] = static_cast<std::uint8_t>(sum / 9);
            }
        }
        std::swap(storage.image, storage.scratch);
    }
    storage.checksum += storage.image[(job * 31U) % PIXELS];
}
void audio_work(buffers &storage, int job, int work, bool save) {
    for (std::size_t n = 0; n < storage.frames; n++) {
        const std::size_t sample_index = static_cast<std::size_t>(job) * storage.frames + n;
        double x = 0.6 * std::sin(6.283185307179586 * 440.0 * static_cast<double>(sample_index) / 48000.0);
        for (int p = 0; p < work; p++) {
            storage.filter[p] += 0.2 * (x - storage.filter[p]);
            x = storage.filter[p];
        }
        const auto value = static_cast<std::int16_t>(x * 30000);
        if (save) storage.audio[sample_index] = value;
        storage.checksum += static_cast<std::uint16_t>(value);
    }
}
void probe_pages(buffers &storage, int job) {
    for (std::size_t page = 0; page < 128; page++) {
        const std::size_t offset =
            ((static_cast<std::size_t>(job) * 128 + page) * storage.page_size) % MEMORY_BYTES;
        storage.pages[offset] = static_cast<unsigned char>(job);
    }
}

std::string artifact_path(std::string_view prefix, std::string_view extension) {
    std::string path(prefix);
    path += '.';
    path += extension;
    return path;
}
std::error_code write_csv(const std::string &path, std::span<const Sample> samples) {
    return lab::write_file(path, [samples](std::ostream &out) {
        out << "index,release_ns,start_ns,finish_ns,cpu_ns\n";
        for (std::size_t n = 0; n < samples.size(); n++) {
            const Sample &sample = samples[n];
            out << n << ',' << sample.release << ',' << sample.start << ',' << sample.finish << ','
                << sample.cpu << '\n';
        }
        return std::error_code{};
    });
}
std::error_code save_media(const Options &options, const buffers &storage, int count) {
    if (options.mode == WorkloadMode::audio) {
        const auto audio = std::span(storage.audio).first(static_cast<std::size_t>(count) * storage.frames);
        return lab::write_wav(artifact_path(options.output, "wav"), audio);
    }
    return lab::write_file(artifact_path(options.output, "pgm"), [&storage](std::ostream &out) {
        out << "P5\n" << WIDTH << ' ' << HEIGHT << "\n255\n";
        out.write(reinterpret_cast<const char *>(storage.image.data()), PIXELS);
        return std::error_code{};
    });
}

class PthreadMutex {
    pthread_mutex_t mutex_{};
    bool initialized_ = false;

public:
    PthreadMutex() = default;
    PthreadMutex(const PthreadMutex &) = delete;
    PthreadMutex &operator=(const PthreadMutex &) = delete;
    ~PthreadMutex() {
        if (initialized_) pthread_mutex_destroy(&mutex_);
    }

    int initialize(const pthread_mutexattr_t &attributes) noexcept {
        const int result = pthread_mutex_init(&mutex_, &attributes);
        initialized_ = result == 0;
        return result;
    }
    int lock() noexcept { return pthread_mutex_lock(&mutex_); }
    int unlock() noexcept { return pthread_mutex_unlock(&mutex_); }
    int close() noexcept {
        if (!initialized_) return 0;
        const int result = pthread_mutex_destroy(&mutex_);
        if (!result) initialized_ = false;
        return result;
    }
};

struct ThreadResult {
    std::string_view action;
    int error = 0;
    void record(std::string_view failed_action, int result) noexcept {
        if (result && !error) {
            action = failed_action;
            error = result;
        }
    }
};

struct lock_state {
    explicit lock_state(std::array<buffers, 2> &owned_buffers, int passes)
        : storage(owned_buffers), work(passes) {}
    PthreadMutex mutex;
    std::binary_semaphore locked{0}, low_go{0}, medium_go{0}, high_go{0};
    std::atomic<bool> abort{false};
    std::array<buffers, 2> &storage;
    std::array<ThreadResult, 3> results{};
    std::uint64_t high_start = 0, high_finish = 0, low_finish = 0, medium_finish = 0;
    int work;
};
void *low_thread(void *arg) {
    auto *s = static_cast<lock_state *>(arg);
    const int lock_result = s->mutex.lock();
    if (lock_result) {
        s->results[0].record("lock priority-inheritance mutex", lock_result);
        s->abort = true;
    }
    s->locked.release();
    s->low_go.acquire();
    if (!lock_result) {
        if (!s->abort)
            for (int n = 0; n < 8; n++) video_work(s->storage[0], n, s->work);
        const auto finish = read_clock(CLOCK_MONOTONIC);
        if (finish)
            s->low_finish = finish.nanoseconds;
        else
            s->results[0].record("read low-thread clock", finish.error);
        s->results[0].record("unlock priority-inheritance mutex", s->mutex.unlock());
    }
    return nullptr;
}
void *medium_thread(void *arg) {
    auto *s = static_cast<lock_state *>(arg);
    s->medium_go.acquire();
    if (!s->abort)
        for (int n = 0; n < 80; n++) video_work(s->storage[1], n, s->work);
    const auto finish = read_clock(CLOCK_MONOTONIC);
    if (finish)
        s->medium_finish = finish.nanoseconds;
    else
        s->results[1].record("read medium-thread clock", finish.error);
    return nullptr;
}
void *high_thread(void *arg) {
    auto *s = static_cast<lock_state *>(arg);
    s->high_go.acquire();
    s->medium_go.release();
    s->low_go.release();
    if (s->abort) return nullptr;
    const auto start = read_clock(CLOCK_MONOTONIC);
    if (start)
        s->high_start = start.nanoseconds;
    else
        s->results[2].record("read high-thread start clock", start.error);
    const int lock_result = s->mutex.lock();
    s->results[2].record("lock priority-inheritance mutex", lock_result);
    if (!lock_result) {
        const auto finish = read_clock(CLOCK_MONOTONIC);
        if (finish)
            s->high_finish = finish.nanoseconds;
        else
            s->results[2].record("read high-thread finish clock", finish.error);
        s->results[2].record("unlock priority-inheritance mutex", s->mutex.unlock());
    }
    return nullptr;
}
struct ThreadStart {
    bool created = false;
    std::string_view action;
    int error = 0;
};
ThreadStart start_thread(pthread_t &thread, void *(*entry)(void *), lock_state &state, int priority) {
    pthread_attr_t attributes;
    sched_param parameters{};
    parameters.sched_priority = priority;
    int result = pthread_attr_init(&attributes);
    if (result) return {false, "initialize thread attributes", result};
    std::string_view action = "configure explicit thread scheduling";
    result = pthread_attr_setinheritsched(&attributes, PTHREAD_EXPLICIT_SCHED);
    if (!result) result = pthread_attr_setschedpolicy(&attributes, SCHED_FIFO);
    if (!result) result = pthread_attr_setschedparam(&attributes, &parameters);
    if (!result) {
        action = "create FIFO thread";
        result = pthread_create(&thread, &attributes, entry, &state);
    }
    const bool created = !result;
    const int destroy_result = pthread_attr_destroy(&attributes);
    if (!result && destroy_result) return {created, "destroy thread attributes", destroy_result};
    return {created, action, result};
}
int read_effective(Effective &effective) {
    effective = Effective{};
    effective.policy = thread_scheduler::policy();
    if (effective.policy < 0) return -1;
    errno = 0;
    effective.nice_value = getpriority(PRIO_PROCESS, 0);
    if (errno || thread_scheduler::parameters(effective.param) ||
        sched_getaffinity(0, sizeof(effective.cpus), &effective.cpus))
        return -1;
    if (effective.policy == SCHED_RR) {
        timespec quantum{};
        if (sched_rr_get_interval(0, &quantum)) return -1;
        effective.rr_quantum_ns = static_cast<std::uint64_t>(quantum.tv_sec) * 1000000000ULL +
                                  static_cast<std::uint64_t>(quantum.tv_nsec);
    }
    if (effective.policy == SCHED_DEADLINE) {
#ifdef SYS_sched_getattr
        if (syscall(SYS_sched_getattr, 0, &effective.attr, sizeof(effective.attr), 0)) return -1;
#else
        errno = ENOSYS;
        return -1;
#endif
    }
    return 0;
}
// The build, requested and effective scheduler sections shared by every successful result.
void write_scheduler(lab::json::Object &result, const Options &options, const Effective &effective) {
    lab::write_build(result);
    const bool fixed_priority =
        options.policy == SchedulingPolicy::fifo || options.policy == SchedulingPolicy::rr;
    result.object("requested")
        .field("policy", policy_name(options.policy))
        .field("nice", options.nice_value)
        .field("priority", fixed_priority ? options.priority : 0)
        .field("cpu", options.cpu);
    std::vector<int> affinity;
    for (int cpu = 0; cpu < CPU_SETSIZE; cpu++)
        if (CPU_ISSET(cpu, &effective.cpus)) affinity.push_back(cpu);
    result.object("effective")
        .field("policy", policy_name(effective.policy))
        .field("nice", effective.nice_value)
        .field("priority", effective.param.sched_priority)
        .field("affinity", affinity)
        .field("rr_quantum_ns", effective.rr_quantum_ns)
        .field("runtime_ns", effective.attr.runtime)
        .field("deadline_ns", effective.attr.deadline)
        .field("period_ns", effective.attr.period);
}
int demote(const Options &options) {
    const sched_param zero{};
    if (thread_scheduler::set(SCHED_OTHER, zero) && policy_id(options.policy) != SCHED_IDLE)
        return failure("restore ordinary scheduling", errno, false);
    return 0;
}
int lock_demo(const Options &options, std::array<buffers, 2> &storage) {
    lock_state state(storage, options.work);
    pthread_mutexattr_t attr;
    int rc = pthread_mutexattr_init(&attr);
    if (rc) return failure("initialize mutex attributes", rc, false);
    rc = pthread_mutexattr_setprotocol(&attr, options.protocol == LockProtocol::inherit ? PTHREAD_PRIO_INHERIT
                                                                                        : PTHREAD_PRIO_NONE);
    if (!rc) rc = state.mutex.initialize(attr);
    const int attr_destroy_result = pthread_mutexattr_destroy(&attr);
    if (!rc) rc = attr_destroy_result;
    if (rc) return failure("mutex protocol", rc, optional_errno(rc));
    std::array<pthread_t, 3> threads{};
    using Entry = void *(*)(void *);
    const std::array<Entry, 3> entries{low_thread, medium_thread, high_thread};
    std::size_t created = 0;
    std::string_view start_action;
    for (std::size_t index = 0; index < threads.size(); index++) {
        const auto started =
            start_thread(threads[index], entries[index], state, options.priority + static_cast<int>(index));
        if (started.created) created++;
        if (started.error) {
            rc = started.error;
            start_action = started.action;
            break;
        }
    }
    if (rc) {
        state.abort = true;
        if (created == threads.size()) {
            state.high_go.release();
        } else {
            state.low_go.release();
            state.medium_go.release();
        }
    } else {
        state.locked.acquire();
        state.high_go.release();
    }
    int join_error = 0;
    for (const pthread_t thread : std::span(threads).first(created)) {
        const int result = pthread_join(thread, nullptr);
        if (!join_error && result) join_error = result;
    }
    const int mutex_destroy_result = state.mutex.close();
    if (rc) return failure(start_action, rc, optional_errno(rc));
    if (join_error) return failure("join FIFO thread", join_error, false);
    for (const auto &result : state.results)
        if (result.error) return failure(result.action, result.error, false);
    if (mutex_destroy_result)
        return failure("destroy priority-inheritance mutex", mutex_destroy_result, false);
    Effective effective;
    if (read_effective(effective)) return failure("read effective scheduler", errno, false);
    rc = demote(options);
    if (rc) return rc;
    const std::array<Sample, 1> wait{Sample{state.high_start, state.high_start, state.high_finish, 0}};
    if (const auto error = write_csv(artifact_path(options.output, "csv"), wait))
        return failure("write lock CSV", error.value(), false);
    {
        lab::json::Object result(std::cout);
        result.field("schema_version", 1).field("status", "ok").field("mode", "lock");
        write_scheduler(result, options, effective);
        result.object("lock")
            .field("protocol", protocol_name(options.protocol))
            .field("high_wait_ns", state.high_finish - state.high_start)
            .field("low_finish_ns", state.low_finish)
            .field("medium_finish_ns", state.medium_finish)
            .field("low_priority", options.priority)
            .field("medium_priority", options.priority + 1)
            .field("high_priority", options.priority + 2);
        result.field("samples", 1).field("period_ns", 0);
    }
    std::cout << '\n';
    return 0;
}
int worker(const Options &options) {
    std::array<buffers, 2> storage;
    int rc = allocate(storage[0], options);
    if (rc) return rc;
    if (options.mode == WorkloadMode::lock) {
        rc = allocate(storage[1], options);
        if (rc) return rc;
    }
    if (options.cpu >= 0) {
        cpu_set_t cpus;
        CPU_ZERO(&cpus);
        CPU_SET(options.cpu, &cpus);
        if (sched_setaffinity(0, sizeof(cpus), &cpus))
            return failure("set affinity", errno, optional_errno(errno));
    }
    if (options.allow_rt) {
        rlimit limit = {1000000, 2000000};
        rlimit old{};
        if (getrlimit(RLIMIT_RTTIME, &old)) return failure("get RLIMIT_RTTIME", errno, false);
        limit.rlim_max = std::min(limit.rlim_max, old.rlim_max);
        limit.rlim_cur = std::min(limit.rlim_cur, limit.rlim_max);
        if (setrlimit(RLIMIT_RTTIME, &limit)) return failure("set RLIMIT_RTTIME", errno, false);
    }
    // Warm up the processing code, not the demand-fault probe.
    for (int n = 0; n < options.warmup; n++) {
        if (options.mode == WorkloadMode::audio)
            audio_work(storage[0], n, options.work, false);
        else
            video_work(storage[0], n, options.work);
    }
    if (set_policy(options)) return failure("set scheduler/nice", errno, optional_errno(errno));
    if (thread_scheduler::policy() != policy_id(options.policy))
        return report_error("effective scheduler does not match request");
    if (options.mode == WorkloadMode::lock) return lock_demo(options, storage);
    const auto current = read_clock(CLOCK_MONOTONIC);
    if (!current) return failure("read monotonic clock", current.error, false);
    const std::uint64_t now = current.nanoseconds;
    const std::uint64_t origin = options.start_ns ? options.start_ns : now + 20000000ULL;
    if (origin < now || origin - now > 2000000000ULL)
        return report_error("start-ns must be in the next two seconds (warmup may have overrun it)");
    // The contention window opens 2 ms before the first release, so reading counters never delays
    // the first job and the time before it (including any prewarm) stays outside the window.
    constexpr std::uint64_t window_lead_ns = 2000000;
    const std::uint64_t open_at = origin - now > window_lead_ns ? origin - window_lead_ns : now;
    if (options.prewarm_ms) {
        // Keep this CPU busy up to the window so the clock does not start from idle.
        const std::uint64_t prewarm_ns = static_cast<std::uint64_t>(options.prewarm_ms) * 1000000;
        if (open_at - now > prewarm_ns && sleep_until(open_at - prewarm_ns))
            return report_error("initial sleep interrupted");
        std::uint32_t state = 1;
        for (auto clock = read_clock(CLOCK_MONOTONIC); clock && clock.nanoseconds < open_at && !interrupted;
             clock = read_clock(CLOCK_MONOTONIC))
            for (int step = 0; step < 1024; step++) state = state * 1664525U + 1013904223U;
        prewarm_sink = state;
    } else if (sleep_until(open_at)) {
        return report_error("initial sleep interrupted");
    }
    lab::schedstat::Window contention(options.cpu, open_at);
    if (sleep_until(origin)) return report_error("initial sleep interrupted");
    rusage before{};
    rusage after{};
    const bool before_available = getrusage(RUSAGE_SELF, &before) == 0;
    int rusage_error = before_available ? 0 : errno;
    int count = 0;
    const std::uint64_t end = origin + static_cast<std::uint64_t>(options.duration_ms) * 1000000ULL;
    const bool periodic = options.mode != WorkloadMode::offline;
    while (count < (options.duration_ms ? MAX_SAMPLES : options.jobs)) {
        Sample &sample = storage[0].samples[count];
        sample.release =
            periodic ? origin + static_cast<std::uint64_t>(count) * options.period_us * 1000 : origin;
        if (periodic && sleep_until(sample.release)) return report_error("periodic sleep interrupted");
        const auto start = read_clock(CLOCK_MONOTONIC);
        if (!start) return failure("read job start clock", start.error, false);
        sample.start = start.nanoseconds;
        if (options.duration_ms && sample.start >= end) break;
        const auto cpu_start = read_clock(CLOCK_THREAD_CPUTIME_ID);
        if (!cpu_start) return failure("read thread CPU clock", cpu_start.error, false);
        probe_pages(storage[0], count);
        if (options.mode == WorkloadMode::audio)
            audio_work(storage[0], count, options.work, true);
        else
            video_work(storage[0], count, options.work);
        const auto cpu_finish = read_clock(CLOCK_THREAD_CPUTIME_ID);
        if (!cpu_finish) return failure("read thread CPU clock", cpu_finish.error, false);
        sample.cpu = cpu_finish.nanoseconds - cpu_start.nanoseconds;
        const auto finish = read_clock(CLOCK_MONOTONIC);
        if (!finish) return failure("read job finish clock", finish.error, false);
        sample.finish = finish.nanoseconds;
        count++;
        if (interrupted) return report_error("worker interrupted");
        if (options.duration_ms && sample.finish >= end) break;
    }
    const auto window_end = read_clock(CLOCK_MONOTONIC);
    contention.close(window_end ? window_end.nanoseconds : now);
    const bool after_available = getrusage(RUSAGE_SELF, &after) == 0;
    if (!after_available && !rusage_error) rusage_error = errno;
    const bool rusage_available = before_available && after_available;
    if (options.duration_ms && count == MAX_SAMPLES && storage[0].samples[count - 1].finish < end)
        return report_error("sample capacity exceeded; increase work or shorten duration");
    // Return to ordinary scheduling before writing artifacts.
    Effective effective;
    if (read_effective(effective)) return failure("read effective scheduler", errno, false);
    rc = demote(options);
    if (rc) return rc;
    const auto measured = std::span(storage[0].samples).first(static_cast<std::size_t>(count));
    std::error_code error = write_csv(artifact_path(options.output, "csv"), measured);
    if (!error) error = save_media(options, storage[0], count);
    if (error) return failure("write artifacts", error.value(), false);
    const std::uint64_t period_ns = periodic ? static_cast<std::uint64_t>(options.period_us) * 1000 : 0;
    const std::uint64_t window_ns = static_cast<std::uint64_t>(options.duration_ms) * 1000000;
    const std::uint64_t elapsed = count ? storage[0].samples[count - 1].finish - origin : window_ns;
    using Counter = std::optional<long>;
    const auto counter = [rusage_available](long after_value, long before_value) {
        return rusage_available ? Counter(after_value - before_value) : std::nullopt;
    };
    {
        lab::json::Object result(std::cout);
        result.field("schema_version", 1).field("status", "ok").field("mode", mode_name(options.mode));
        write_scheduler(result, options, effective);
        result.field("samples", count)
            .field("period_ns", period_ns)
            .field("elapsed_ns", elapsed)
            .field("window_ns", window_ns)
            .field("checksum", storage[0].checksum)
            .field("rusage_available", rusage_available)
            .field("minor_faults", counter(after.ru_minflt, before.ru_minflt))
            .field("major_faults", counter(after.ru_majflt, before.ru_majflt))
            .field("voluntary_cs", counter(after.ru_nvcsw, before.ru_nvcsw))
            .field("involuntary_cs", counter(after.ru_nivcsw, before.ru_nivcsw))
            .field("rusage_error", rusage_available ? std::nullopt : std::optional<int>(rusage_error))
            .field("memory", memory_name(options.memory))
            .field("work", options.work)
            .field("warmup", options.warmup);
        contention.write(result);
        result.object("deadline")
            .field("runtime_us", options.runtime_us)
            .field("deadline_us", options.deadline_us)
            .field("period_us", options.period_us);
    }
    std::cout << '\n';
    return 0;
}
int kill_and_reap(pid_t child) noexcept {
    int first_error = 0;
    if (kill(child, SIGKILL) && errno != ESRCH) first_error = errno;
    for (;;) {
        if (waitpid(child, nullptr, 0) >= 0) break;
        if (errno == EINTR) continue;
        if (errno != ECHILD && !first_error) first_error = errno;
        break;
    }
    return first_error;
}

int guarded_run(std::span<char *const> args) {
    const ParseResult parsed = parse(args);
    if (parsed.status == ParseStatus::help) {
        usage();
        return flush_stdout(exit_code::ok);
    }
    if (parsed.status == ParseStatus::build_info) return lab::print_build_info();
    if (parsed.status == ParseStatus::invalid) return exit_code::invalid;
    const Options &options = parsed.options;
    const int policy = policy_id(options.policy);
    const bool real_time = policy == SCHED_FIFO || policy == SCHED_RR || policy == SCHED_DEADLINE;
    cpu_set_t allowed;
    CPU_ZERO(&allowed);
    if (sched_getaffinity(0, sizeof(allowed), &allowed))
        return failure("read allowed CPUs (CPU_SETSIZE bound)", errno, false);
    if (options.cpu >= 0 && !CPU_ISSET(options.cpu, &allowed)) {
        std::cerr << "requested CPU is not in the effective allowed mask\n";
        return exit_code::invalid;
    }
    const int initial_policy = thread_scheduler::policy();
    if (initial_policy < 0)
        return failure("inspect guardian scheduler (kernel or container syscall restriction)", errno,
                       optional_errno(errno));
    errno = 0;
    const int initial_nice = getpriority(PRIO_PROCESS, 0);
    if (errno) return failure("inspect guardian nice", errno, optional_errno(errno));
    if (initial_policy != SCHED_OTHER || initial_nice > 0)
        return report_skip("guardian requires SCHED_OTHER and nice <= 0; do not launch via chrt/nice");
    if (real_time && !options.allow_rt) return report_skip("RT requires explicit --allow-rt");
    if (real_time && CPU_COUNT(&allowed) < 2)
        return report_skip("RT requires at least two allowed CPUs for housekeeping");
    if (real_time && policy != SCHED_DEADLINE && options.cpu < 0)
        return report_skip("FIFO/RR require an explicit worker CPU, leaving another for watchdog");
    if (lab::install_stop_handlers()) return failure("install guardian signal handlers", errno, false);
    // DEADLINE inherits the original domain-wide mask, never the watchdog's pin.
    const pid_t child = fork();
    if (child < 0) return failure("fork guardian", errno, false);
    if (child == 0) {
        int result = exit_code::failure;
        try {
            result = worker(options);
        } catch (const std::bad_alloc &) { result = failure("allocate memory", ENOMEM, false); }
        // Leave without running the guardian's exit handlers or static destructors.
        std::_Exit(flush_stdout(result));
    }
    if (real_time) {
        cpu_set_t guard;
        CPU_ZERO(&guard);
        for (int i = 0; i < CPU_SETSIZE; i++)
            if (CPU_ISSET(i, &allowed) && i != options.cpu) {
                CPU_SET(i, &guard);
                break;
            }
        if (sched_setaffinity(0, sizeof(guard), &guard)) {
            const int affinity_error = errno;
            const int cleanup_error = kill_and_reap(child);
            return failure(cleanup_error ? "terminate worker after watchdog affinity failure"
                                         : "pin watchdog",
                           cleanup_error ? cleanup_error : affinity_error, false);
        }
    }
    const auto start = read_clock(CLOCK_MONOTONIC);
    if (!start) {
        const int cleanup_error = kill_and_reap(child);
        return failure(cleanup_error ? "terminate worker after clock failure" : "read watchdog clock",
                       cleanup_error ? cleanup_error : start.error, false);
    }
    const std::uint64_t stop = start.nanoseconds + static_cast<std::uint64_t>(options.timeout_ms) * 1000000;
    int wstatus = 0;
    for (;;) {
        const pid_t wait_result = waitpid(child, &wstatus, WNOHANG);
        if (wait_result == child) break;
        if (wait_result < 0 && errno != EINTR) {
            const int wait_error = errno;
            const int cleanup_error = kill_and_reap(child);
            return failure(cleanup_error ? "terminate worker after wait failure" : "wait for worker",
                           cleanup_error ? cleanup_error : wait_error, false);
        }
        const auto current = read_clock(CLOCK_MONOTONIC);
        if (!current) {
            const int cleanup_error = kill_and_reap(child);
            return failure(cleanup_error ? "terminate worker after clock failure" : "read watchdog clock",
                           cleanup_error ? cleanup_error : current.error, false);
        }
        if (interrupted || current.nanoseconds >= stop) {
            const int cleanup_error = kill_and_reap(child);
            if (cleanup_error) return failure("terminate worker", cleanup_error, false);
            return report_error(interrupted ? "interrupted; worker killed"
                                            : "wall-clock watchdog expired; worker killed");
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    if (WIFEXITED(wstatus)) return WEXITSTATUS(wstatus);
    return report_error("worker terminated by signal (possibly RLIMIT_RTTIME)");
}

} // namespace

int main(int argc, char **argv) {
    try {
        return guarded_run(std::span<char *const>(argv, static_cast<std::size_t>(argc)));
    } catch (const std::exception &error) {
        std::cerr << "sched-work: " << error.what() << '\n';
        return exit_code::failure;
    }
}
