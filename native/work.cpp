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
#include <semaphore>
#include <span>
#include <string_view>
#include <thread>
#include <vector>
#include "media.hpp"
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

#ifndef SCHED_DEADLINE
#define SCHED_DEADLINE 6
#endif

constexpr int WIDTH = 160;
constexpr int HEIGHT = 90;
constexpr int PIXELS = WIDTH * HEIGHT;
constexpr int MAX_SAMPLES = 20000;
constexpr size_t MEMORY_BYTES = 16U * 1024U * 1024U;

struct options {
    const char *mode, *policy, *output, *memory, *protocol;
    int jobs, work, cpu, nice_value, priority, allow_rt, warmup;
    int period_us, runtime_us, deadline_us, duration_ms, timeout_ms;
    uint64_t start_ns;
};
struct sample {
    uint64_t release, start, finish, cpu;
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
    ~Mapping() { if (address_ != MAP_FAILED) munmap(address_,MEMORY_BYTES); }
    bool allocate() {
        address_ = mmap(nullptr,MEMORY_BYTES,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
        return address_ != MAP_FAILED;
    }
    void *data() const { return address_; }
    volatile unsigned char &operator[](size_t offset) {
        return static_cast<volatile unsigned char *>(address_)[offset];
    }
};
struct buffers {
    std::array<std::vector<uint8_t>,2> planes;
    std::span<uint8_t> image, scratch;
    std::vector<int16_t> audio;
    Mapping pages;
    std::vector<sample> samples;
    size_t frames = 0, page_size = 0;
    std::array<double,128> filter{};
    uint64_t checksum = 0;
};
struct effective {
    int policy, nice_value;
    struct sched_param param;
    cpu_set_t cpus;
    struct lab_sched_attr attr;
    uint64_t rr_quantum_ns;
};
static volatile sig_atomic_t interrupted;
static void handle_signal(int sig) { interrupted = sig; }

// musl leaves the process-oriented sched_* wrappers as ENOSYS stubs.
// These Linux syscalls deliberately address the current thread (TID 0).
namespace thread_scheduler {
static int policy() { return static_cast<int>(syscall(SYS_sched_getscheduler,0)); }
static int parameters(sched_param &param) {
    return static_cast<int>(syscall(SYS_sched_getparam,0,&param));
}
static int set(int policy, const sched_param &param) {
    return static_cast<int>(syscall(SYS_sched_setscheduler,0,policy,&param));
}
}

static uint64_t clock_ns(clockid_t clock_id) {
    struct timespec ts;
    if (clock_gettime(clock_id, &ts)) { perror("clock_gettime"); exit(1); }
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}
static int sleep_until(uint64_t ns) {
    struct timespec ts = {(time_t)(ns / 1000000000ULL), (long)(ns % 1000000000ULL)};
    int rc;
    do { rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL); }
    while (rc == EINTR && !interrupted);
    return rc;
}
static void json_string(FILE *f, const char *s) {
    fputc('"', f);
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') { fputc('\\', f); fputc(c, f); }
        else if (c < 32) fprintf(f, "\\u%04x", c);
        else fputc(c, f);
    }
    fputc('"', f);
}
static int status(const char *state, const char *reason, int code) {
    printf("{\"schema_version\":1,\"status\":");
    json_string(stdout, state);
    printf(",\"reason\":");
    json_string(stdout, reason);
    puts("}");
    return code;
}
static int failure(const char *action, int err, int optional) {
    char msg[256];
    snprintf(msg, sizeof(msg), "%s: %s (%d)", action, strerror(err), err);
    return status(optional ? "skipped" : "error", msg, optional ? 77 : 1);
}
static int number(const char *s, long min, long max, const char *name) {
    long n = 0;
    std::string_view input(s);
    auto [end, error] = std::from_chars(input.data(),input.data()+input.size(),n);
    if (error != std::errc{} || end != input.data()+input.size() || n < min || n > max) {
        fprintf(stderr, "%s must be in [%ld, %ld]\n", name, min, max);
        exit(2);
    }
    return (int)n;
}
static int policy_id(const char *s) {
    if (!strcmp(s, "other")) return SCHED_OTHER;
    if (!strcmp(s, "batch")) return SCHED_BATCH;
    if (!strcmp(s, "idle")) return SCHED_IDLE;
    if (!strcmp(s, "fifo")) return SCHED_FIFO;
    if (!strcmp(s, "rr")) return SCHED_RR;
    if (!strcmp(s, "deadline")) return SCHED_DEADLINE;
    return -1;
}
static const char *policy_name(int p) {
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
static void usage(void) {
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
static struct options parse(int argc, char **argv) {
    struct options o = {"offline", "other", NULL, "prefault", "none",
                        40, 4, -1, 0, 10, 0, 2, 10000, 1000, 10000, 0, 8000, 0};
    enum { MODE = 1000, POLICY, OUTPUT, MEMORY, PROTOCOL, JOBS, WORK, CPU,
           NICE, PRIORITY, ALLOW_RT, WARMUP, PERIOD, RUNTIME, DEADLINE,
           DURATION, TIMEOUT, START };
    const struct option opts[] = {
        {"mode",1,0,MODE}, {"policy",1,0,POLICY}, {"output",1,0,OUTPUT},
        {"memory",1,0,MEMORY}, {"protocol",1,0,PROTOCOL}, {"jobs",1,0,JOBS},
        {"work",1,0,WORK}, {"cpu",1,0,CPU}, {"nice",1,0,NICE},
        {"priority",1,0,PRIORITY}, {"allow-rt",0,0,ALLOW_RT},
        {"warmup",1,0,WARMUP}, {"period-us",1,0,PERIOD},
        {"runtime-us",1,0,RUNTIME}, {"deadline-us",1,0,DEADLINE},
        {"duration-ms",1,0,DURATION}, {"timeout-ms",1,0,TIMEOUT},
        {"start-ns",1,0,START}, {"help",0,0,'h'}, {0,0,0,0}
    };
    int c, period_set = 0;
    while ((c = getopt_long(argc, argv, "h", opts, NULL)) != -1) {
        switch (c) {
            case MODE: o.mode = optarg; break;
            case POLICY: o.policy = optarg; break;
            case OUTPUT: o.output = optarg; break;
            case MEMORY: o.memory = optarg; break;
            case PROTOCOL: o.protocol = optarg; break;
            case JOBS: o.jobs = number(optarg,1,MAX_SAMPLES,"jobs"); break;
            case WORK: o.work = number(optarg,1,128,"work"); break;
            case CPU: o.cpu = number(optarg,0,CPU_SETSIZE-1,"cpu"); break;
            case NICE: o.nice_value = number(optarg,-20,19,"nice"); break;
            case PRIORITY: o.priority = number(optarg,1,20,"priority"); break;
            case ALLOW_RT: o.allow_rt = 1; break;
            case WARMUP: o.warmup = number(optarg,0,100,"warmup"); break;
            case PERIOD: o.period_us = number(optarg,1000,100000,"period-us"); period_set = 1; break;
            case RUNTIME: o.runtime_us = number(optarg,100,100000,"runtime-us"); break;
            case DEADLINE: o.deadline_us = number(optarg,1000,100000,"deadline-us"); break;
            case DURATION: o.duration_ms = number(optarg,0,10000,"duration-ms"); break;
            case TIMEOUT: o.timeout_ms = number(optarg,100,15000,"timeout-ms"); break;
            case START: {
                std::string_view input(optarg);
                auto [end, error] = std::from_chars(input.data(),input.data()+input.size(),o.start_ns);
                if (error != std::errc{} || end != input.data()+input.size()) {
                    fprintf(stderr,"start-ns must be an unsigned integer\n"); exit(2);
                }
                break;
            }
            case 'h': usage(); exit(0);
            default: exit(2);
        }
    }
    if (!period_set && !strcmp(o.mode,"video")) o.period_us = 30000;
    if (optind != argc || !o.output || policy_id(o.policy) < 0 ||
        (strcmp(o.mode,"offline") && strcmp(o.mode,"audio") &&
         strcmp(o.mode,"video") && strcmp(o.mode,"lock")) ||
        (strcmp(o.memory,"demand") && strcmp(o.memory,"prefault") && strcmp(o.memory,"mlock")) ||
        (strcmp(o.protocol,"none") && strcmp(o.protocol,"inherit"))) {
        fprintf(stderr,"Invalid mode, policy, memory, protocol or missing --output\n"); exit(2);
    }
    if (o.duration_ms && strcmp(o.mode,"offline")) {
        fprintf(stderr,"--duration-ms is only valid for offline mode\n"); exit(2);
    }
    if (!strcmp(o.policy,"deadline") &&
        (o.runtime_us > o.deadline_us || o.deadline_us > o.period_us ||
         o.runtime_us * 5 > o.period_us || o.cpu >= 0 ||
         !strcmp(o.mode,"offline") || !strcmp(o.mode,"lock"))) {
        fprintf(stderr,"DEADLINE requires periodic mode, no --cpu, runtime <= deadline <= period, utilization <= 20%%\n");
        exit(2);
    }
    if (!strcmp(o.mode,"lock") && strcmp(o.policy,"fifo")) {
        fprintf(stderr,"lock mode requires --policy fifo\n"); exit(2);
    }
    if (!strcmp(o.mode,"lock") && o.priority > 18) {
        fprintf(stderr,"lock base priority must be <= 18 (high thread is base + 2)\n"); exit(2);
    }
    if (!strcmp(o.mode,"audio") && (uint64_t)o.jobs*o.period_us*48000/1000000 > 10000000) {
        fprintf(stderr,"audio output is limited to 10 million samples; reduce jobs/period\n"); exit(2);
    }
    return o;
}
static int set_policy(const struct options *o) {
    int p = policy_id(o->policy);
    if (setpriority(PRIO_PROCESS,0,o->nice_value)) return -1;
    if (p == SCHED_DEADLINE) {
#ifdef SYS_sched_setattr
        struct lab_sched_attr a;
        memset(&a,0,sizeof(a));
        a.size = sizeof(a); a.policy = SCHED_DEADLINE;
        a.runtime = (uint64_t)o->runtime_us * 1000;
        a.deadline = (uint64_t)o->deadline_us * 1000;
        a.period = (uint64_t)o->period_us * 1000;
        return (int)syscall(SYS_sched_setattr,0,&a,0);
#else
        errno = ENOSYS; return -1;
#endif
    }
    struct sched_param sp{};
    if (p == SCHED_FIFO || p == SCHED_RR) sp.sched_priority = o->priority;
    return thread_scheduler::set(p,sp);
}
static int optional_errno(int e) {
    return e == EPERM || e == EACCES || e == ENOSYS || e == EBUSY ||
           e == EINVAL || e == ENOTSUP;
}
static int allocate(struct buffers *b, const struct options *o) {
    b->frames = (size_t)o->period_us * 48000 / 1000000;
    b->page_size = (size_t)sysconf(_SC_PAGESIZE);
    size_t audio_samples = !strcmp(o->mode,"audio") ? (size_t)o->jobs*b->frames : 1;
    try {
        b->samples.resize(MAX_SAMPLES);
        for (auto &plane : b->planes) plane.resize(PIXELS);
        b->audio.resize(audio_samples);
    } catch (const std::bad_alloc &) {
        return failure("preallocate buffers",ENOMEM,0);
    }
    b->image = b->planes[0]; b->scratch = b->planes[1];
    if (!b->pages.allocate()) return failure("map page probe",errno,0);
    /* Commit output buffers before changing policy; the separate page probe is intentional. */
    std::ranges::fill(b->samples,sample{1,1,1,1});
    std::ranges::fill(b->audio,1);
    if (strcmp(o->memory,"demand"))
        for (size_t n = 0; n < MEMORY_BYTES; n += b->page_size) b->pages[n] = 1;
    if (!strcmp(o->memory,"mlock") && mlock(b->pages.data(),MEMORY_BYTES))
        return failure("mlock page probe",errno,errno == EPERM || errno == ENOMEM || errno == EAGAIN);
    return 0;
}
static void video_work(struct buffers *b, int job, int work) {
    for (int y = 0; y < HEIGHT; y++)
        for (int x = 0; x < WIDTH; x++)
            b->image[y*WIDTH+x] = (uint8_t)((x*3+y*5+job*7) & 255);
    // Both ping-pong buffers must retain this frame's border, not the previous frame's.
    std::ranges::copy(b->image,b->scratch.begin());
    for (int p = 0; p < work; p++) {
        for (int y = 1; y < HEIGHT-1; y++) {
            for (int x = 1; x < WIDTH-1; x++) {
                unsigned sum = 0;
                for (int dy = -1; dy <= 1; dy++)
                    for (int dx = -1; dx <= 1; dx++)
                        sum += b->image[(y+dy)*WIDTH+x+dx];
                b->scratch[y*WIDTH+x] = (uint8_t)(sum/9);
            }
        }
        std::swap(b->image,b->scratch);
    }
    b->checksum += b->image[(job*31U) % PIXELS];
}
static void audio_work(struct buffers *b, int job, int work, int save) {
    for (size_t n = 0; n < b->frames; n++) {
        double x = 0.6 * sin(6.283185307179586 * 440.0 *
                             ((double)job*b->frames+n) / 48000.0);
        for (int p = 0; p < work; p++) {
            b->filter[p] += 0.2 * (x-b->filter[p]);
            x = b->filter[p];
        }
        int16_t v = (int16_t)(x*30000);
        if (save) b->audio[(size_t)job*b->frames+n] = v;
        b->checksum += (uint16_t)v;
    }
}
static void probe_pages(struct buffers *b, int job) {
    for (size_t p = 0; p < 128; p++) {
        size_t off = (((size_t)job*128+p)*b->page_size) % MEMORY_BYTES;
        b->pages[off] = (unsigned char)job;
    }
}
using lab::File;
static File open_artifact(const char *prefix, const char *extension) {
    char path[PATH_MAX];
    if (snprintf(path,sizeof(path),"%s.%s",prefix,extension) >= (int)sizeof(path)) {
        errno = ENAMETOOLONG; return File{};
    }
    return File{fopen(path,"wb")};
}
static int save_media(const struct options *o, const struct buffers *b, int count) {
    File file;
    if (!strcmp(o->mode,"audio")) {
        file = open_artifact(o->output,"wav"); if (!file) return -1;
        FILE *f = file.get();
        lab::write_wav(f,std::span(b->audio).first((size_t)count*b->frames));
    } else {
        file = open_artifact(o->output,"pgm"); if (!file) return -1;
        FILE *f = file.get();
        fprintf(f,"P5\n%d %d\n255\n",WIDTH,HEIGHT);
        fwrite(b->image.data(),1,PIXELS,f);
    }
    int bad = ferror(file.get()); if (fclose(file.release())) bad = 1;
    return bad ? -1 : 0;
}

struct lock_state {
    pthread_mutex_t mutex{};
    std::binary_semaphore locked{0}, low_go{0}, medium_go{0}, high_go{0};
    std::atomic<bool> abort{false};
    buffers *b = nullptr;
    uint64_t high_start = 0, high_finish = 0, low_finish = 0, medium_finish = 0;
    int work = 0;
};
static void *low_thread(void *arg) {
    auto *s = static_cast<lock_state *>(arg);
    pthread_mutex_lock(&s->mutex);
    s->locked.release();
    s->low_go.acquire();
    if (!s->abort)
        for (int n = 0; n < 8; n++) video_work(&s->b[0],n,s->work);
    s->low_finish = clock_ns(CLOCK_MONOTONIC);
    pthread_mutex_unlock(&s->mutex);
    return NULL;
}
static void *medium_thread(void *arg) {
    auto *s = static_cast<lock_state *>(arg);
    s->medium_go.acquire();
    if (!s->abort)
        for (int n = 0; n < 80; n++) video_work(&s->b[1],n,s->work);
    s->medium_finish = clock_ns(CLOCK_MONOTONIC);
    return NULL;
}
static void *high_thread(void *arg) {
    auto *s = static_cast<lock_state *>(arg);
    s->high_go.acquire();
    if (s->abort) return nullptr;
    s->medium_go.release();
    s->low_go.release();
    s->high_start = clock_ns(CLOCK_MONOTONIC);
    pthread_mutex_lock(&s->mutex);
    s->high_finish = clock_ns(CLOCK_MONOTONIC);
    pthread_mutex_unlock(&s->mutex);
    return NULL;
}
static int start_thread(pthread_t *thread, void *(*fn)(void *), struct lock_state *s, int priority) {
    pthread_attr_t attr;
    struct sched_param param{};
    param.sched_priority = priority;
    int rc = pthread_attr_init(&attr);
    if (rc) return rc;
    if (!(rc = pthread_attr_setinheritsched(&attr,PTHREAD_EXPLICIT_SCHED)) &&
        !(rc = pthread_attr_setschedpolicy(&attr,SCHED_FIFO)) &&
        !(rc = pthread_attr_setschedparam(&attr,&param)))
        rc = pthread_create(thread,&attr,fn,s);
    pthread_attr_destroy(&attr);
    return rc;
}
static int read_effective(struct effective *e) {
    memset(e,0,sizeof(*e));
    e->policy = thread_scheduler::policy();
    if (e->policy < 0) return -1;
    errno = 0;
    e->nice_value = getpriority(PRIO_PROCESS,0);
    if (errno || thread_scheduler::parameters(e->param) ||
        sched_getaffinity(0,sizeof(e->cpus),&e->cpus)) return -1;
    if (e->policy == SCHED_RR) {
        struct timespec quantum;
        if (sched_rr_get_interval(0,&quantum)) return -1;
        e->rr_quantum_ns = (uint64_t)quantum.tv_sec*1000000000ULL+(uint64_t)quantum.tv_nsec;
    }
    if (e->policy == SCHED_DEADLINE) {
#ifdef SYS_sched_getattr
        if (syscall(SYS_sched_getattr,0,&e->attr,sizeof(e->attr),0)) return -1;
#else
        errno = ENOSYS; return -1;
#endif
    }
    return 0;
}
static void emit_effective(const struct options *o, const struct effective *e) {
    printf("\"build\":{\"cxx_standard\":%ld,\"compiler\":",static_cast<long>(__cplusplus));
    json_string(stdout,__VERSION__);
    printf("},");
    printf("\"requested\":{\"policy\":\"%s\",\"nice\":%d,\"priority\":%d,\"cpu\":%d},",
           o->policy,o->nice_value,
           (!strcmp(o->policy,"fifo") || !strcmp(o->policy,"rr")) ? o->priority : 0,o->cpu);
    printf("\"effective\":{\"policy\":\"%s\",\"nice\":%d,\"priority\":%d,\"affinity\":[",
           policy_name(e->policy),e->nice_value,e->param.sched_priority);
    int sep = 0;
    for (int cpu = 0; cpu < CPU_SETSIZE; cpu++)
        if (CPU_ISSET(cpu,&e->cpus)) { printf("%s%d",sep ? "," : "",cpu); sep = 1; }
    printf("],\"rr_quantum_ns\":%llu,\"runtime_ns\":%llu,\"deadline_ns\":%llu,\"period_ns\":%llu},",
           (unsigned long long)e->rr_quantum_ns,(unsigned long long)e->attr.runtime,
           (unsigned long long)e->attr.deadline,(unsigned long long)e->attr.period);
}
static int demote(const struct options *o) {
    struct sched_param zero{};
    if (thread_scheduler::set(SCHED_OTHER,zero) && policy_id(o->policy) != SCHED_IDLE)
        return failure("restore ordinary scheduling",errno,0);
    return 0;
}
static int lock_demo(const struct options *o, struct buffers *buffers) {
    lock_state s;
    s.b = buffers; s.work = o->work;
    pthread_mutexattr_t attr;
    int rc = pthread_mutexattr_init(&attr);
    if (rc) return failure("mutex attribute",rc,0);
    rc = pthread_mutexattr_setprotocol(&attr,!strcmp(o->protocol,"inherit") ?
                                      PTHREAD_PRIO_INHERIT : PTHREAD_PRIO_NONE);
    if (!rc) rc = pthread_mutex_init(&s.mutex,&attr);
    pthread_mutexattr_destroy(&attr);
    if (rc) return failure("mutex protocol",rc,optional_errno(rc));
    std::array<pthread_t,3> threads{};
    using Entry = void *(*)(void *);
    const std::array<Entry,3> entries{low_thread,medium_thread,high_thread};
    size_t created = 0;
    for (; created < threads.size(); created++) {
        rc = start_thread(&threads[created],entries[created],&s,o->priority+static_cast<int>(created));
        if (rc) break;
    }
    if (rc) {
        s.abort = true;
        s.low_go.release(); s.medium_go.release(); s.high_go.release();
    } else {
        s.locked.acquire();
        s.high_go.release();
    }
    for (pthread_t thread : std::span(threads).first(created)) pthread_join(thread,nullptr);
    if (rc) { pthread_mutex_destroy(&s.mutex); return failure("create FIFO thread",rc,optional_errno(rc)); }
    struct effective effective;
    if (read_effective(&effective)) return failure("read effective scheduler",errno,0);
    if ((rc = demote(o))) return rc;
    auto file = open_artifact(o->output,"csv");
    if (!file) return failure("open lock CSV",errno,0);
    FILE *f = file.get();
    fprintf(f,"index,release_ns,start_ns,finish_ns,cpu_ns\n0,%llu,%llu,%llu,0\n",
            (unsigned long long)s.high_start,(unsigned long long)s.high_start,
            (unsigned long long)s.high_finish);
    if (fclose(file.release())) return failure("write lock CSV",errno,0);
    printf("{\"schema_version\":1,\"status\":\"ok\",\"mode\":\"lock\",");
    emit_effective(o,&effective);
    printf("\"lock\":{\"protocol\":\"%s\",\"high_wait_ns\":%llu,"
           "\"low_finish_ns\":%llu,\"medium_finish_ns\":%llu,"
           "\"low_priority\":%d,\"medium_priority\":%d,\"high_priority\":%d},",
           o->protocol,(unsigned long long)(s.high_finish-s.high_start),
           (unsigned long long)s.low_finish,(unsigned long long)s.medium_finish,
           o->priority,o->priority+1,o->priority+2);
    pthread_mutex_destroy(&s.mutex);
    puts("\"samples\":1,\"period_ns\":0}");
    return 0;
}
static int worker(const struct options *o) {
    std::array<buffers,2> b;
    int rc = allocate(&b[0],o); if (rc) return rc;
    if (!strcmp(o->mode,"lock")) { rc = allocate(&b[1],o); if (rc) return rc; }
    if (o->cpu >= 0) {
        cpu_set_t cpus; CPU_ZERO(&cpus); CPU_SET(o->cpu,&cpus);
        if (sched_setaffinity(0,sizeof(cpus),&cpus))
            return failure("set affinity",errno,optional_errno(errno));
    }
    if (o->allow_rt) {
        struct rlimit limit = {1000000,2000000};
        struct rlimit old;
        if (getrlimit(RLIMIT_RTTIME,&old)) return failure("get RLIMIT_RTTIME",errno,0);
        if (old.rlim_max < limit.rlim_max) limit.rlim_max = old.rlim_max;
        if (limit.rlim_cur > limit.rlim_max) limit.rlim_cur = limit.rlim_max;
        if (setrlimit(RLIMIT_RTTIME,&limit)) return failure("set RLIMIT_RTTIME",errno,0);
    }
    /* Warm-up the processing code, not the demand-fault probe. */
    for (int n = 0; n < o->warmup; n++) {
        if (!strcmp(o->mode,"audio")) audio_work(&b[0],n,o->work,0);
        else video_work(&b[0],n,o->work);
    }
    if (set_policy(o)) return failure("set scheduler/nice",errno,optional_errno(errno));
    if (thread_scheduler::policy() != policy_id(o->policy))
        return status("error","effective scheduler does not match request",1);
    if (!strcmp(o->mode,"lock")) return lock_demo(o,b.data());
    uint64_t now = clock_ns(CLOCK_MONOTONIC);
    uint64_t origin = o->start_ns ? o->start_ns : now+20000000ULL;
    if (origin < now || origin-now > 2000000000ULL)
        return status("error","start-ns must be in the next two seconds (warmup may have overrun it)",1);
    if (sleep_until(origin)) return status("error","initial sleep interrupted",1);
    struct rusage before, after;
    getrusage(RUSAGE_SELF,&before);
    int count = 0;
    uint64_t end = origin+(uint64_t)o->duration_ms*1000000ULL;
    int periodic = strcmp(o->mode,"offline") != 0;
    while (count < (o->duration_ms ? MAX_SAMPLES : o->jobs)) {
        struct sample *s = &b[0].samples[count];
        s->release = periodic ? origin+(uint64_t)count*o->period_us*1000 : origin;
        if (periodic && sleep_until(s->release)) return status("error","periodic sleep interrupted",1);
        s->start = clock_ns(CLOCK_MONOTONIC);
        if (o->duration_ms && s->start >= end) break;
        uint64_t cpu_start = clock_ns(CLOCK_THREAD_CPUTIME_ID);
        probe_pages(&b[0],count);
        if (!strcmp(o->mode,"audio")) audio_work(&b[0],count,o->work,1);
        else video_work(&b[0],count,o->work);
        s->cpu = clock_ns(CLOCK_THREAD_CPUTIME_ID)-cpu_start;
        s->finish = clock_ns(CLOCK_MONOTONIC);
        count++;
        if (interrupted) return status("error","worker interrupted",1);
        if (o->duration_ms && s->finish >= end) break;
    }
    getrusage(RUSAGE_SELF,&after);
    if (o->duration_ms && count == MAX_SAMPLES && b[0].samples[count-1].finish < end)
        return status("error","sample capacity exceeded; increase work or shorten duration",1);
    /* Return to ordinary scheduling before writing artifacts. */
    struct effective effective;
    if (read_effective(&effective)) return failure("read effective scheduler",errno,0);
    if ((rc = demote(o))) return rc;
    auto file = open_artifact(o->output,"csv");
    if (!file) { fprintf(stderr,"open CSV: %s\n",strerror(errno)); return 1; }
    FILE *f = file.get();
    fprintf(f,"index,release_ns,start_ns,finish_ns,cpu_ns\n");
    for (int n = 0; n < count; n++) {
        const struct sample *s = &b[0].samples[n];
        fprintf(f,"%d,%llu,%llu,%llu,%llu\n",n,(unsigned long long)s->release,
                (unsigned long long)s->start,(unsigned long long)s->finish,
                (unsigned long long)s->cpu);
    }
    if (fclose(file.release()) || save_media(o,&b[0],count)) {
        fprintf(stderr,"write artifacts: %s\n",strerror(errno)); return 1;
    }
    printf("{\"schema_version\":1,\"status\":\"ok\",\"mode\":\"%s\",",o->mode);
    emit_effective(o,&effective);
    uint64_t elapsed = count ? b[0].samples[count-1].finish-origin : (uint64_t)o->duration_ms*1000000;
    printf("\"samples\":%d,\"period_ns\":%llu,\"elapsed_ns\":%llu,\"window_ns\":%llu,\"checksum\":%llu,"
           "\"minor_faults\":%ld,\"major_faults\":%ld,\"voluntary_cs\":%ld,"
           "\"involuntary_cs\":%ld,\"memory\":\"%s\",\"work\":%d,\"warmup\":%d,"
           "\"deadline\":{\"runtime_us\":%d,\"deadline_us\":%d,\"period_us\":%d}}\n",
           count,(unsigned long long)(periodic ? (uint64_t)o->period_us*1000 : 0),
           (unsigned long long)elapsed,(unsigned long long)((uint64_t)o->duration_ms*1000000),
           (unsigned long long)b[0].checksum,after.ru_minflt-before.ru_minflt,
           after.ru_majflt-before.ru_majflt,after.ru_nvcsw-before.ru_nvcsw,
           after.ru_nivcsw-before.ru_nivcsw,o->memory,o->work,o->warmup,
           o->runtime_us,o->deadline_us,o->period_us);
    return 0;
}
int main(int argc, char **argv) {
    struct options o = parse(argc,argv);
    int p = policy_id(o.policy);
    int rt = p == SCHED_FIFO || p == SCHED_RR || p == SCHED_DEADLINE;
    cpu_set_t allowed; CPU_ZERO(&allowed);
    if (sched_getaffinity(0,sizeof(allowed),&allowed))
        return failure("read allowed CPUs (CPU_SETSIZE bound)",errno,0);
    if (o.cpu >= 0 && !CPU_ISSET(o.cpu,&allowed)) {
        fprintf(stderr,"requested CPU is not in the effective allowed mask\n"); return 2;
    }
    int initial_policy = thread_scheduler::policy();
    if (initial_policy < 0)
        return failure("inspect guardian scheduler (kernel or container syscall restriction)",errno,optional_errno(errno));
    errno = 0;
    int initial_nice = getpriority(PRIO_PROCESS,0);
    if (errno) return failure("inspect guardian nice",errno,optional_errno(errno));
    if (initial_policy != SCHED_OTHER || initial_nice > 0)
        return status("skipped","guardian requires SCHED_OTHER and nice <= 0; do not launch via chrt/nice",77);
    if (rt && !o.allow_rt) return status("skipped","RT requires explicit --allow-rt",77);
    if (rt && CPU_COUNT(&allowed) < 2)
        return status("skipped","RT requires at least two allowed CPUs for housekeeping",77);
    if (rt && p != SCHED_DEADLINE && o.cpu < 0)
        return status("skipped","FIFO/RR require an explicit worker CPU, leaving another for watchdog",77);
    struct sigaction sa;
    memset(&sa,0,sizeof(sa)); sa.sa_handler = handle_signal;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM,&sa,NULL); sigaction(SIGINT,&sa,NULL);
    /* DEADLINE inherits the original domain-wide mask, never the watchdog's pin. */
    pid_t child = fork();
    if (child < 0) return failure("fork guardian",errno,0);
    if (child == 0) { int rc = worker(&o); fflush(stdout); _exit(rc); }
    if (rt) {
        cpu_set_t guard; CPU_ZERO(&guard);
        for (int i = 0; i < CPU_SETSIZE; i++)
            if (CPU_ISSET(i,&allowed) && i != o.cpu) { CPU_SET(i,&guard); break; }
        if (sched_setaffinity(0,sizeof(guard),&guard)) {
            int err = errno; kill(child,SIGKILL); waitpid(child,NULL,0);
            return failure("pin watchdog",err,0);
        }
    }
    uint64_t stop = clock_ns(CLOCK_MONOTONIC)+(uint64_t)o.timeout_ms*1000000;
    int wstatus = 0;
    for (;;) {
        pid_t rc = waitpid(child,&wstatus,WNOHANG);
        if (rc == child) break;
        if (rc < 0 && errno != EINTR) return failure("waitpid",errno,0);
        if (interrupted || clock_ns(CLOCK_MONOTONIC) >= stop) {
            kill(child,SIGKILL);
            while (waitpid(child,NULL,0) < 0 && errno == EINTR) {}
            return status("error",interrupted ? "interrupted; worker killed" :
                          "wall-clock watchdog expired; worker killed",1);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    if (WIFEXITED(wstatus)) return WEXITSTATUS(wstatus);
    return status("error","worker terminated by signal (possibly RLIMIT_RTTIME)",1);
}
