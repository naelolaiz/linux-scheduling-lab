# A measured guide to Linux scheduling

This lab separates three questions that are often confused:

1. **Which runnable thread gets a CPU?** Policy, nice level, RT priority and deadlines.
2. **How much resource may a workload use?** Cgroups, placement, memory and I/O controls.
3. **How quickly can the whole system respond?** Kernel preemption, IRQs, dependencies,
   timers, firmware, devices, power management and physical hardware.

Changing the answer to one does not automatically solve the others. Start on an
ordinary noncritical Linux machine. All beginner labs work without an init-system
integration, sound card, display, network, or privileged service.

For implementation choices, read [threading, code and priority practices](THREADING_AND_CODE.md):
thread ownership, C++20 versus pthread APIs, bounded queues, callback constraints,
shutdown, priority selection and a runnable producer/consumer example.

## 1. Five-minute safe quickstart

Requirements: GCC 11+ or Clang 14+ with a matching C++20 library including
`std::binary_semaphore`, CMake 3.21+ (its default generator uses GNU Make; Ninja
also works) and Python 3.10+. A glibc or musl Linux
installation is supported. Linux 5.4+ is the baseline; optional interfaces depend
on the actual kernel build and permissions. This is not a promise that every
custom kernel exposes every feature.

Install missing build dependencies using your distribution's normal admin process.
These are **manual setup examples**, not commands the lab executes:

