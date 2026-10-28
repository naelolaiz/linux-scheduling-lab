# Linux scheduling lab

Learn Linux scheduling by **doing real work and measuring it**: synthesized audio
DSP, generated video filtering, offline throughput, contention, RT policies,
priority inversion, page faults and cgroup CPU controls.

The native engine is **C++20**; Python's standard library orchestrates experiments
and produces offline HTML reports. No audio device, GUI, media framework, root,
or systemd is needed for the quickstart. Gentoo/OpenRC is a first-class setup.

```sh
make
make test
python3 -m schedlab run quick --repetitions 1 --out results/first
# Open results/first/report.html in a browser; no server or internet needed.
```

**Start with the [tutorial](docs/TUTORIAL.md).** It explains each hypothesis,
command, measurement, tradeoff, permission and exercise. Continue with the
[kernel/hardware comparison track](docs/KERNELS_AND_HARDWARE.md) and
[optional resource/hardware experiments](docs/ADVANCED.md). The
[threading and code practices chapter](docs/THREADING_AND_CODE.md) includes a
runnable C++20 `std::jthread`/bounded-queue audio pipeline.

```sh
python3 -m schedlab doctor --out results/environment.json
python3 -m schedlab run fairness --out results/fairness
python3 -m schedlab run mixed --out results/mixed
python3 -m schedlab run memory --out results/memory
python3 -m schedlab --help
```

Runs contain raw timestamp CSVs, requested/effective scheduler settings, WAV/PGM
artifacts, environment/config snapshots, per-run statistics and `report.html`.
`replay` and `compare` support experiments across kernel boots and machines.
Existing output directories are never overwritten by `run`.

**Safety:** default runs are unprivileged, finite and limited to a few workers.
RT requires `--allow-rt`, permission and at least two allowed CPUs. Independent
watchdogs remain ordinary tasks; no automatic sudo or global tuning is performed.
Unavailable capabilities produce explicit skips, not mislabeled fallback runs.
Use a noncritical machine for opt-in experiments.

**Requirements:** Linux, GNU Make, GCC 11+ or Clang 14+ with a C++20 standard
library (including semaphores), Python 3.9+. The baseline targets Linux 5.4+;
optional features are detected rather than assumed. glibc and musl are covered
by CI, with x86-64/arm64 native jobs and arm64/RISC-V cross-compilation.
See the tutorial for exact support boundaries.

This is an educational measurement tool, **not an RT certification suite**.
Observed maxima are not WCET bounds; synthetic audio misses are not device XRUNs.
CI checks correctness and portability, never universal performance rankings.
