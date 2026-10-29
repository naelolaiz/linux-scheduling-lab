#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <memory>
#include <new>
#include <optional>
#include <semaphore>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include <errno.h>
#include <getopt.h>
#include <limits.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "media.hpp"

#ifndef SCHED_DEADLINE
#define SCHED_DEADLINE 6
#endif

namespace {

constexpr int WIDTH = 160;
constexpr int HEIGHT = 90;
constexpr int PIXELS = WIDTH * HEIGHT;
constexpr int MAX_SAMPLES = 20000;
constexpr size_t MEMORY_BYTES = size_t{16} * 1024 * 1024;

enum class WorkloadMode { offline, audio, video, lock };
enum class SchedulingPolicy { other, batch, idle, fifo, rr, deadline };
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
    std::uint64_t start_ns = 0;
};
struct Sample {
    std::uint64_t release = 0;
    std::uint64_t start = 0;
    std::uint64_t finish = 0;
    std::uint64_t cpu = 0;
};
/* Linux's version-0 sched_setattr ABI, independent of libc header version. */
struct lab_sched_attr {
    uint32_t size, policy;
    uint64_t flags;
    int32_t nice_value;
    uint32_t priority;
    uint64_t runtime, deadline, period;
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
    volatile unsigned char &operator[](size_t offset) {
        return static_cast<volatile unsigned char *>(address_)[offset];
    }
};
struct buffers {
    std::array<std::vector<uint8_t>, 2> planes;
    std::span<uint8_t> image, scratch;
    std::vector<int16_t> audio;
    Mapping pages;
    std::vector<Sample> samples;
    size_t frames = 0, page_size = 0;
    std::array<double, 128> filter{};
    uint64_t checksum = 0;
};
struct Effective {
    int policy = SCHED_OTHER;
    int nice_value = 0;
    sched_param param{};
    cpu_set_t cpus{};
    lab_sched_attr attr{};
    std::uint64_t rr_quantum_ns = 0;
};
volatile sig_atomic_t interrupted;
void handle_signal(int sig) { interrupted = sig; }

// musl leaves the process-oriented sched_* wrappers as ENOSYS stubs.
// These Linux syscalls deliberately address the current thread (TID 0).
namespace thread_scheduler {
int policy() { return static_cast<int>(syscall(SYS_sched_getscheduler, 0)); }
int parameters(sched_param &param) { return static_cast<int>(syscall(SYS_sched_getparam, 0, &param)); }
int set(int policy, const sched_param &param) {
    return static_cast<int>(syscall(SYS_sched_setscheduler, 0, policy, &param));
}
} // namespace thread_scheduler

struct ClockReading {
    std::uint64_t nanoseconds = 0;
    int error = 0;
    explicit operator bool() const noexcept { return error == 0; }
};