| Distribution | Packages / manual command |
|---|---|
| [Gentoo / Portage](https://wiki.gentoo.org/wiki/Handbook:AMD64/Working/Portage), including OpenRC | `emerge --ask sys-devel/gcc dev-build/cmake dev-build/make dev-lang/python` |
| [Debian / Ubuntu apt](https://manpages.debian.org/bookworm/apt/apt.8.en.html) | `apt install g++ cmake make python3` |
| [Fedora / DNF](https://dnf.readthedocs.io/en/latest/command_ref.html) | `dnf install gcc-c++ cmake make python3` |
| [Alpine / apk](https://docs.alpinelinux.org/user-handbook/0.1a/Working/apk.html), musl | `apk add g++ cmake make python3` |

Optionally [install uv](https://docs.astral.sh/uv/getting-started/installation/)
to use the `uv run poe …` shortcuts; each one runs a command shown in this
tutorial, and nothing else about the lab changes.

Gentoo's selected compiler must support C++20; `g++ --version` and
`python3 --version` show what the current shell actually uses. No OpenRC service
needs to be started. Each [CMake preset](../CMakePresets.json) builds into its
own `build/<preset>` directory: `default` uses `CXX` or the system `c++`, `gcc`
and `clang` select a compiler, and the cross presets show how to name a
cross-toolchain compiler. Changing flags (for example
`cmake --preset default -DCMAKE_CXX_FLAGS=-fno-omit-frame-pointer`) rebuilds the
affected programs on the next build; no clean step is needed. To test or run the
lab against another build, set `SCHEDLAB_NATIVE_DIR=build/<preset>`; the `poe`
tasks derive it from `SCHEDLAB_PRESET=<preset>`.

The `asan` and `tsan` presets need the compiler's sanitizer runtime (GCC's
libsanitizer, built on Gentoo with `USE=sanitize`, or Clang's compiler-rt). A
sanitizer report exits with status 99, which no lab path uses. If ThreadSanitizer
stops with "unexpected memory mapping", the kernel's ASLR entropy is too high for
that runtime; CI lowers it with `sysctl vm.mmap_rnd_bits=28`, which the lab never
changes on your machine.

**Build/setup sources:** [GCC](https://gcc.gnu.org/projects/cxx-status.html#cxx20),
[Clang](https://clang.llvm.org/cxx_status.html#cxx20) and
[libstdc++ library support](https://gcc.gnu.org/onlinedocs/libstdc++/manual/status.html)
distinguish compiler features from library availability.
[CMake presets](https://cmake.org/cmake/help/latest/manual/cmake-presets.7.html),
[Gentoo's compiler setup](https://wiki.gentoo.org/wiki/GCC) and
[OpenRC](https://wiki.gentoo.org/wiki/OpenRC) explain the corresponding tools;
[uv projects](https://docs.astral.sh/uv/concepts/projects/) and
[Poe the Poet](https://poethepoet.natn.io/) document the optional task wrappers.
The stated minima are compatibility targets, not vendor support guarantees;
prefer maintained releases (see [Python version status](https://devguide.python.org/versions/)).

From the repository root:

```sh
cmake --preset default && cmake --build --preset default
python3 -m unittest discover -s tests -v
python3 -m schedlab run quick --repetitions 1 --out results/first
```

With uv, `uv run poe test` performs the first two steps.

When the run finishes it prints its findings and caveats. For the full summary
in the terminal, run `python3 -m schedlab analyze results/first` (or
`uv run poe analyze results/first`): one row per case with the median of its
primary metric (response p99 for periodic work, throughput for offline work,
high-priority wait for locks), the range across repetitions and a bar plot, then
the preset's findings and caveats. Give it several run directories to compare the
same cases across boots, compilers or machines; `--format markdown` and
`--format json` produce the same analysis for other tools.

Open `results/first/report.html` directly in a browser. Its interactive dashboard
shows the same findings, plus run/status/policy filters, selectable metric
rankings, latency-tail plots, sortable evidence tables, and dark/light themes. It
needs no web server or remote assets. The directory also contains:

| File | Meaning |
|---|---|
| `run.json` | Manifest, environment (including the native build), commands, results, metrics and structured skips |
| `*.csv` | Integer monotonic release/start/finish timestamps and thread CPU times |
| `*.stdout` | Original native JSON result, useful for debugging the parser |
| `audio-*.wav` | Actual generated, filtered mono PCM audio at 48 kHz |
| `video-*.pgm`, `offline-*.pgm` | Last generated/filtered 160x90 grayscale frame |
| `report.html` | Self-contained interactive visualization and human-readable analysis |

The audio file can be played with any WAV player; playback is not measured.
The image is PGM, not an encoded movie. These artifacts are not placeholders:
the measured native loops synthesize and filter samples or generate and blur pixels.

`results/first` must not already exist. Choose a new name for each run. Default
automatically named directories include a timestamp. No background service remains.
Cleanup means removing **only your chosen run directory**, using your usual file
manager. `cmake --build --preset default --target clean` (or `uv run poe clean`)
removes just the native build products, not results.

**Artifact sources:** the lab's [PCM WAV serializer](../native/media.hpp),
Python's [WAV inspection API](https://docs.python.org/3/library/wave.html), and
the [PGM format manual](https://manpages.debian.org/bookworm/netpbm/pgm.5.en.html)
describe the generated files. The [runner](../schedlab/runner.py) defines the
manifest and fresh-directory behavior; these are project interfaces, not
kernel-standard result formats.

## 2. Read the measurements before changing priorities

For each periodic job the native code records:

| Quantity | Definition / interpretation |
|---|---|
| Release | Planned absolute [`CLOCK_MONOTONIC`](https://man7.org/linux/man-pages/man2/clock_gettime.2.html) release, not the time it finally ran |
| Start | Actual start of processing after the absolute sleep |
| Finish | Completion of native work, not completion of later artifact writes |
| Wake-up lateness | Start minus release |
| Wall processing latency | Finish minus start; includes preemption and blocking |
| Thread CPU processing | [`CLOCK_THREAD_CPUTIME_ID`](https://man7.org/linux/man-pages/man2/clock_gettime.2.html) consumed in the processing section |
| Response time | Finish minus release; captures both wake delay and processing |
| Deadline miss | Finish strictly after release plus relative deadline |
| Throughput | Completed jobs per elapsed second; see fixed-window distinction below |
| Faults/context switches | [`getrusage`](https://man7.org/linux/man-pages/man2/getrusage.2.html) deltas across the measured run, not setup or file output; fields are `null` and `rusage_available` is false if collection fails |

Audio/video releases advance by a fixed period, even after a miss. Late jobs are
processed in sequence, not silently dropped or rescheduled from "now". Thus an
overloaded run can accumulate lateness. Under DEADLINE the application comparison
uses the requested relative deadline, not necessarily its period.

Offline fixed-work jobs share an initial release: their response times are
**cumulative completion times**, not independent periodic latency. Their wake
column is also cumulative; do not interpret it as timer jitter. In fixed-window
fairness experiments, only jobs finished within the common window count toward
throughput. One admitted job can finish after the window and remains in raw
latency samples. Zero admitted jobs is valid starvation evidence: latency
quantiles are absent, never fabricated as zero.

Each nonempty distribution reports sample count, p50, p95, p99 and maximum using
linear interpolation between sorted observations. The HTML summarizes p50/p99/max;
all quantiles are in JSON. Repetition tables use the median of per-run p99 values
and the largest observation, not a pooled distribution hiding run-to-run variation.

The code preallocates data/results, warms the processing path (two jobs by default),
then changes worker policy. Measured sections have no logging, allocation or file
I/O. Clock reads and page-probe work still cost time. Artifacts are written afterward.
The `memory` lab intentionally leaves a separate mapping untouched for its
demand-fault condition.

**Method:** hold work, placement and competitors constant; change one control;
repeat; retain raw samples; inspect tails and misses as well as throughput.
Groups use a common future release; repetitions alternate the order of conditions.
Separate process groups permit cleanup while preserving the launcher's session
and autogroup, so the harness does not accidentally isolate nice competitors into
different autogroups. Cgroups and external programs can still affect fairness.

No expected numeric ranking is a test assertion. Thermal state, other users,
virtualization, caches, compiler changes and kernel behavior are real confounders.
Thirty buffers is a demonstration, not a reliable estimate of a rare tail.

**Measurement sources:** [absolute `clock_nanosleep` releases](https://man7.org/linux/man-pages/man2/clock_nanosleep.2.html),
[autogroup/task-group effects](https://man7.org/linux/man-pages/man7/sched.7.html),
and [NIST's guidance on comparative experimental designs](https://www.itl.nist.gov/div898/handbook/pri/section3/pri33.htm).
For percentile conventions, compare the [inclusive interpolation description](https://docs.python.org/3/library/statistics.html#statistics.quantiles)
with this lab's [exact formulas and window/miss rules](../schedlab/metrics.py).
Those formulas describe observed samples; they do not establish population-tail
confidence intervals or a WCET bound.

## 3. Policy map: what the knobs actually do

| Policy/control | Strength | Cost / important limit |
|---|---|---|
| [`SCHED_OTHER`, nice -20..19](https://man7.org/linux/man-pages/man7/sched.7.html) | General-purpose fair scheduling; lower nice means larger relative weight | Not a CPU cap, reservation, or real-time guarantee; grouping affects comparisons |
| [`SCHED_BATCH`](https://man7.org/linux/man-pages/man7/sched.7.html) | Noninteractive CPU-intensive work | Same fair class, different wake-up treatment; not universally faster |
| [`SCHED_IDLE`](https://man7.org/linux/man-pages/man7/sched.7.html) | Best-effort background work that may make little progress | Can starve under contention; not the kernel's idle thread |
| [`SCHED_FIFO`, priority 1..99](https://man7.org/linux/man-pages/man7/sched.7.html) | Fixed-priority preemption; suitable for carefully bounded periodic tasks | Can starve lower priorities; equal-priority tasks do not rotate on a time quantum |
| [`SCHED_RR`, priority 1..99](https://man7.org/linux/man-pages/man7/sched.7.html) | FIFO-like priorities plus equal-priority time slicing | Does not make different priorities fair; slice size is kernel-configured |
| [`SCHED_DEADLINE`](https://docs.kernel.org/scheduler/sched-deadline.html) | Runtime/deadline/period reservation with admission control and CBS/EDF | Privilege, domain/affinity constraints, budget sizing and admission failures |
| [`cpu.weight`](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpu-interface-files) | Relative shares between contending eligible cgroups | Not a cap and not a FIFO/RR bandwidth control |
| [`cpu.max`](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpu-interface-files) | Aggregate fair-class CPU bandwidth cap | Periodic throttling can worsen tails; does not govern FIFO/RR |
| [Affinity](https://man7.org/linux/man-pages/man2/sched_setaffinity.2.html) / [cpuset](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpuset) | Restrict eligible CPUs (and cpusets can restrict memory nodes) | Affinity alone is not exclusive ownership or IRQ isolation |

Policies are **per thread**. A process with several threads may have mixed policies.
Linux [nice is also per-thread in practice](https://man7.org/linux/man-pages/man2/getpriority.2.html). A high-priority audio callback does not
raise the priority of a helper, device IRQ, filesystem, or lock owner automatically.

[CFS](https://docs.kernel.org/scheduler/sched-design-CFS.html) and
[EEVDF](https://docs.kernel.org/scheduler/sched-eevdf.html) are scheduler implementation designs, not additional user-facing RT
policies. EEVDF virtual deadlines select among eligible fair tasks; they are not
wall-clock guarantees. The transition began in mainline 6.6 and evolves; vendor
backports make version-number-only inference unreliable.
[sched_ext](https://docs.kernel.org/scheduler/sched-ext.html) can replace
fair scheduling when enabled and loaded; the environment snapshot records visible
state. See [kernel/hardware comparisons](KERNELS_AND_HARDWARE.md).

## 4. Lab: offline throughput versus relative shares

**Goal/hypothesis:** lowering priority should chiefly change access under
contention, not magically reduce the cost of the same pixels.

```sh
python3 -m schedlab run offline --out results/offline
python3 -m schedlab run fairness --out results/fairness
```

`offline` performs 80 identical frame jobs with eight blur passes, separately under
OTHER/BATCH/IDLE. `fairness` pins two generated-frame workers to one allowed CPU
for a shared 500 ms window. Each pair has an OTHER/nice-0 control and a candidate:
OTHER/0, OTHER/10, BATCH/0 or IDLE/0. Three repetitions are the default.

**Observe:** fixed-work completion time, completed-window jobs, CPU time, involuntary
switches, starvation and effective nice/policy. A quiet CPU may make policies look
similar. BATCH is not a promise of better throughput; IDLE progress may be tiny.
Nice differences within the same fair group often change the share substantially,
but do not assert an exact ratio from a short experiment. `schedlab analyze`
reports each candidate's measured share of the pair's CPU time beside the share
predicted by the kernel's load weights (`sched_prio_to_weight`: 1024 at nice 0,
110 at nice 10, 3 for IDLE), so you can see how close a short window comes.

**Pros/cons:** these policies are useful for background renders/builds because they
usually need no special permission when lowering importance. They cannot reserve
timely service for a foreground task. Negative nice needs an appropriate
`RLIMIT_NICE` or privilege; it remains fair scheduling.

**Exercise:** use the native interface below to compare nice 0, 5 and 15 with the
same work. Do not compare unrelated output sizes and call it a scheduler speedup.
Default permissions and cleanup are those of the quickstart.

**Sources and further reading:** [`getpriority`/`setpriority`](https://man7.org/linux/man-pages/man2/getpriority.2.html)
for the nice API and permissions; [`sched(7)`](https://man7.org/linux/man-pages/man7/sched.7.html)
for OTHER/BATCH/IDLE and autogroups; [scheduler nice design](https://docs.kernel.org/scheduler/sched-nice-design.html)
for the historical weight rationale. That last document discusses the 2.6.23
redesign, not an exact throughput ratio promised by every current kernel.

## 5. Lab: periodic audio, video, and an offline competitor

**Goal/hypothesis:** a short computation can still finish late if it waits for CPU.

```sh
python3 -m schedlab run audio --out results/audio
python3 -m schedlab run video --out results/video
python3 -m schedlab run mixed --out results/mixed
```

Audio generates a 440 Hz signal at 48 kHz and runs cascaded low-pass filters.
The default 10 ms period corresponds to 480 samples; eight stages are used by
the harness. Video generates 160x90 frames and performs eight 3x3 box-blur passes,
with a default 30 ms period. These are small, headless stand-ins for callback and
frame processing, not production codecs.

`audio` and `video` compare alone versus a same-CPU offline worker. `mixed` keeps
audio unchanged while the offline worker is OTHER, BATCH or IDLE. The background
duration covers the foreground release sequence.

**Observe:** separate wake lateness from processing wall time and CPU time.
If wall time grows while CPU work stays similar, descheduling is a plausible
cause. If both grow, consider frequency, cache/memory interference and code/data
changes. Correlation is not proof; a trace can establish causes.

**Pros/cons:** callback-shaped workloads expose deadline sensitivity without
requiring hardware. They omit the actual device/driver/server path. A synthetic
miss here is **not an ALSA, JACK or PipeWire XRUN**.

**Exercise:** change work or period using the native CLI. First establish a
baseline that comfortably fits, then intentionally exceed it. The correct result
is a measured miss, not silently dropped work. For a real sound-device experiment,
see [advanced experiments](ADVANCED.md#real-audio-and-video).

**Sources and further reading:** [the generated DSP/frame workloads](../native/work.cpp)
define the exact synthetic work; [PortAudio's callback constraints](https://www.portaudio.com/docs/v19-doxydocs/writing_a_callback.html)
explain why blocking/allocation matters; [ALSA PCM states and recovery](https://www.alsa-project.org/alsa-doc/alsa-lib/pcm.html)
define real underrun/overrun behavior. The [absolute-sleep API](https://man7.org/linux/man-pages/man2/clock_nanosleep.2.html)
explains the release mechanism, not a guarantee of prompt dispatch.

## 6. Lab: placement and number of workers

**Goal/hypothesis:** avoiding a shared logical CPU can help, but additional workers
can contend for shared physical resources.

```sh
python3 -m schedlab run affinity --out results/affinity
python3 -m schedlab doctor --out results/topology.json
```

This compares audio and one background worker on the same logical CPU, on two
different allowed logical CPUs, and audio with two background workers on one CPU.
It never starts a worker per machine CPU. A single-CPU allocation produces an
explicit skip for this preset.

**Observe:** effective affinity and `cpu_topology`. The first two allowed logical
CPUs might be SMT siblings, different cores, sockets, NUMA nodes or different
capacity cores. The automatic choice is reproducible, **not topology-optimal**.
Use `--cpu N` for the foreground and custom native invocations for an explicit
background choice. Never assume CPU 0 is available.

**Pros/cons:** pinning reduces migrations but may remove useful balancing.
Two SMT threads still share execution resources; separate cores can share caches,
power budgets and memory bandwidth. Placement is not CPU isolation.

**Exercise:** compare a sibling, a separate physical core and (if available) a
remote NUMA node. Record which is which before explaining the outcome.

**Sources and further reading:** [`sched_setaffinity(2)`](https://man7.org/linux/man-pages/man2/sched_setaffinity.2.html)
and [`pthread_setaffinity_np(3)`](https://man7.org/linux/man-pages/man3/pthread_setaffinity_np.3.html)
describe eligible CPUs and per-thread placement; [CPU topology](https://docs.kernel.org/admin-guide/cputopology.html)
and [scheduler capacity](https://docs.kernel.org/scheduler/sched-capacity.html)
explain why logical CPU IDs need not represent equal, independent resources.

## 7. Opt-in lab: FIFO, RR and equal-priority sharing

**Read [permissions and safety](ADVANCED.md#permissions-and-safety) first.**
Use a noncritical host with at least two allowed CPUs and a normal-priority shell.
These commands explicitly opt into RT; they never invoke sudo or grant themselves
capabilities:

```sh
python3 -m schedlab run rt --allow-rt --out results/rt
python3 -m schedlab run sharing --allow-rt --out results/sharing
```

`rt` keeps audio work and background work constant and compares OTHER/FIFO/RR
(RT priority 10). `sharing` runs two equal-priority fixed-work frame workers under
FIFO, then RR. It records individual job timelines and the effective RR quantum.

**Hypothesis/observation:** under FIFO, the first runnable equal-priority worker
typically completes its nonblocking batch before the other progresses. RR can
interleave batches **only if they last long enough to cross the quantum**. Compare
raw `start_ns` and `finish_ns` intervals across the two CSV files, first-start
delays, and context switches; throughput alone misses this distinction.
Insufficient inherited RT CPU-time limits can terminate the batch: this is an
explicit failure, not evidence that the kernel "fell back" to OTHER.

**Pros/cons:** FIFO avoids time-slice rotation among equals; RR provides rotation,
not equal service across different RT priorities. Both preempt ordinary tasks.
Neither requires PREEMPT_RT; kernel preemption affects the latency with which
they can actually be dispatched.

The native guardian remains OTHER on a different allowed logical CPU, with an
absolute timeout. The worker receives a nonincreasing `RLIMIT_RTTIME` (soft at
most one second, hard at most two seconds); a stricter inherited limit is retained.
The harness also kills process groups on timeout. A blocked or sleeping periodic
task can reset the RT CPU-time accounting, so this limit is **not** a wall-clock
watchdog. Separate logical CPUs are a minimum safeguard, not freedom from shared
hardware interference or a guarantee against a broken kernel.

**Exercise:** inspect whether the measured batch is longer than the RR quantum.
Change work carefully; leave the watchdog, RT throttling and priority ceilings intact.

**Sources and further reading:** [FIFO/RR ordering and privilege rules](https://man7.org/linux/man-pages/man7/sched.7.html),
[`sched_rr_get_interval`](https://man7.org/linux/man-pages/man2/sched_rr_get_interval.2.html)
for querying the actual quantum, and [`RLIMIT_RTTIME`](https://man7.org/linux/man-pages/man2/getrlimit.2.html)
for CPU-time accounting/reset behavior. The [guardian implementation](../native/work.cpp)
adds the separate wall-clock check; a resource limit is not a substitute for it.

## 8. Opt-in lab: DEADLINE admission and budgets

```sh
python3 -m schedlab run deadline --allow-rt --out results/deadline
```

**Goal/hypothesis:** a reservation differs from "highest priority".
The preset requests 1 ms runtime, 10 ms relative deadline and 10 ms period.
The native interface validates runtime <= deadline <= period and caps requested
utilization at 20%. The effective kernel parameters are read back.

**Observe:** success, permission failure or admission failure (`EBUSY`), and
budget-related lateness. The code **does not pin DEADLINE to one CPU**. Admission
may require affinity spanning its root scheduling domain; cpusets, containers
and other reservations can prevent admission even when the arithmetic is valid.
No fallback policy is substituted.

**Pros/cons:** CBS/EDF provides a different isolation model, but correct budgeting,
admission assumptions and the rest of the system still matter. A task whose work
exceeds runtime may be throttled; deadline priorities are not FIFO numbers.

**Exercise:** use native `--runtime-us`, `--deadline-us` and `--period-us` to vary
one budget on an authorized lab host. Do not bypass the utilization limit to
chase a universal "best" result.

**Sources and further reading:** [`sched_setattr(2)`](https://man7.org/linux/man-pages/man2/sched_setattr.2.html)
documents the ABI, parameter validation and admission errors;
[kernel DEADLINE documentation](https://docs.kernel.org/scheduler/sched-deadline.html)
explains EDF/CBS, bandwidth management and scheduling-domain constraints.
The 20% cap is a [lab safety choice](../native/work.cpp), not a kernel-wide limit.

## 9. Opt-in lab: priority inversion and inheritance

```sh
python3 -m schedlab run locks --allow-rt --out results/locks
```

**Goal/hypothesis:** priority alone cannot help a thread blocked by a lower-priority
lock owner. Three FIFO threads run on one CPU at priorities 10, 11 and 12.
The low thread holds a mutex. The high thread releases low and medium workers,
then tries to acquire that mutex. Medium performs a longer frame-filter batch.
The control uses `PTHREAD_PRIO_NONE`; the paired case uses `PTHREAD_PRIO_INHERIT`.

**Observe:** high-thread wait time and low/medium completion order in `core.lock`.
Without inheritance the medium thread can delay the low lock owner. With
inheritance the owner temporarily receives the blocked high thread's priority.
The JSON priorities describe configured base priorities, not a polling
measurement of the temporary inherited boost. The completion ordering is the
observable demonstration. The lock CSV's CPU-time field is zero/not instrumented;
its wall/response interval is the high-thread wait.

**Pros/cons:** PI addresses one dependency; it adds overhead and does not eliminate
deadlocks, long critical sections, I/O waits or arbitrary dependency chains.
The use of pthread mutex attributes is intentional: `std::mutex` cannot request
POSIX PI. C++20 semaphores coordinate setup outside the measured lock interval.

**Exercise:** compare waits and completion order, then inspect an actual audio
application for a dependency on an ordinary-priority worker. Raising only the
callback's priority may make that dependency worse.

**Sources and further reading:** [`pthread_mutexattr_setprotocol`](https://man7.org/linux/man-pages/man3/pthread_mutexattr_setprotocol.3p.html)
defines PI; [Linux PI futexes](https://docs.kernel.org/locking/pi-futex.html)
explain the kernel mechanism; [`PTHREAD_EXPLICIT_SCHED`](https://man7.org/linux/man-pages/man3/pthread_attr_setinheritsched.3.html)
explains why the thread-creation attributes matter. Runtime support and
permission still have to be checked on the actual build.

## 10. Lab: demand faults, prefaulting and locking memory

```sh
python3 -m schedlab run memory --out results/memory
```

**Goal/hypothesis:** a bounded computation may fault when first touching memory.
Each audio job writes 128 pages of a separate 16 MiB probe mapping. Cases are
demand paging, write-prefaulting, and prefaulting plus `mlock` on that mapping.
Data/results buffers are preallocated in every condition.

**Observe:** minor/major fault counts and latency tails. `mlock` may skip because
the soft memlock limit is too low. Demand faults normally increase minor faults;
major faults require backing-store activity and may remain zero.

**Pros/cons:** prefaulting removes first-touch costs for those pages, not later
reclaim. `mlock` resists paging of the locked mapping but consumes a limited
resource. This lab **does not lock the entire process**, stack, libraries or
future allocations, and does not induce swapping. A production callback should
preallocate/prefault appropriate buffers and stack and avoid unbounded allocators;
an indiscriminate `mlockall(MCL_FUTURE)` can make later allocations fail.

**Exercise:** contrast CPU-time and wall-time outliers. Do not enable whole-machine
memory pressure or drop caches to make a demonstration more dramatic.

**Sources and further reading:** [`mlock`/`mlockall`](https://man7.org/linux/man-pages/man2/mlock.2.html)
for locking, limits and failure modes; [`mmap`](https://man7.org/linux/man-pages/man2/mmap.2.html)
for anonymous mappings; [`getrusage`](https://man7.org/linux/man-pages/man2/getrusage.2.html)
for the distinction between minor and major faults. None promises that
prefaulting alone prevents future reclaim or all other latency sources.

## 11. Opt-in delegation lab: cgroup weight versus quota

```sh
python3 -m schedlab run cgroup --cgroup-parent /path/to/your/delegated/subtree \
  --out results/cgroup
```

The path is a **placeholder for an administrator-provided delegation**, not a
directory the lab creates at the cgroup root. It must already be a domain cgroup
with `cpu` enabled in `cgroup.subtree_control`, and allow creating/moving tasks
into children. Running without a parent gives an explanatory skip.

**Goal/hypothesis:** relative share and aggregate bandwidth are different controls.
Two same-CPU OTHER frame workers run in fresh sibling groups: first weights
100 and 400, then unlimited and `20000 100000` (20 ms per 100 ms period).
The worker and its guardian join their leaf before forking. The Python watchdog
remains outside the leaves.

**Observe:** completed work, `cpu.stat` throttling and pressure counters, plus
the ancestor limits in the manifest. The cap is aggregate CPU time, not
"20% on each CPU". A child cannot override an ancestor's cap. Group counters
include setup, guardian and artifact activity; native job metrics cover only
the measured work.

**Pros/cons:** weights share spare CPU under contention; quota constrains aggregate
use but can cause burst/throttle latency. Neither `cpu.weight` nor `cpu.max`
governs FIFO/RR. Kernel RT-group support has separate limitations.

The helper never enables a controller, changes the parent's controls, or moves
the caller. It removes only the fresh empty leaves it created. Cleanup failure
is an error, not ignored. Memory, I/O, cpuset/NUMA and optional systemd examples
are in [advanced experiments](ADVANCED.md).

**Sources and further reading:** [v2 CPU interface files](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpu-interface-files)
and [delegation rules](https://docs.kernel.org/admin-guide/cgroup-v2.html#delegation);
[fair-class bandwidth control](https://docs.kernel.org/scheduler/sched-bwc.html)
for throttling behavior; [RT group scheduling](https://docs.kernel.org/scheduler/sched-rt-group.html)
for the separate FIFO/RR mechanism and its configuration restrictions.
Current v2 documentation also describes weight/bandwidth callbacks for BPF
schedulers. Their effect depends on kernel and sched_ext implementation support;
this does not turn `cpu.weight` or `cpu.max` into FIFO/RR bandwidth controls.

## 12. Command reference and reproducibility

Run commands from the repository root; no Python package installation is needed.
The `uv run poe …` forms are optional shortcuts for the plain commands beside them.

| Command | Purpose |
|---|---|
| `cmake --preset P && cmake --build --preset P`, `uv run poe build` | Native C++20 build in `build/P`; presets `default`, `gcc`, `clang`, `asan`, `tsan`, `static` and cross targets |
| `SCHEDLAB_PRESET=asan uv run poe test` (or `tsan`) | Tests under AddressSanitizer+UBSan or ThreadSanitizer; a report exits 99 and fails the test. Sanitized timings are not measurements |
| `python3 -m unittest discover -s tests -v`, `uv run poe test` | Unprivileged correctness/integration tests (`poe test` builds first) |
| `cmake --build --preset P --target clean`, `uv run poe clean` | Remove the built native executables |
| `SCHEDLAB_NATIVE_DIR=build/P`, `SCHEDLAB_PRESET=P` | Use a non-default build for tests and runs (the latter for `poe` tasks) |
| `uv run poe format`, `uv run poe lint` | Apply / check ruff, clang-format and clang-tidy at locked versions; `lint` configures `build/P` for its compile database |
| `python3 -m schedlab doctor [--out FILE]` | Read-only inventory; unknown/unreadable facts remain explicit |
| `python3 -m schedlab run PRESET` | Run any preset from `--help`; generate a fresh result directory |
| `--repetitions N` | 1..10 repetitions, default 3 |
| `--out DIR`, `--label TEXT`, `--cpu N` | Output, human experiment label, allowed worker CPU |
| `--allow-rt` | Explicit consent to attempt RT; not a permission grant |
| `--cgroup-parent DIR` | Explicit, existing delegated subtree |
| `python3 -m schedlab report RUN.json --out FILE.html` | Rebuild a report from saved results |
| `python3 -m schedlab replay RUN.json --out DIR` | Rerun a validated preset with current code; renew privileged opt-ins |
| `python3 -m schedlab compare DIR1 DIR2 --out DIR3` | Compare manifests from directories or `run.json` paths |
| `python3 -m schedlab analyze DIR... [--format text\|markdown\|json] [--out FILE]`, `uv run poe analyze DIR...` | Per-case table and bar plot, preset findings, caveats; several runs add a same-case comparison |
| `./build/default/sched-work --help` | Full native option list and bounds |
| `./build/default/sched-work --build-info` | Compiler, flags, sanitizers and C/C++ libraries of this binary |
| `./build/default/thread-pipeline --help` | Ordinary-priority C++20 bounded-queue/ownership example |

Example custom unprivileged native run:

```sh
mkdir -p results/custom
./build/default/sched-work --mode audio --policy other --jobs 100 --work 16 \
  --period-us 10000 --memory prefault --output results/custom/audio \
  > results/custom/audio.json
```

Native options: `--mode offline|audio|video|lock`, `--policy other|batch|idle|fifo|rr|deadline`,
`--nice -20..19`, `--priority 1..20` (lock base <=18), `--cpu N`, `--jobs 1..20000`,
`--work 1..128`, `--warmup 0..100`, `--period-us 1000..100000`,
`--runtime-us 100..100000`, `--deadline-us 1000..100000`,
`--duration-ms 0..10000` (offline only), `--timeout-ms 100..15000`,
`--start-ns N`, `--memory demand|prefault|mlock`, `--protocol none|inherit`,
`--allow-rt`, `--output PREFIX`. A value follows its option (`--jobs 40`) or an
equals sign (`--jobs=40`); option names are never abbreviated.
`--start-ns` is an absolute monotonic common start in the next two seconds after
setup; omit it for ordinary manual runs. Audio is capped at ten million samples.
Choose job count/period/work that fit the watchdog, rather than expecting it to
extend itself. Direct native runs explicitly overwrite the chosen artifact prefix;
the Python runner prevents accidental reuse of a whole run directory.

Native exit status: 0 success, 77 unsupported/permission/safety skip, 2 invalid
input, 1 actual failure. The harness returns 1 for any failed run, but 0 for an
honestly skipped capability. Check the printed counts or manifest when automation
requires every optional feature. Prefixing the test command (`uv run poe test` or
`python3 -m unittest discover -s tests -v`) with `SCHEDLAB_TEST_RT=1` makes a
separate opt-in test run on a prepared host; unlike the ordinary suite, it
requires FIFO/RR and PI to work, not merely skip. `SCHEDLAB_TEST_DEADLINE=1`
similarly requires DEADLINE admission to succeed. `SCHEDLAB_CGROUP_PARENT=/your/delegation`
enables delegated CPU-control checks, including leaf cleanup. These are opt-in
environment variables, never enabled by CI.

`replay` never executes stored shell commands, never inherits RT consent or a
delegated path, and uses the **current checkout's preset implementation**. Keep the
same commit, flags and compiler for a controlled comparison; each manifest records
them (`environment.native_build`, plus the binary hash) so a change is visible.
An unavailable saved CPU requires `--cpu` to remap deliberately.

Manifests include hostname, process/boot metadata and paths. Review them before
sharing. Reports load no remote resources. Compare uses links to original CSV
locations; retain the source run directories or move them with an updated report.

**Interface sources:** [CLI parsing and replay validation](../schedlab/__main__.py),
[preset parameters and process cleanup](../schedlab/runner.py),
[environment/config inspection](../schedlab/environment.py), and
[report generation](../schedlab/report.py) are authoritative for these project
commands. Python's [`subprocess` documentation](https://docs.python.org/3/library/subprocess.html)
explains process/session handling and timeout behavior; in particular, `preexec_fn`
is not safe in a multithreaded launcher, which is why this harness remains
single-threaded.

## 13. Coverage, decisions, troubleshooting

| Topic | Executable automated lab | Opt-in/manual or conceptual boundary |
|---|---|---|
| OTHER/nice, BATCH, IDLE | offline, fairness, mixed | Negative nice requires permissions |
| Audio/video work and deadlines | quick, audio, video | No hardware device or production codec |
| Affinity and worker count | affinity | CPU/IRQ isolation and NUMA placement are manual |
| FIFO/RR and equal-priority behavior | rt, sharing | Explicit RT consent and safeguards |
| DEADLINE | deadline | Kernel admission, capabilities and root domain constraints |
| Priority inversion/inheritance | locks | POSIX PI support and authorized FIFO threads |
| Prefault/mlock and page faults | memory | No swap stress; locks only a probe mapping |
| cgroup weight/quota | cgroup | Explicit CPU delegation; memory/I/O/cpusets manual |
| Kernel and architecture comparison | doctor, replay, compare | Boot/config changes performed separately by an administrator |
| PREEMPT_RT, IRQ chains, isolation, power, uclamp, sched_ext | Inventory and documented recipes | No global tuning or scheduler loading |
| Actual XRUNs, cyclictest/rtla, ffmpeg | Not in default suite | Optional hardware/tool-specific experiments |

| Need | Start here | Avoid assuming |
|---|---|---|
| Background render/build | BATCH or positive nice, bounded worker count | Nice is a utilization cap |
| Idle-only optional processing | IDLE with acceptable starvation | Guaranteed completion |
| Low-latency audio | Fix buffering/allocations/dependencies, then modest FIFO/RR if appropriate | Highest RT priority fixes drivers/IRQs |
| Budgeted periodic computation | DEADLINE after admission and workload analysis | Average CPU time is a sufficient runtime budget |
| Aggregate service limits | Delegated cgroup quota/weight | Thread nice replaces group limits |
| Tight latency tails | Kernel/hardware investigation plus application changes | A short test's maximum is a bound |

| Symptom | Check / interpretation |
|---|---|
| C++20 header/semaphore errors | Compiler **and standard library** age; use GCC 11+ or suitable Clang/library |
| `set scheduler/nice: Operation not permitted` | RTPRIO/NICE limits, capabilities, seccomp and RT group restrictions; no fallback occurred |
| `mlock ... Cannot allocate memory` | Memlock limit or resource exhaustion; not necessarily out of physical RAM |
| DEADLINE `EBUSY` / `EPERM` | Admission/affinity/root-domain constraints or privilege |
| Guardian safety skip | Parent must be OTHER with nice <=0; FIFO/RR need a selected CPU and two allowed CPUs |
| RT task killed by signal | Inherited `RLIMIT_RTTIME`, kernel limits, or other signal; inspect reason instead of trusting partial files |
| Watchdog expiration | Overloaded host, excessive work/jobs, quota/starvation; reduce workload, do not disable safeguards |
| Kernel config unknown | `/proc/config.gz` or matching installed config not readable; see kernel guide |
| cgroup permission/read-only errors | No usable delegation, v1 hierarchy, namespace restrictions; OpenRC itself is not a failure |
| Unexpected fairness | Autogroups/cgroups, sched_ext, CPU selection, external load, short window |
| Native affinity-mask error | This build uses libc's `CPU_SETSIZE` mask (normally 1024 CPUs); larger masks are outside native support |
| Different results across boots | Confirm code, flags, topology, preemption, firmware, governor, thermals and background work |

**Glossary:** runnable = eligible to execute; blocked = waiting for a dependency;
preemption = replacing a running task; priority inversion = high-priority work
waiting indirectly on lower-priority work; PI = priority inheritance; IRQ = device
interrupt; XRUN = device/server buffer overrun or underrun; WCET = justified
worst-case execution-time bound; SMT = logical threads sharing a physical core;
NUMA = nonuniform memory access; quota = bandwidth ceiling; weight = relative share;
admission = deciding whether requested reservations can be accepted.

**Reference paths for this matrix:** use the linked [policy definitions](#3-policy-map-what-the-knobs-actually-do),
[permission/limit sources](ADVANCED.md#permissions-and-safety),
[kernel/config sources](KERNELS_AND_HARDWARE.md#3-preemption-modes-and-relevant-config),
and [device-XRUN sources](ADVANCED.md#audio-measure-a-real-xrun-do-not-rename-a-synthetic-miss)
when diagnosing a particular row. These are decision aids, not additional
automated workloads or guarantees.

## 14. Implementation and trustworthy sources

`native/work.cpp` uses C++20 vectors/spans/arrays for preallocated buffers, RAII
for mapped memory and files, atomics and semaphores for setup coordination. Linux/POSIX
interfaces remain where ISO C++ has no equivalent: scheduler policies,
absolute clock sleeps, affinity, resource limits and PI mutex attributes. No
Python executes the measured callbacks. There is deliberately no duplicate C
engine: the language alone does not change Linux scheduler semantics, and two
engines would introduce extra differences into comparisons.

The policy/getparam/setpolicy wrapper intentionally uses named `SYS_sched_*`
constants from the target's headers, never architecture-numbered syscalls.
For a versioned example, musl v1.2.5's
[`sched_getscheduler`](https://git.musl-libc.org/cgit/musl/tree/src/sched/sched_getscheduler.c?h=v1.2.5),
[`sched_getparam`](https://git.musl-libc.org/cgit/musl/tree/src/sched/sched_getparam.c?h=v1.2.5) and
[`sched_setscheduler`](https://git.musl-libc.org/cgit/musl/tree/src/sched/sched_setscheduler.c?h=v1.2.5)
are `ENOSYS` stubs; check the [current source directory](https://git.musl-libc.org/cgit/musl/tree/src/sched)
for other releases. Calling the Linux per-thread syscalls directly provides the
intended semantics without pretending an unavailable libc call succeeded.

`schedlab/` contains only standard-library Python: orchestration, read-only
inspection, metric math, reporting and delegated cgroup lifecycle.
`tests/` covers output schemas, metrics, option validation, media validity,
requested/effective settings, faults, skips and watchdog/process-group cleanup.
CI builds/tests GCC and Clang, and tests a static Alpine-built musl native core
on the host (with the host's Python harness), plus architecture builds. Cross-compilation
is not native execution and no hosted runner certifies real-time performance.

**Build/test sources:** the [checked-in workflow](../.github/workflows/ci.yml)
specifies the actual matrix and commands; [GitHub-hosted runner documentation](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
describes the execution platforms; [workflow artifacts](https://docs.github.com/en/actions/using-workflows/storing-workflow-data-as-artifacts)
explain report retention/downloads. [GCC cross-toolchain configuration](https://gcc.gnu.org/install/configure.html)
distinguishes build/host/target systems. The [test suite](../tests/test_lab.py)
shows which assertions are exercised and which checks require explicit opt-in.

Normative references:

- [sched(7)](https://man7.org/linux/man-pages/man7/sched.7.html)
- [sched_setattr(2)](https://man7.org/linux/man-pages/man2/sched_setattr.2.html)
- [sched_setscheduler(2)](https://man7.org/linux/man-pages/man2/sched_setscheduler.2.html)
- [getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html)
- [mlock(2)](https://man7.org/linux/man-pages/man2/mlock.2.html)
- [pthread_mutexattr_setprotocol(3p)](https://man7.org/linux/man-pages/man3/pthread_mutexattr_setprotocol.3p.html)
- [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)
- [Linux DEADLINE](https://docs.kernel.org/scheduler/sched-deadline.html)
- [EEVDF](https://docs.kernel.org/scheduler/sched-eevdf.html)

The kernel documentation and system-call contracts are the technical authority.
Continue with [kernels/hardware](KERNELS_AND_HARDWARE.md) and
[advanced manual experiments](ADVANCED.md), keeping the beginner defaults intact.
