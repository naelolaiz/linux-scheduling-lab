#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <sched.h>
#include <span>
#include <stop_token>
#include <string_view>
#include <system_error>
#include <thread>
#include <type_traits>
#include <vector>
#include <sys/syscall.h>
#include <unistd.h>
#include "media.hpp"

namespace {
constexpr std::size_t frames = 480;
struct Block {
    std::size_t sequence = 0;
    std::array<std::int16_t,frames> audio{};
};

// Exactly one producer and one consumer. Slots are published/released, never shared concurrently.
template<typename T, std::size_t Capacity>
class SpscQueue {
    static_assert(Capacity > 0);
    static_assert(std::atomic<std::size_t>::is_always_lock_free);
    static_assert(std::is_nothrow_copy_assignable_v<T>);
    std::array<T,Capacity+1> slots_{};
    std::atomic<std::size_t> head_{0}, tail_{0};
public:
    bool try_push(const T &value) noexcept {
        const auto head = head_.load(std::memory_order_relaxed);
        const auto next = (head+1) % slots_.size();
        if (next == tail_.load(std::memory_order_acquire)) return false;
        slots_[head] = value;
        head_.store(next,std::memory_order_release);
        return true;
    }
    bool try_pop(T &value) noexcept {
        const auto tail = tail_.load(std::memory_order_relaxed);
        if (tail == head_.load(std::memory_order_acquire)) return false;
        value = slots_[tail];
        tail_.store((tail+1) % slots_.size(),std::memory_order_release);
        return true;
    }
};

bool parse_number(std::string_view text, int &value, int lower, int upper) {
    const auto [end,error] = std::from_chars(text.data(),text.data()+text.size(),value);
    return error == std::errc{} && end == text.data()+text.size() && value >= lower && value <= upper;
}
}

int main(int argc, char **argv) {
    const char *output = nullptr;
    int buffers = 30, consumer_delay_ms = 0;
    for (int i = 1; i < argc; i++) {
        const std::string_view name(argv[i]);
        if (name == "--help") {
            std::puts("thread-pipeline --output FILE.wav [--buffers 1..100] [--consumer-delay-ms 0..50]\n"
                      "Ordinary-priority C++20 SPSC example; NOT an RT/device measurement.");
            return 0;
        }
        if (i+1 == argc) { std::fputs("missing option value\n",stderr); return 2; }
        const char *value = argv[++i];
        if (name == "--output") output = value;
        else if (name == "--buffers" && parse_number(value,buffers,1,100)) {}
        else if (name == "--consumer-delay-ms" && parse_number(value,consumer_delay_ms,0,50)) {}
        else { std::fputs("invalid option or bounded value\n",stderr); return 2; }
    }
    if (!output) { std::fputs("--output is required\n",stderr); return 2; }
    const int policy = static_cast<int>(syscall(SYS_sched_getscheduler,0));
    if (policy < 0) { std::perror("inspect scheduler"); return 1; }
    if (policy != SCHED_OTHER) {
        std::fputs("this example must start with SCHED_OTHER; use sched-work for guarded RT experiments\n",stderr);
        return 2;
    }
    try {
        SpscQueue<Block,8> queue;
        std::vector<std::int16_t> recording(static_cast<std::size_t>(buffers)*frames,0);
        std::atomic<bool> producer_done{false};
        std::size_t consumed = 0, dropped = 0;

        std::jthread consumer([&](std::stop_token stop) {
            Block block;
            while (!stop.stop_requested()) {
                // Observe completion before checking the queue, so the final publication cannot be lost.
                const bool done = producer_done.load(std::memory_order_acquire);
                if (queue.try_pop(block)) {
                    std::ranges::copy(block.audio,recording.begin()+block.sequence*frames);
                    consumed++;
                    if (consumer_delay_ms)
                        std::this_thread::sleep_for(std::chrono::milliseconds(consumer_delay_ms));
                } else if (done) {
                    break;
                } else {
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
                }
            }
        });
        std::jthread producer([&](std::stop_token stop) {
            Block block;
            double filter = 0;
            auto release = std::chrono::steady_clock::now();
            for (int job = 0; job < buffers && !stop.stop_requested(); job++) {
                std::this_thread::sleep_until(release);
                block.sequence = static_cast<std::size_t>(job);
                for (std::size_t i = 0; i < frames; i++) {
                    const double sample = std::sin(6.283185307179586*440.0*
                        (static_cast<double>(job)*frames+i)/48000.0);
                    filter += .2*(sample-filter);
                    block.audio[i] = static_cast<std::int16_t>(filter*16000);
                }
                if (!queue.try_push(block)) dropped++;  // Drop newest; never wait on the slow consumer.
                release += std::chrono::milliseconds(10);
            }
            producer_done.store(true,std::memory_order_release);
        });
        producer.join();
        consumer.join();  // Explicit drain before scope-exit requests cancellation.
        if (!lab::write_wav(output,recording)) { std::perror("write WAV"); return 1; }
        std::printf("{\"status\":\"ok\",\"policy\":\"other\",\"buffers\":%d,"
                    "\"consumed\":%zu,\"dropped\":%zu,\"queue_capacity\":8,"
                    "\"consumer_delay_ms\":%d,\"device_xruns_measured\":false}\n",
                    buffers,consumed,dropped,consumer_delay_ms);
    } catch (const std::system_error &error) {
        std::fprintf(stderr,"thread setup/join failed: %s\n",error.what()); return 1;
    } catch (const std::bad_alloc &error) {
        std::fprintf(stderr,"buffer preallocation failed: %s\n",error.what()); return 1;
    }
}