ClockReading read_clock(clockid_t clock_id) noexcept {
    timespec timestamp{};
    if (clock_gettime(clock_id, &timestamp)) return {.error = errno};
    return {.nanoseconds = static_cast<std::uint64_t>(timestamp.tv_sec) * 1000000000ULL +
                           static_cast<std::uint64_t>(timestamp.tv_nsec)};
}
int sleep_until(std::uint64_t ns) {
    const timespec ts = {static_cast<time_t>(ns / 1000000000ULL), static_cast<long>(ns % 1000000000ULL)};
    int rc;
    do {
        rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, nullptr);
    } while (rc == EINTR && !interrupted);
    return rc;
}
void json_escaped(FILE &file, std::string_view text) {
    for (const unsigned char c : text) {
        if (c == '"' || c == '\\') {
            fputc('\\', &file);
            fputc(c, &file);
        } else if (c < 32) {
            fprintf(&file, "\\u%04x", c);
        } else {
            fputc(c, &file);
        }
    }
}
void json_string(FILE &file, std::string_view text) {
    fputc('"', &file);
    json_escaped(file, text);
    fputc('"', &file);
}
int status(std::string_view state, std::string_view reason, int code) {
    printf("{\"schema_version\":1,\"status\":");
    json_string(*stdout, state);
    printf(",\"reason\":");
    json_string(*stdout, reason);
    puts("}");
    return code;
}
int failure(std::string_view action, int error, bool optional) {
    printf("{\"schema_version\":1,\"status\":\"%s\",\"reason\":\"", optional ? "skipped" : "error");
    json_escaped(*stdout, action);
    json_escaped(*stdout, ": ");
    // Only the guardian's or worker's main thread reports failures, after any lock threads joined.
    json_escaped(*stdout, strerror(error)); // NOLINT(concurrency-mt-unsafe)
    printf(" (%d)\"}\n", error);
    return optional ? 77 : 1;
}
std::optional<int> number(std::string_view input, long min, long max, std::string_view name) {
    long n = 0;
    auto [end, error] = std::from_chars(input.data(), input.data() + input.size(), n);
    if (error != std::errc{} || end != input.data() + input.size() || n < min || n > max) {
        fprintf(stderr, "%.*s must be in [%ld, %ld]\n", static_cast<int>(name.size()), name.data(), min, max);
        return std::nullopt;
    }
    return static_cast<int>(n);
}
std::optional<WorkloadMode> parse_mode(std::string_view value) {
    if (value == "offline") return WorkloadMode::offline;
    if (value == "audio") return WorkloadMode::audio;
    if (value == "video") return WorkloadMode::video;
    if (value == "lock") return WorkloadMode::lock;
    return std::nullopt;
}
std::optional<SchedulingPolicy> parse_policy(std::string_view value) {
    if (value == "other") return SchedulingPolicy::other;
    if (value == "batch") return SchedulingPolicy::batch;
    if (value == "idle") return SchedulingPolicy::idle;
    if (value == "fifo") return SchedulingPolicy::fifo;
    if (value == "rr") return SchedulingPolicy::rr;
    if (value == "deadline") return SchedulingPolicy::deadline;
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
int policy_id(SchedulingPolicy policy) {
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
const char *mode_name(WorkloadMode mode) {
    switch (mode) {
        case WorkloadMode::offline: return "offline";
        case WorkloadMode::audio: return "audio";
        case WorkloadMode::video: return "video";
        case WorkloadMode::lock: return "lock";
    }
    return "unknown";
}
const char *policy_name(SchedulingPolicy policy) {
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
const char *memory_name(MemoryStrategy memory) {
    switch (memory) {
        case MemoryStrategy::demand: return "demand";
        case MemoryStrategy::prefault: return "prefault";
        case MemoryStrategy::locked: return "mlock";
    }
    return "unknown";
}
const char *protocol_name(LockProtocol protocol) {
    return protocol == LockProtocol::inherit ? "inherit" : "none";
}
const char *policy_name(int p) {
    switch (p) {
        case SCHED_OTHER: return "other";
        case SCHED_BATCH: return "batch";
        case SCHED_IDLE: return "idle";
        case SCHED_FIFO: return "fifo";
        case SCHED_RR: return "rr";
        case SCHED_DEADLINE: return "deadline";
        default: return "unknown";
    }
}
void usage() {
    puts("sched-work --output PREFIX [--mode offline|audio|video|lock]\n"
         "  --policy other|batch|idle|fifo|rr|deadline --nice -20..19\n"
         "  --cpu N --jobs 1..20000 --work 1..128 --warmup 0..100\n"
         "  --period-us 1000..100000 --duration-ms 0..10000\n"
         "  --start-ns MONOTONIC_NS --timeout-ms 100..15000\n"
         "  --allow-rt --priority 1..20 --runtime-us N --deadline-us N\n"
         "  --memory demand|prefault|mlock --protocol none|inherit\n"
         "Default: 40 jobs, 4 filter passes, 10ms audio / 30ms video period.\n"
         "Exit codes: 0 ok, 77 unavailable/permission/safety skip, 2 invalid, 1 failure.");
}
enum class ParseStatus { run, help, invalid };
struct ParseResult {
    Options options;
    ParseStatus status = ParseStatus::invalid;
};

ParseResult parse(int argc, char **argv) {
    Options options;
    enum {
        MODE = 1000,
        POLICY,
        OUTPUT,
        MEMORY,
        PROTOCOL,
        JOBS,
        WORK,
        CPU,
        NICE,
        PRIORITY,
        ALLOW_RT,
        WARMUP,
        PERIOD,
        RUNTIME,
        DEADLINE,
        DURATION,
        TIMEOUT,
        START
    };
    const struct option opts[] = {{"mode", required_argument, nullptr, MODE},
                                  {"policy", required_argument, nullptr, POLICY},
                                  {"output", required_argument, nullptr, OUTPUT},
                                  {"memory", required_argument, nullptr, MEMORY},
                                  {"protocol", required_argument, nullptr, PROTOCOL},
                                  {"jobs", required_argument, nullptr, JOBS},
                                  {"work", required_argument, nullptr, WORK},
                                  {"cpu", required_argument, nullptr, CPU},
                                  {"nice", required_argument, nullptr, NICE},
                                  {"priority", required_argument, nullptr, PRIORITY},
                                  {"allow-rt", no_argument, nullptr, ALLOW_RT},
                                  {"warmup", required_argument, nullptr, WARMUP},
                                  {"period-us", required_argument, nullptr, PERIOD},
                                  {"runtime-us", required_argument, nullptr, RUNTIME},
                                  {"deadline-us", required_argument, nullptr, DEADLINE},
                                  {"duration-ms", required_argument, nullptr, DURATION},
                                  {"timeout-ms", required_argument, nullptr, TIMEOUT},
                                  {"start-ns", required_argument, nullptr, START},
                                  {"help", no_argument, nullptr, 'h'},
                                  {nullptr, 0, nullptr, 0}};
    bool output_set = false;
    bool period_set = false;
    int option;
    // Options are parsed once, before the guardian forks or any thread exists.
    while ((option = getopt_long(argc, argv, "h", opts, nullptr)) != -1) { // NOLINT(concurrency-mt-unsafe)
        std::optional<int> parsed_number;
        switch (option) {
            case MODE: {
                const auto value = parse_mode(optarg);
                if (!value) {
                    fputs("invalid mode\n", stderr);
                    return {options, ParseStatus::invalid};
                }
                options.mode = *value;
                break;
            }
            case POLICY: {
                const auto value = parse_policy(optarg);
                if (!value) {
                    fputs("invalid policy\n", stderr);
                    return {options, ParseStatus::invalid};
                }
                options.policy = *value;
                break;
            }
            case OUTPUT:
                options.output = optarg;
                output_set = true;
                break;
            case MEMORY: {
                const auto value = parse_memory(optarg);
                if (!value) {
                    fputs("invalid memory strategy\n", stderr);
                    return {options, ParseStatus::invalid};
                }
                options.memory = *value;
                break;
            }
            case PROTOCOL: {
                const auto value = parse_protocol(optarg);
                if (!value) {
                    fputs("invalid mutex protocol\n", stderr);
                    return {options, ParseStatus::invalid};
                }
                options.protocol = *value;
                break;
            }
            case JOBS:
                parsed_number = number(optarg, 1, MAX_SAMPLES, "jobs");
                if (parsed_number) options.jobs = *parsed_number;
                break;
            case WORK:
                parsed_number = number(optarg, 1, 128, "work");
                if (parsed_number) options.work = *parsed_number;
                break;
            case CPU:
                parsed_number = number(optarg, 0, CPU_SETSIZE - 1, "cpu");
                if (parsed_number) options.cpu = *parsed_number;
                break;
            case NICE:
                parsed_number = number(optarg, -20, 19, "nice");
                if (parsed_number) options.nice_value = *parsed_number;
                break;
            case PRIORITY:
                parsed_number = number(optarg, 1, 20, "priority");
                if (parsed_number) options.priority = *parsed_number;
                break;
            case ALLOW_RT: options.allow_rt = true; break;
            case WARMUP:
                parsed_number = number(optarg, 0, 100, "warmup");
                if (parsed_number) options.warmup = *parsed_number;
                break;
            case PERIOD:
                parsed_number = number(optarg, 1000, 100000, "period-us");
                if (parsed_number) {
                    options.period_us = *parsed_number;
                    period_set = true;
                }
                break;
            case RUNTIME:
                parsed_number = number(optarg, 100, 100000, "runtime-us");
                if (parsed_number) options.runtime_us = *parsed_number;
                break;
            case DEADLINE:
                parsed_number = number(optarg, 1000, 100000, "deadline-us");
                if (parsed_number) options.deadline_us = *parsed_number;
                break;
            case DURATION:
                parsed_number = number(optarg, 0, 10000, "duration-ms");
                if (parsed_number) options.duration_ms = *parsed_number;
                break;
            case TIMEOUT:
                parsed_number = number(optarg, 100, 15000, "timeout-ms");
                if (parsed_number) options.timeout_ms = *parsed_number;
                break;
            case START: {
                const std::string_view input(optarg);
                auto [end, error] =
                    std::from_chars(input.data(), input.data() + input.size(), options.start_ns);
                if (error != std::errc{} || end != input.data() + input.size()) {
                    fputs("start-ns must be an unsigned integer\n", stderr);
                    return {options, ParseStatus::invalid};
                }
                break;
            }
            case 'h': return {options, ParseStatus::help};
            default: return {options, ParseStatus::invalid};
        }
        if (!parsed_number.has_value() && option >= JOBS && option <= TIMEOUT && option != ALLOW_RT &&
            option != START)
            return {options, ParseStatus::invalid};
    }
    if (!period_set && options.mode == WorkloadMode::video) options.period_us = 30000;
    if (optind != argc || !output_set) {
        fputs("unexpected argument or missing --output\n", stderr);
        return {options, ParseStatus::invalid};
    }
    if (options.duration_ms && options.mode != WorkloadMode::offline) {
        fputs("--duration-ms is only valid for offline mode\n", stderr);
        return {options, ParseStatus::invalid};
    }
    if (options.policy == SchedulingPolicy::deadline &&
        (options.runtime_us > options.deadline_us || options.deadline_us > options.period_us ||
         options.runtime_us * 5 > options.period_us || options.cpu >= 0 ||
         options.mode == WorkloadMode::offline || options.mode == WorkloadMode::lock)) {
        fputs(
            "DEADLINE requires periodic mode, no --cpu, runtime <= deadline <= period, utilization <= 20%\n",
            stderr);
        return {options, ParseStatus::invalid};
    }
    if (options.mode == WorkloadMode::lock && options.policy != SchedulingPolicy::fifo) {
        fputs("lock mode requires --policy fifo\n", stderr);
        return {options, ParseStatus::invalid};
    }
    if (options.mode == WorkloadMode::lock && options.priority > 18) {
        fputs("lock base priority must be <= 18 (high thread is base + 2)\n", stderr);
        return {options, ParseStatus::invalid};
    }
    if (options.mode == WorkloadMode::audio &&
        static_cast<std::uint64_t>(options.jobs) * options.period_us * 48000 / 1000000 > 10000000) {
        fputs("audio output is limited to 10 million samples; reduce jobs/period\n", stderr);
        return {options, ParseStatus::invalid};
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
int optional_errno(int e) {
    return e == EPERM || e == EACCES || e == ENOSYS || e == EBUSY || e == EINVAL || e == ENOTSUP;
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
    /* Commit output buffers before changing policy; the separate page probe is intentional. */
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
        double x = 0.6 * sin(6.283185307179586 * 440.0 * static_cast<double>(sample_index) / 48000.0);
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
using lab::File;
File open_artifact(std::string_view prefix, std::string_view extension) {
    std::string path;
    try {
        path.reserve(prefix.size() + extension.size() + 1);
        path.append(prefix);
        path.push_back('.');
        path.append(extension);
    } catch (const std::bad_alloc &) {
        errno = ENOMEM;
        return {};
    }
    return File{fopen(path.c_str(), "wb")};
}
int save_media(const Options &options, const buffers &storage, int count) {
    File file;
    bool written = false;
    if (options.mode == WorkloadMode::audio) {
        file = open_artifact(options.output, "wav");
        if (!file) return -1;
        written = lab::write_wav(
            *file, std::span(storage.audio).first(static_cast<std::size_t>(count) * storage.frames));
    } else {
        file = open_artifact(options.output, "pgm");
        if (!file) return -1;
        written = fprintf(file.get(), "P5\n%d %d\n255\n", WIDTH, HEIGHT) >= 0 &&
                  fwrite(storage.image.data(), 1, PIXELS, file.get()) == PIXELS;
    }
    const int write_error = errno;
    const bool closed = lab::close(file);
    if (!written) errno = write_error;
    return written && closed ? 0 : -1;
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
    const char *action = nullptr;
    int error = 0;
    void record(const char *failed_action, int result) noexcept {
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
    const char *action = nullptr;
    int error = 0;
};
ThreadStart start_thread(pthread_t &thread, void *(*entry)(void *), lock_state &state, int priority) {
    pthread_attr_t attributes;
    sched_param parameters{};
    parameters.sched_priority = priority;
    int result = pthread_attr_init(&attributes);
    if (result) return {false, "initialize thread attributes", result};
    const char *action = "configure explicit thread scheduling";
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
void emit_effective(const Options &options, const Effective &effective) {
    printf("\"build\":{\"cxx_standard\":%ld,\"compiler\":", __cplusplus);
    json_string(*stdout, __VERSION__);
    printf("},");
    printf("\"requested\":{\"policy\":\"%s\",\"nice\":%d,\"priority\":%d,\"cpu\":%d},",
           policy_name(options.policy), options.nice_value,
           (options.policy == SchedulingPolicy::fifo || options.policy == SchedulingPolicy::rr)
               ? options.priority
               : 0,
           options.cpu);
    printf("\"effective\":{\"policy\":\"%s\",\"nice\":%d,\"priority\":%d,\"affinity\":[",
           policy_name(effective.policy), effective.nice_value, effective.param.sched_priority);
    int sep = 0;
    for (int cpu = 0; cpu < CPU_SETSIZE; cpu++)
        if (CPU_ISSET(cpu, &effective.cpus)) {
            printf("%s%d", sep ? "," : "", cpu);
            sep = 1;
        }
    printf("],\"rr_quantum_ns\":%llu,\"runtime_ns\":%llu,\"deadline_ns\":%llu,\"period_ns\":%llu},",
           static_cast<unsigned long long>(effective.rr_quantum_ns),
           static_cast<unsigned long long>(effective.attr.runtime),
           static_cast<unsigned long long>(effective.attr.deadline),
           static_cast<unsigned long long>(effective.attr.period));
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
    const char *start_action = nullptr;
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
    auto file = open_artifact(options.output, "csv");
    if (!file) return failure("open lock CSV", errno, false);
    const bool written =
        fprintf(file.get(), "index,release_ns,start_ns,finish_ns,cpu_ns\n0,%llu,%llu,%llu,0\n",
                static_cast<unsigned long long>(state.high_start),
                static_cast<unsigned long long>(state.high_start),
                static_cast<unsigned long long>(state.high_finish)) >= 0;
    const int write_error = errno;
    const bool closed = lab::close(file);
    if (!written) errno = write_error;
    if (!written || !closed) return failure("write lock CSV", errno, false);
    printf("{\"schema_version\":1,\"status\":\"ok\",\"mode\":\"lock\",");
    emit_effective(options, effective);
    printf("\"lock\":{\"protocol\":\"%s\",\"high_wait_ns\":%llu,"
           "\"low_finish_ns\":%llu,\"medium_finish_ns\":%llu,"
           "\"low_priority\":%d,\"medium_priority\":%d,\"high_priority\":%d},",
           protocol_name(options.protocol),
           static_cast<unsigned long long>(state.high_finish - state.high_start),
           static_cast<unsigned long long>(state.low_finish),
           static_cast<unsigned long long>(state.medium_finish), options.priority, options.priority + 1,
           options.priority + 2);
    puts("\"samples\":1,\"period_ns\":0}");
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
    /* Warm-up the processing code, not the demand-fault probe. */
    for (int n = 0; n < options.warmup; n++) {
        if (options.mode == WorkloadMode::audio)
            audio_work(storage[0], n, options.work, false);
        else
            video_work(storage[0], n, options.work);
    }
    if (set_policy(options)) return failure("set scheduler/nice", errno, optional_errno(errno));
    if (thread_scheduler::policy() != policy_id(options.policy))
        return status("error", "effective scheduler does not match request", 1);
    if (options.mode == WorkloadMode::lock) return lock_demo(options, storage);
    const auto current = read_clock(CLOCK_MONOTONIC);
    if (!current) return failure("read monotonic clock", current.error, false);
    const std::uint64_t now = current.nanoseconds;
    const std::uint64_t origin = options.start_ns ? options.start_ns : now + 20000000ULL;
    if (origin < now || origin - now > 2000000000ULL)
        return status("error", "start-ns must be in the next two seconds (warmup may have overrun it)", 1);
    if (sleep_until(origin)) return status("error", "initial sleep interrupted", 1);
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
        if (periodic && sleep_until(sample.release)) return status("error", "periodic sleep interrupted", 1);
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
        if (interrupted) return status("error", "worker interrupted", 1);
        if (options.duration_ms && sample.finish >= end) break;
    }
    const bool after_available = getrusage(RUSAGE_SELF, &after) == 0;
    if (!after_available && !rusage_error) rusage_error = errno;
    const bool rusage_available = before_available && after_available;
    if (options.duration_ms && count == MAX_SAMPLES && storage[0].samples[count - 1].finish < end)
        return status("error", "sample capacity exceeded; increase work or shorten duration", 1);
    /* Return to ordinary scheduling before writing artifacts. */
    Effective effective;
    if (read_effective(effective)) return failure("read effective scheduler", errno, false);
    rc = demote(options);
    if (rc) return rc;
    auto file = open_artifact(options.output, "csv");
    if (!file) return failure("open CSV", errno, false);
    bool csv_written = fprintf(file.get(), "index,release_ns,start_ns,finish_ns,cpu_ns\n") >= 0;
    for (int n = 0; n < count; n++) {
        const Sample &sample = storage[0].samples[n];
        if (csv_written)
            csv_written = fprintf(file.get(), "%d,%llu,%llu,%llu,%llu\n", n,
                                  static_cast<unsigned long long>(sample.release),
                                  static_cast<unsigned long long>(sample.start),
                                  static_cast<unsigned long long>(sample.finish),
                                  static_cast<unsigned long long>(sample.cpu)) >= 0;
    }
    const int csv_error = errno;
    const bool csv_closed = lab::close(file);
    if (!csv_written) errno = csv_error;
    if (!csv_written || !csv_closed || save_media(options, storage[0], count))
        return failure("write artifacts", errno ? errno : EIO, false);
    printf("{\"schema_version\":1,\"status\":\"ok\",\"mode\":\"%s\",", mode_name(options.mode));
    emit_effective(options, effective);
    const std::uint64_t period_ns = periodic ? static_cast<std::uint64_t>(options.period_us) * 1000 : 0;
    const std::uint64_t window_ns = static_cast<std::uint64_t>(options.duration_ms) * 1000000;
    const std::uint64_t elapsed = count ? storage[0].samples[count - 1].finish - origin : window_ns;
    printf("\"samples\":%d,\"period_ns\":%llu,\"elapsed_ns\":%llu,\"window_ns\":%llu,"
           "\"checksum\":%llu,\"rusage_available\":%s,",
           count, static_cast<unsigned long long>(period_ns), static_cast<unsigned long long>(elapsed),
           static_cast<unsigned long long>(window_ns), static_cast<unsigned long long>(storage[0].checksum),
           rusage_available ? "true" : "false");
    if (rusage_available) {
        printf("\"minor_faults\":%ld,\"major_faults\":%ld,\"voluntary_cs\":%ld,"
               "\"involuntary_cs\":%ld,\"rusage_error\":null,",
               after.ru_minflt - before.ru_minflt, after.ru_majflt - before.ru_majflt,
               after.ru_nvcsw - before.ru_nvcsw, after.ru_nivcsw - before.ru_nivcsw);
    } else {
        printf("\"minor_faults\":null,\"major_faults\":null,\"voluntary_cs\":null,"
               "\"involuntary_cs\":null,\"rusage_error\":%d,",
               rusage_error);
    }
    printf("\"memory\":\"%s\",\"work\":%d,\"warmup\":%d,"
           "\"deadline\":{\"runtime_us\":%d,\"deadline_us\":%d,\"period_us\":%d}}\n",
           memory_name(options.memory), options.work, options.warmup, options.runtime_us, options.deadline_us,
           options.period_us);
    return 0;
}
int flush_stdout(int result) noexcept {
    errno = 0;
    const int flush_result = fflush(stdout);
    if (!flush_result && !ferror(stdout)) return result;
    const int output_error = errno ? errno : EIO;
    // Called only from a process's main thread when it is about to exit.
    fprintf(stderr, "write standard output: %s (%d)\n",
            strerror(output_error), // NOLINT(concurrency-mt-unsafe)
            output_error);
    fflush(stderr);
    return result == 0 ? 1 : result;
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

} // namespace

int main(int argc, char **argv) {
    const ParseResult parsed = parse(argc, argv);
    if (parsed.status == ParseStatus::help) {
        usage();
        return flush_stdout(0);
    }
    if (parsed.status == ParseStatus::invalid) return 2;
    const Options &options = parsed.options;
    const int policy = policy_id(options.policy);
    const bool real_time = policy == SCHED_FIFO || policy == SCHED_RR || policy == SCHED_DEADLINE;
    cpu_set_t allowed;
    CPU_ZERO(&allowed);
    if (sched_getaffinity(0, sizeof(allowed), &allowed))
        return failure("read allowed CPUs (CPU_SETSIZE bound)", errno, false);
    if (options.cpu >= 0 && !CPU_ISSET(options.cpu, &allowed)) {
        fprintf(stderr, "requested CPU is not in the effective allowed mask\n");
        return 2;
    }
    const int initial_policy = thread_scheduler::policy();
    if (initial_policy < 0)
        return failure("inspect guardian scheduler (kernel or container syscall restriction)", errno,
                       optional_errno(errno));
    errno = 0;
    const int initial_nice = getpriority(PRIO_PROCESS, 0);
    if (errno) return failure("inspect guardian nice", errno, optional_errno(errno));
    if (initial_policy != SCHED_OTHER || initial_nice > 0)
        return status("skipped", "guardian requires SCHED_OTHER and nice <= 0; do not launch via chrt/nice",
                      77);
    if (real_time && !options.allow_rt) return status("skipped", "RT requires explicit --allow-rt", 77);
    if (real_time && CPU_COUNT(&allowed) < 2)
        return status("skipped", "RT requires at least two allowed CPUs for housekeeping", 77);
    if (real_time && policy != SCHED_DEADLINE && options.cpu < 0)
        return status("skipped", "FIFO/RR require an explicit worker CPU, leaving another for watchdog", 77);
    struct sigaction action{};
    action.sa_handler = handle_signal;
    if (sigemptyset(&action.sa_mask) || sigaction(SIGTERM, &action, nullptr) ||
        sigaction(SIGINT, &action, nullptr))
        return failure("install guardian signal handlers", errno, false);
    /* DEADLINE inherits the original domain-wide mask, never the watchdog's pin. */
    const pid_t child = fork();
    if (child < 0) return failure("fork guardian", errno, false);
    if (child == 0) {
        const int result = worker(options);
        _exit(flush_stdout(result));
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
            return status(
                "error",
                interrupted ? "interrupted; worker killed" : "wall-clock watchdog expired; worker killed", 1);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    if (WIFEXITED(wstatus)) return WEXITSTATUS(wstatus);
    return status("error", "worker terminated by signal (possibly RLIMIT_RTTIME)", 1);
}
