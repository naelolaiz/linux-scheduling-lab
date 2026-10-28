# Threading and code practices for time-sensitive processing

Use the [scheduling tutorial](TUTORIAL.md) to measure outcomes, not to justify
unsafe code with a higher priority. This chapter explains what to use, what to
avoid, and where the runnable examples make those choices.

## 1. Choose a threading API for the requirement

| Requirement | Prefer | Why / caveat |
|---|---|---|
| Owned background worker with cooperative cancellation | C++20 [`std::jthread`](https://en.cppreference.com/w/cpp/thread/jthread.html) and [`std::stop_token`](https://en.cppreference.com/w/cpp/thread/stop_token.html) | Scope-bound lifetime; no forgotten join. Stop is cooperative, not forced interruption |
| Ordinary mutex/condition coordination outside a callback | [`std::mutex`](https://en.cppreference.com/w/cpp/thread/mutex.html), [`std::unique_lock`](https://en.cppreference.com/w/cpp/thread/unique_lock.html), [`std::condition_variable_any`](https://en.cppreference.com/w/cpp/thread/condition_variable_any.html) | Explicit ownership and predicate-based waits; not a bounded RT blocking guarantee |
| Setup barrier, handoff or counted permits | [`std::latch`](https://en.cppreference.com/w/cpp/thread/latch.html), [`std::barrier`](https://en.cppreference.com/w/cpp/thread/barrier.html), [`std::counting_semaphore`](https://en.cppreference.com/w/cpp/thread/counting_semaphore.html) as appropriate | Clear coordination, but waits can block; keep them out of deadline paths unless justified |
| Linux thread policy/affinity/limits | [Target-thread Linux APIs](https://man7.org/linux/man-pages/man7/sched.7.html) during setup | ISO C++ has no RT policy/priority/affinity abstraction |
| FIFO threads that must never execute user work under the wrong initial policy | [`pthread_create`](https://man7.org/linux/man-pages/man3/pthread_create.3.html) with [explicit scheduling attributes](https://man7.org/linux/man-pages/man3/pthread_attr_setinheritsched.3.html) | Avoids the create-then-retune race; check every attribute/create error |
| A shared mutex that needs priority inheritance | A pthread mutex configured with [`PTHREAD_PRIO_INHERIT`](https://man7.org/linux/man-pages/man3/pthread_mutexattr_setprotocol.3p.html) | `std::mutex` cannot portably request PI; PI does not eliminate deadlock or long waits |
| Small fixed one-producer/one-consumer transfer | [Preallocated bounded SPSC queue](../native/pipeline.cpp) with a documented [memory-order protocol](https://en.cppreference.com/w/cpp/atomic/memory_order.html) | Nonblocking producer; **not** a general MPMC container |
| Independent offline CPU work | A bounded worker pool / fixed owned workers; see [Core Guidelines CP.41](https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines#cp41-minimize-thread-creation-and-destruction) | Avoid creating one thread per job or per input frame |
| Audio callback created by a server/driver | That API's existing callback thread and [callback rules](https://www.portaudio.com/docs/v19-doxydocs/writing_a_callback.html) | Do not add a sleeping user thread as a substitute for the device clock |

[`std::async`](https://en.cppreference.com/w/cpp/thread/async.html) can choose deferred execution, allocate and synchronize unexpectedly;
it is not a callback-safe scheduling policy. [`std::execution::par`](https://en.cppreference.com/w/cpp/algorithm/execution_policy_tag_t.html) does not give
you per-worker Linux priorities or a known bounded worker count. Do not equate
"modern C++" with "RT-safe by default".

The native RT core uses POSIX attributes for its PI demonstration because they
express the required initialization contract. Elsewhere, prefer C++ ownership
and data structures rather than manually managing raw thread lifetimes.

**Version note:** the examples use C++20 interfaces. The reference pages also
describe later-standard additions; those additions are not requirements or
promises about this lab's compiler/library combinations.

## 2. Runnable C++20 pipeline

```sh
make
mkdir -p results/threading
./build/thread-pipeline --output results/threading/normal.wav
./build/thread-pipeline --consumer-delay-ms 50 --output results/threading/slow.wav
```

Read [`native/pipeline.cpp`](../native/pipeline.cpp) alongside the output. Both
threads remain **SCHED_OTHER**; the program refuses an inherited RT policy.
It is a concurrency/ownership example, **not an audio device or RT benchmark**.
Use `sched-work` for the guarded scheduler experiments.

A producer creates 480-sample 48 kHz audio blocks every 10 ms and filters a sine
signal. An owned consumer transfers accepted blocks into a preallocated recording.
The eight-block queue has an explicit overflow policy: **drop the newest block
and count it**, never wait for the slow consumer. Dropped sequence positions stay
silent in the output WAV, so the artifact preserves the intended time positions.
File I/O occurs in main only after both threads have joined.

`--buffers` accepts 1..100, default 30. `--consumer-delay-ms` accepts 0..50 and
intentionally models a slow background stage. JSON reports `consumed`, `dropped`
and capacity; consumed plus dropped must equal requested buffers on success.
The slow case should usually drop blocks, but the count depends on actual thread
scheduling. It is not a pass/fail performance threshold, and the count is **not
hardware XRUNs**. Output paths are explicit and overwritten; remove only those
files when finished.

Important details in the code:

- Queue slots and the entire recording are allocated before either thread starts.
  Blocks are values, not pointers into a producer's reused stack buffer.
- Only the producer writes the head index, only the consumer writes the tail.
  Release publication and acquire observation order access to the non-atomic slot.
  The producer checks that a slot is free before overwriting it.
- The example asserts lock-free index atomics at compile time. This is a target
  capability requirement, not a promise that every `std::atomic<T>` is lock-free.
- The completion flag is observed **before** the empty-queue check. Reading it
  after a failed pop could lose the final publication in a race.
- Each thread owns its counters; main reads them only after `join`. No atomic
  increment is needed merely because a value eventually crosses a thread boundary.
- `jthread` provides cleanup if construction of another thread fails. On normal
  completion, explicit joins drain accepted work; simply leaving scope would
  request cancellation and could abandon queued blocks.
- Cancellation is checked between bounded iterations. `sleep_until` is not
  stop-token-aware; shutdown can wait for that short sleep (or a consumer delay).
  Production shutdown contracts must account for such waits.

The example shares its WAV serializer with the timing core; it is not
used to claim language-versus-language scheduling differences. There is no C
version because a C wrapper would not demonstrate a different Linux policy.

**Sources and further reading:** [acquire/release publication](https://en.cppreference.com/w/cpp/atomic/memory_order.html),
[target-dependent atomic lock freedom](https://en.cppreference.com/w/cpp/atomic/atomic/is_always_lock_free.html),
and [`jthread::join`](https://en.cppreference.com/w/cpp/thread/jthread/join.html)
explain the relevant contracts. The [queue and completion protocol in this example](../native/pipeline.cpp)
must still be reviewed as a whole; knowing that an index is atomic does not prove
the algorithm or object lifetimes correct.

## 3. Design the processing path before selecting priority

**Separate responsibilities.** A device callback should consume ready input,
perform bounded processing, publish output, and return. Decoding, disk access,
network I/O, logging, UI, graph rebuilding and unbounded control work belong to
other threads. Size and time those stages; pushing work into a helper merely
creates a dependency, it does not make the work disappear.

**Bound work and memory.** Use fixed buffers or capacity-reserved storage and
check worst input sizes. `std::vector` is fine when allocated and sized in setup;
`push_back`, `resize`, `std::string` construction and hash-table insertion can
allocate in the callback. Prefer `std::span` for nonowning views with an explicit
owner/lifetime; it neither owns memory nor automatically prevents an out-of-range
access. Do not return a span/pointer into a temporary buffer.

**Warm and prefault deliberately.** Touch relevant writable pages, initialize DSP
state and lazy paths, and consider stack usage. Prefaulting is not pinning;
`mlock` requires an adequate limit and a bounded memory budget. The main lab locks
only its probe mapping, not everything a production application could need.

**Never make failure invisible.** Ring overflow, starvation, permission denial,
late callbacks, buffer underruns and dropped video frames are different events.
Count them outside the hot path or in preallocated records. Do not log every
sample from the callback. Choose an explicit response: drop newest/oldest,
overwrite a latest-value slot, emit silence, or fail the stream. Each has
different correctness and continuity consequences.

**Do not solve backpressure by increasing a queue forever.** A larger queue trades
memory and latency for burst tolerance; it cannot sustain a producer permanently
faster than the consumer. Derive a capacity from an acceptable backlog and
measure high-water marks and overflow. The demo's eight slots are a teaching
choice, not a production recommendation.

**Avoid unbounded waiting.** Blocking mutexes, condition waits, semaphores, system
calls, I/O, futexes and allocator locks all need analysis. `try_lock` is not an
automatic RT solution: the fallback still needs a valid result, and unlock can
involve OS synchronization. A busy-spin can starve the thread that would satisfy
the dependency. A fair or ordinary helper may be a better design than more RT
threads fighting over shared state.

**Use PI where a mutex is genuinely necessary.** Keep critical sections short,
define lock ordering and avoid holding a lock across I/O or callbacks.
Inheritance can boost a lock owner but cannot guarantee the duration of its work.
Do not assume PI on a default `std::mutex`.

**Callback/code sources:** [PortAudio's callback restrictions](https://www.portaudio.com/docs/v19-doxydocs/writing_a_callback.html)
and [JACK's process-callback contract](https://jackaudio.org/api/group__ClientCallbacks.html)
explain the blocking/allocation concern. [`vector::reserve`](https://en.cppreference.com/w/cpp/container/vector/reserve.html)
and [`span`](https://en.cppreference.com/w/cpp/container/span.html) document capacity
and nonownership; [`mlock`](https://man7.org/linux/man-pages/man2/mlock.2.html)
and [PI mutex protocols](https://man7.org/linux/man-pages/man3/pthread_mutexattr_setprotocol.3p.html)
cover the separate OS mechanisms. A queue's capacity and overflow policy remain
application design choices, not defaults supplied by those APIs.

## 4. Correct cancellation, errors and lifetime

Threads are resources. Define who starts, stops and joins each thread, and which
objects must outlive it. Avoid `detach()` for application workers: shutdown can
otherwise leave callbacks referencing destroyed state. If using a pool, stop
accepting work, signal cancellation, choose drain versus discard, join workers,
then release shared buffers/devices.

A `std::stop_token` is a request, not a kill mechanism, deadline or cancellation
exception. Not every blocking call observes it. A condition-variable wait needs a
predicate to tolerate spurious wakeups; a stop-aware overload can help when
available. Bound noninterruptible work and use the owning API's shutdown protocol
for device/network calls.

Exceptions do not propagate automatically from a worker to its owner. An
uncaught exception in a thread terminates the process. Design an explicit error
channel for operations that can fail: an appropriate result/promise outside the
callback, or a preallocated status/error record for a time-sensitive stage.
Surface the failure after joining. Do not allocate rich error strings in a
callback or catch everything and report success.

The pipeline's processing operations are preallocated numerical work; potentially
failing file output is after joining. Main reports thread construction/join and
allocation failures. The RT engine independently checks system-call errors and
uses structured skipped/error results. Neither silently relabels OTHER as FIFO.

**Lifetime/error sources:** [`jthread` destruction and stop requests](https://en.cppreference.com/w/cpp/thread/jthread.html),
[stop-aware condition-variable waits](https://en.cppreference.com/w/cpp/thread/condition_variable_any/wait.html),
[thread-function exception behavior](https://en.cppreference.com/w/cpp/thread/thread/thread.html),
and [`promise::set_exception`](https://en.cppreference.com/w/cpp/thread/promise/set_exception.html)
for an off-callback error channel. [Core Guidelines CP.26](https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines#cp26-dont-detach-a-thread)
explains the lifetime risks of detaching.

## 5. Thread count, placement and dependencies

Start with the minimum pipeline that expresses actual parallelism. Budget
headroom for housekeeping, interrupts, the audio server and the rest of the
application. Hardware concurrency is a hint, not an instruction to launch that
many workers: containers may expose many CPUs but have a small quota.

Policy and affinity are per-thread. Apply settings from the thread itself or
before user work begins, and read them back. Using `native_handle()` after
`std::jthread` creation requires a setup handshake if it matters that no work runs
before the settings take effect. For strict initial policy requirements the
pthread attributes used by the lock lab are clearer.

Preserve the guardian's ordinary scheduling. Never wrap the whole harness in
`chrt -f` and assume only the callback is affected. Avoid priority 99 by default.
IRQ handlers, decoder workers, lock owners and memory/I/O paths can be on the
critical dependency chain; starving them may worsen the highest-priority task.

Use the [hardware guide](KERNELS_AND_HARDWARE.md) to distinguish SMT siblings,
cores, capacity classes and NUMA placement. Avoid early cache-line padding,
affinity or "lock-free" rewrites without measuring the actual contention.
Lock-free means a progress property, not wait-free per-operation latency or a
hard-RT guarantee; it also says nothing about the latency of the surrounding
memory subsystem.

**Placement/dependency sources:** [`pthread_create`](https://man7.org/linux/man-pages/man3/pthread_create.3.html)
documents inherited thread attributes; [`pthread_setaffinity_np`](https://man7.org/linux/man-pages/man3/pthread_setaffinity_np.3.html)
and [`pthread_getschedparam`](https://man7.org/linux/man-pages/man3/pthread_getschedparam.3.html)
cover setting/reading the actual thread state.
[PREEMPT_RT execution-context differences](https://docs.kernel.org/core-api/real-time/differences.html)
explain why IRQ/helper context matters independently of the callback policy.

## 6. Choosing priorities: a policy, not a magic number

Start with OTHER/nice 0 and measure. Keep priority changes local to the thread
that needs them, with an explicit authorization and rollback plan.

| Thread role | Sensible starting point | What would justify a change |
|---|---|---|
| UI/control, management, watchdog | OTHER/nice 0 | Fix blocking work and grouping before considering priority boosts; keep the watchdog outside RT |
| Optional housekeeping/rendering | OTHER/nice +5 or +10, or BATCH | Reduce foreground interference; inspect completion-time cost under contention |
| Disposable background work | IDLE | Only if starvation and unbounded completion delay are acceptable |
| Offline encoding / parallel analysis | BATCH or OTHER with a bounded worker count; delegated weight/quota if needed | Aggregate resource policy, not a high RT priority |
| Bounded device callback / periodic DSP | First fix its code/dependencies; modest FIFO or RR if the audio stack and permissions support it | Repeated deadline evidence plus a dependency/budget analysis; 10 is this lab's example, not a universal recommendation |
| Equal-priority continuously runnable RT peers | RR when time-slice rotation is intended | FIFO can let a peer monopolize the CPU until it blocks; quantum may exceed a short job |
| Periodic computation needing an explicit reservation | DEADLINE after admission and conservative runtime analysis | A runtime/deadline/period budget, not "RT priority 100" |
| IRQ/helper/lock-owner on the critical path | Inspect actual policy, priority and dependency ordering | The callback must not starve the work needed to feed/unblock it; use PI for appropriate mutex dependencies |

Nice and RT priority are **different scales**: nice -20 is more favored than
nice +19 *within applicable fair scheduling*, while FIFO/RR priority 99 is
higher than 1. Nice is not how you rank FIFO/RR tasks. OTHER/BATCH/IDLE use a
zero static RT priority. A DEADLINE task is not expressed as a FIFO number.
Do not pass `sched_priority=10` with OTHER and assume it is a portable boost.

Use the **lowest RT priority that satisfies the analyzed ordering** and leave
headroom. Never start with 99. A high-priority thread should normally execute
bounded work then block/return; FIFO plus an unbounded loop can starve the system.
RR rotates among equal priorities, not against lower-priority threads. Existing
device/server priorities differ across systems: do not copy a fixed "IRQ=50,
audio=80" recipe without inspecting the real dependency chain.

The lock demo's 10/11/12 priorities intentionally create an inversion; they are
an experimental arrangement, not a suggested production hierarchy. Similarly,
the independent guardian plus reserved logical housekeeping CPU is a safety
mechanism, not a claim that the lab has configured isolation.

An ordinary-priority stage can still be preferable to another RT stage. Reduce
work, increase a permissible device buffer, remove unnecessary synchronization,
or improve queue/placement design before raising priority. If a slow I/O stage
is the bottleneck, CPU priority may achieve nothing. If an ancestor quota
throttles ordinary helpers, changing the callback's priority can hide rather than
solve the resource mismatch.

Read effective settings back (`pthread_getschedparam`, Linux scheduler APIs,
`getpriority`, affinity) and retain them with the measurements. Return permission
errors as errors/skips; never silently relabel an ordinary thread as RT.

**Priority sources:** [`sched(7)`](https://man7.org/linux/man-pages/man7/sched.7.html)
defines policy ordering, static priorities and permissions;
[`getpriority(2)`](https://man7.org/linux/man-pages/man2/getpriority.2.html) defines
the different nice scale; [DEADLINE bandwidth rules](https://docs.kernel.org/scheduler/sched-deadline.html)
define reservation parameters; [PI protocols](https://man7.org/linux/man-pages/man3/pthread_mutexattr_setprotocol.3p.html)
address lock-owner dependencies. The role-based starting points above are
recommendations for measurement, not numeric priorities mandated by these sources.

## 7. How many threads? Budget capacity, not just logical CPUs

There is no universally correct "one thread per core" rule. Distinguish:
threads that exist, runnable CPU-consuming threads, intermittently active
callbacks, and threads blocked on I/O. Only some of them contend for CPU at
the same instant, but all still have memory and lifecycle costs.

Use this decision sequence:

1. **Inventory effective resources.** Read allowed CPUs and every ancestor's
   `cpu.max`, then inspect topology. Eight allowed CPUs with an aggregate
   two-CPU quota are not eight CPUs of sustained fair-class capacity.
   Weight-based competition and other workloads can reduce actual service
   further. A fractional quota can still require one worker, but does not
   become one reserved CPU by rounding it up.
2. **Reserve a responsiveness margin.** Account for the callback, helpers, audio
   server, IRQ/housekeeping work and unrelated users. "Reserve" here is a design
   budget unless the system actually configures isolation/reservations.
3. **Start small.** One processing thread and one bounded background stage is a
   useful starting experiment. For CPU-bound offline work, try 1, 2 and a small
   allowed count before attempting to use every logical CPU.
4. **Increase only with evidence.** Look for additional completed work without
   unacceptable p99 response, misses, memory growth, cache misses, throttling or
   energy/thermal cost. Stop when marginal gains flatten or the latency objective
   fails, not when `hardware_concurrency()` says you have reached a magic number.
5. **Account for nested libraries.** A pool of four jobs, each starting four
   codec/OpenMP/BLAS workers, can mean sixteen runnable workers plus the caller.
   Choose one owner of parallelism; set subordinate library thread counts
   explicitly and record them. A library may also have hidden service threads.

For blocking I/O, more workers than CPU cores can sometimes improve overlap, but
the correct limit depends on queue depth, service time, filesystem/network limits
and memory. Prefer asynchronous I/O/event loops where appropriate rather than
unbounded threads. Never apply the I/O-overlap heuristic blindly to CPU-bound DSP.

SMT is not double execution capacity. Heterogeneous cores need not have equal
throughput or latency; NUMA workers can become memory-bandwidth-bound. Extra
threads may increase power enough to lower sustained frequency. A useful count
is therefore specific to workload, topology, quota and thermal operating point.

**Capacity sources:** [`hardware_concurrency` is only a hint](https://en.cppreference.com/w/cpp/thread/thread/hardware_concurrency.html),
[cgroup CPU bandwidth is hierarchical](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpu-interface-files),
and [scheduler capacity](https://docs.kernel.org/scheduler/sched-capacity.html)
describes asymmetric resources. For library-level thread controls, consult the
[OpenMP 5.2 environment-variable specification](https://www.openmp.org/spec-html/5.2/openmpch21.html);
do not assume every library uses OpenMP or the same environment variables.

### A thread-count experiment you can defend

For a **strong-scaling** comparison, keep total input and processing work fixed,
divide the same jobs between 1, 2 and N workers, and compare total completion time
and resource consumption. Starting N copies of an entire fixed-work command
multiplies the work; that is **not** a strong-scaling speedup test.

For **weak scaling**, increase work in proportion to workers and ask whether
time per worker/batch stays stable. For a **foreground-latency** experiment,
keep the foreground workload unchanged while varying the bounded number of
background workers. These are three different questions.

The existing `affinity` preset performs the third kind: it changes placement
and one versus two background **process workers**, each internally doing native
work. It is not an intra-process parallel speedup benchmark. The pipeline
example has two actual C++ threads and a deliberately fixed SPSC topology.
Neither pretends that changing a worker count alone proves a general scaling law.

When adapting an application, record a table with: total jobs/bytes, worker
count, thread policy/affinity, completion time, total CPU time, per-stage queue
high-water/overflow, context switches, foreground p99/misses, quota throttling
and temperature. Hold chunk size, output quality and library-internal parallelism
constant unless that is the variable being studied. Include a serial baseline.

**Measurement/scaling reference:** LLNL's [Introduction to Parallel Computing](https://hpc.llnl.gov/documentation/tutorials/introduction-parallel-computing-tutorial)
defines strong/weak scaling, observed speedup, granularity and parallel overhead.
The [lab metric implementation](../schedlab/metrics.py) and
[preset definitions](../schedlab/runner.py) establish which of those quantities
these particular commands actually measure.

## 8. Choose a parallelization strategy

| Strategy | Good fit | Costs / pitfalls |
|---|---|---|
| Serial bounded callback | Small audio blocks, short per-frame work | Often avoids more coordination than it gives up in parallel speed; optimize only after measuring |
| Pipeline stages | Input/decode, DSP, encode/write with meaningful overlap | Throughput can improve while end-to-end latency grows; every queue needs bounds and an overflow policy |
| Data parallelism | Independent channels, frames or tiles | Shared memory bandwidth and result assembly; tiles may require halos/borders, channels may share DSP state |
| Task graph | Independent branches with explicit dependencies | Dependency bookkeeping and the longest path dominate response; do not make a callback wait for arbitrary background work |
| Bounded job pool | Many offline independent jobs | Amortizes creation cost; queue and chunk policy determine contention and fairness |
| Parallelizing one tiny callback across workers | Only when measured compute savings exceed wakeup/join costs | Barrier waits follow the slowest worker; higher priority does not remove dispatch/communication jitter |

**Static partitioning** assigns each worker a known range. It is simple, reduces
dispatch synchronization and can preserve cache/NUMA locality. It works well for
similar-cost jobs on similar-capacity cores. Uneven inputs or heterogeneous cores
can leave one worker as the long tail.

**Dynamic chunk assignment** lets a worker fetch the next chunk when ready.
It balances irregular offline work but adds shared-counter/queue traffic, weakens
locality, and makes individual task start times less predictable. Fetch a useful
chunk rather than an atomic index for every pixel/sample. An atomic fetch-add
with relaxed ordering can assign **unique indices**, but that alone does not
publish task data or make a shared result object safe.

**Work stealing** can help irregular offline graphs and reduce idle workers.
It adds queue synchronization, migration and timing variability. A general
work-stealing runtime is not automatically suitable for a callback with a tight
deadline. Prefer mature, measured runtimes for this complexity rather than a
home-grown lock-free scheduler.

**Scheduling-strategy sources:** [LLNL's partitioning, load-balancing and granularity discussion](https://hpc.llnl.gov/documentation/tutorials/introduction-parallel-computing-tutorial)
supports the static/dynamic tradeoffs. The oneTBB guides describe
[chunk/partitioner choices](https://uxlfoundation.github.io/oneTBB/main/tbb_userguide/Partitioner_Summary.html)
and [task scheduling/work stealing](https://uxlfoundation.github.io/oneTBB/main/tbb_userguide/How_Task_Scheduler_Works.html).
They are further-reading examples of a mature offline runtime, not a dependency
of this project or an endorsement for hard-RT callbacks.

For audio, per-channel parallelism may be independent, but an IIR filter's state
depends on prior samples; do not split its time sequence into unordered chunks.
FFT/block processing has overlap/windowing and algorithmic-delay requirements.
For video, frame/tile parallelism may need reference frames or filter halos;
reordering work must preserve output correctness and quality. Test equivalent
outputs before comparing throughput.

Keep worker-local scratch buffers and ownership wherever possible. Separate
immutable input from disjoint output ranges. A reduction/aggregation phase
needs an explicit synchronization boundary; don't read partially written
results just because task indices were atomic.

**Data-dependency references:** SciPy's [IIR/FIR state equations and initial/final state](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.lfilter.html)
and [overlap-add convolution](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.oaconvolve.html)
provide concrete DSP background; neither Python nor SciPy runs in this lab's
measured callbacks. For pipeline/data/task decomposition more generally, see
[LLNL's parallel-program design examples](https://hpc.llnl.gov/documentation/tutorials/introduction-parallel-computing-tutorial).

## 9. Choose a synchronization strategy

| Communication pattern | Starting strategy | Important contract |
|---|---|---|
| One callback produces, one helper consumes | Bounded SPSC ring, as in the example | Exactly one producer/consumer; bounded value copy and explicit overflow |
| Several producers/consumers | Per-producer SPSC queues plus a bounded aggregator, or a suitable MPSC/MPMC design | Do not add a second consumer to the example's SPSC queue; its correctness would be lost |
| Offline jobs with shared queue | Ordinary mutex + predicate-based condition variable | Often simpler and adequate; do not hold the queue lock while doing the job |
| Latest scalar control parameter | Suitable lock-free atomic value | Verify target lock freedom and coherent update requirements; several independent atomics do not form one snapshot |
| Coherent multi-field configuration | Immutable versioned snapshot with a safe publication/reclamation protocol | Pointer publication is not lifetime management; reclaim old objects outside callbacks |
| Setup milestone | Latch/barrier/semaphore | One-shot versus reusable semantics; ensure error/cancellation paths cannot strand other participants |
| RT thread must share a lock | Short bounded critical section with an analyzed PI mutex | Priority inheritance helps the owner run; it does not bound I/O, nesting or deadlock |
| Waiting for completion | Join/future/condition protocol off the callback | Make drain/discard and cancellation decisions explicit |

Avoid "spin until the flag changes" as a default. A higher-priority spinner can
prevent a lower-priority producer from ever running on that CPU. Atomic
`wait`/`notify` or a condition variable can avoid burning CPU in ordinary workers,
but their wake-up path is not a hard-RT bound. Polling has a responsiveness/CPU
tradeoff; the pipeline's 1 ms consumer poll is an explicit teaching choice.

Double buffering is useful only with an ownership/acknowledgment protocol: an
atomic pointer swap does not prove that the reader has finished with the old
buffer. Likewise, `std::atomic<std::shared_ptr<T>>` need not be lock-free;
reference-count decrements and final destruction can allocate/free or execute
arbitrary destructor work on the callback. Defer reclamation to a safe context.

Define a lock order, avoid callbacks while holding locks, and eliminate cyclic
waits. Barrier-based parallel callbacks need special care: all participants must
be scheduled and complete, so one delayed participant determines the join tail.
Cancellation while one participant is missing can deadlock an otherwise correct
steady-state barrier. Test partial thread creation and shutdown, not only the
happy-path loop.

Finally, measurement is part of the strategy. False sharing, allocator contention,
queue churn, migrations and lock wait can erase a parallel speedup. Profile them
on the actual target, changing one design choice at a time. Do not prescribe
cache-line padding or memory-order weakening as universal optimizations.

**Synchronization sources:** [Core Guidelines CP.20/21/22/42](https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines#s-concurrency)
cover ownership, lock ordering, unknown callbacks and wait predicates;
[atomic wait](https://en.cppreference.com/w/cpp/atomic/atomic/wait.html),
[barriers](https://en.cppreference.com/w/cpp/thread/barrier.html) and
[semaphores](https://en.cppreference.com/w/cpp/thread/counting_semaphore.html)
describe distinct blocking protocols. The
[atomic shared-pointer contract](https://en.cppreference.com/w/cpp/memory/shared_ptr/atomic2.html)
explains lock freedom and destruction sequencing; it is not a universal callback-safe
reclamation mechanism. The [PI mutex specification](https://man7.org/linux/man-pages/man3/pthread_mutexattr_setprotocol.3p.html)
applies when that OS protocol is explicitly selected.

## 10. Review checklist

Before trying a higher scheduling priority, verify:

1. Inputs, queue capacities, allocations and work per iteration are bounded.
2. No hidden file/network/UI/logging/allocator activity occurs in the callback.
3. Ownership/lifetimes and shutdown/drain rules are explicit and exercised.
4. Shared state has a documented synchronization protocol; `volatile` is not it.
5. Overflow, cancellation, timeout and unavailable-policy paths remain observable.
6. Dependency/IRQ/helper priorities cannot starve the requested service.
7. Requested policy, effective settings, periods and resource limits are recorded.
8. Correctness tests run separately from realistic timing experiments.

Use ASan/UBSan and (where practical) ThreadSanitizer for correctness on an ordinary
build. They change execution/memory behavior, so **do not use instrumented timings
as RT evidence**. Sanitizers also do not prove a lock-free algorithm correct;
review its memory model, publication and ownership invariants.

**Tool references:** Clang's [AddressSanitizer](https://clang.llvm.org/docs/AddressSanitizer.html),
[UndefinedBehaviorSanitizer](https://clang.llvm.org/docs/UndefinedBehaviorSanitizer.html)
and [ThreadSanitizer](https://clang.llvm.org/docs/ThreadSanitizer.html) document
usage, overhead and supported platforms. For hardware-counter measurements,
[`perf stat`](https://man7.org/linux/man-pages/man1/perf-stat.1.html) is useful
further reading; PMU access and event availability are kernel/hardware dependent.
These are optional diagnostic techniques, not additional CI performance gates.

## Further reading

- [C++ Core Guidelines: concurrency](https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines#s-concurrency)
- [`std::jthread`](https://en.cppreference.com/w/cpp/thread/jthread.html)
- [`std::stop_token`](https://en.cppreference.com/w/cpp/thread/stop_token.html)
- [`std::span`](https://en.cppreference.com/w/cpp/container/span.html)
- [C++ memory order](https://en.cppreference.com/w/cpp/atomic/memory_order.html)
- [Atomic lock-free queries](https://en.cppreference.com/w/cpp/atomic/atomic/is_always_lock_free.html)
- [`std::thread::hardware_concurrency` is a hint](https://en.cppreference.com/w/cpp/thread/thread/hardware_concurrency.html)
- [Condition-variable predicates and stop-aware waits](https://en.cppreference.com/w/cpp/thread/condition_variable_any/wait.html)
- [C++ barriers and phase completion](https://en.cppreference.com/w/cpp/thread/barrier.html)
- [Atomic shared-pointer specialization](https://en.cppreference.com/w/cpp/memory/shared_ptr/atomic2.html)
- [OpenMP environment-variable controls](https://www.openmp.org/spec-html/5.2/openmpch21.html)
- [POSIX thread scheduling attributes](https://man7.org/linux/man-pages/man3/pthread_attr_setschedpolicy.3.html)
- [Explicit versus inherited scheduling](https://man7.org/linux/man-pages/man3/pthread_attr_setinheritsched.3.html)
- [Priority-inheritance protocol](https://man7.org/linux/man-pages/man3/pthread_mutexattr_setprotocol.3p.html)
- [PortAudio: writing a callback](https://www.portaudio.com/docs/v19-doxydocs/writing_a_callback.html)
- [JACK process callback constraints](https://jackaudio.org/api/group__ClientCallbacks.html)
