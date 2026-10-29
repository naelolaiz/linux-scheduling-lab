# Linux scheduling lab

Learn Linux scheduling by **doing real work and measuring it**: synthesized audio
DSP, generated video filtering, offline throughput, contention, RT policies,
priority inversion, page faults and cgroup CPU controls.

Feature references: [Linux scheduling policies](https://man7.org/linux/man-pages/man7/sched.7.html),
[priority-inheritance mutexes](https://man7.org/linux/man-pages/man3/pthread_mutexattr_setprotocol.3p.html),
[memory locking](https://man7.org/linux/man-pages/man2/mlock.2.html), and
[cgroup resource controls](https://docs.kernel.org/admin-guide/cgroup-v2.html).
For the synthetic media work, see the [native implementation](native/work.cpp)
and [audio-callback design guidance](https://www.portaudio.com/docs/v19-doxydocs/writing_a_callback.html).
Each lab in the tutorial has its own nearby API sources and further reading.

The native engine is **C++20**, built with [CMake presets](CMakePresets.json);
Python's standard library orchestrates experiments and produces offline HTML
reports. No audio device, GUI, media framework, root, or systemd is needed for
the quickstart. Gentoo/OpenRC is a first-class setup.

```sh
uv run poe test      # CMake build in build/default, then the unprivileged test suite
uv run poe lab run quick --repetitions 1 --out results/first
# Open results/first/report.html in a browser; no server or internet needed.
```

[uv](https://docs.astral.sh/uv/) and [Poe the Poet](https://poethepoet.natn.io/)
are optional: each task in [`pyproject.toml`](pyproject.toml) (`uv run poe --help`)
wraps one plain command, and the lab itself needs only the standard library.
Without uv, the same quickstart is:

```sh
cmake --preset default && cmake --build --preset default
python3 -m unittest discover -s tests -v
python3 -m schedlab run quick --repetitions 1 --out results/first
```

Other presets build side by side in `build/<preset>`: `SCHEDLAB_PRESET=clang uv run poe test`,
or `SCHEDLAB_NATIVE_DIR=build/clang python3 -m schedlab …` after a plain `clang` build.
`uv run poe format` and `uv run poe lint` apply and check [ruff](https://docs.astral.sh/ruff/),
[clang-format](https://clang.llvm.org/docs/ClangFormat.html) and
[clang-tidy](https://clang.llvm.org/extra/clang-tidy/) (configured in `pyproject.toml`,
`.clang-format` and `.clang-tidy`; versions locked in `uv.lock`).

**Start with the [tutorial](docs/TUTORIAL.md).** It explains each hypothesis,
command, measurement, tradeoff, permission and exercise. Continue with the
[kernel/hardware comparison track](docs/KERNELS_AND_HARDWARE.md) and
[optional resource/hardware experiments](docs/ADVANCED.md). The
[threading and code practices chapter](docs/THREADING_AND_CODE.md) includes a
runnable C++20 [`std::jthread`](https://en.cppreference.com/w/cpp/thread/jthread.html)/bounded-queue
audio pipeline, with its [publication and shutdown protocol](native/pipeline.cpp).

```sh
python3 -m schedlab doctor --out results/environment.json
python3 -m schedlab run fairness --out results/fairness
python3 -m schedlab run mixed --out results/mixed
python3 -m schedlab run memory --out results/memory
python3 -m schedlab --help
```

Runs contain raw timestamp CSVs, requested/effective scheduler settings,
[WAV](https://docs.python.org/3/library/wave.html)/[PGM](https://manpages.debian.org/bookworm/netpbm/pgm.5.en.html)
artifacts, environment/config snapshots, per-run statistics and an interactive,
self-contained `report.html`. The dashboard filters by status/policy, ranks any
latency or throughput metric, compares percentile tails, and needs no server.
`replay` and `compare` support experiments across kernel boots and machines.
Existing output directories are never overwritten by `run`.

Measurement sources: [monotonic and thread-CPU clocks](https://man7.org/linux/man-pages/man2/clock_gettime.2.html),
[resource/fault counters](https://man7.org/linux/man-pages/man2/getrusage.2.html),
and the lab's [metric definitions](schedlab/metrics.py) and
[replay/reporting implementation](schedlab).

**Safety:** default runs are unprivileged, finite and limited to a few workers.
RT requires `--allow-rt`, permission and at least two allowed CPUs. Independent
watchdogs remain ordinary tasks; no automatic sudo or global tuning is performed.
Unavailable capabilities produce explicit skips, not mislabeled fallback runs.
Use a noncritical machine for opt-in experiments.

Safety references: [RT privilege and CPU-time limits](https://man7.org/linux/man-pages/man2/getrlimit.2.html)
and [DEADLINE admission/bandwidth rules](https://docs.kernel.org/scheduler/sched-deadline.html).
The lab's [watchdog and opt-in checks](native/work.cpp) are additional safeguards,
not kernel latency guarantees.

**Requirements:** Linux, CMake 3.21+ (with GNU Make or Ninja), GCC 11+ or Clang 14+
with a C++20 standard library (including semaphores), Python 3.10+. Optional: uv
for the `poe` tasks. The baseline targets Linux 5.4+;
optional features are detected rather than assumed. glibc and musl are covered
by CI, with x86-64/arm64 native jobs and arm64/RISC-V cross-compilation.
See the tutorial for exact support boundaries.

Build references: [GCC C++20 support](https://gcc.gnu.org/projects/cxx-status.html#cxx20),
[Clang C++20 support](https://clang.llvm.org/cxx_status.html#cxx20),
[CMake presets](https://cmake.org/cmake/help/latest/manual/cmake-presets.7.html), the exact
[CI matrix](.github/workflows/ci.yml), and [GitHub-hosted runner platforms](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
For this init-independent setup, see [Gentoo GCC](https://wiki.gentoo.org/wiki/GCC)
and [OpenRC documentation](https://wiki.gentoo.org/wiki/OpenRC).

This is an educational measurement tool, **not an RT certification suite**.
Observed maxima are not WCET bounds; synthetic audio misses are not
[device XRUNs](https://www.alsa-project.org/alsa-doc/alsa-lib/pcm.html).
CI checks correctness and portability, never universal performance rankings.
