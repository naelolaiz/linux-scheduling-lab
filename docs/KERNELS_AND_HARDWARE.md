# Comparing kernels, processors and architectures

This track answers a narrower question than "which kernel or CPU is best?":
**for this unchanged workload, placement and environment, what changed in the
observed timing distribution?** Run the [beginner tutorial](TUTORIAL.md) first.

The lab never installs/rebuilds a kernel, edits the bootloader, loads a BPF
scheduler, disables RT throttling, changes a governor, or moves interrupts.
Administration and reboot decisions remain separate, manual work.

## 1. A reproducible comparison protocol

Start with one physical machine and two known-good boot entries. Have recovery
access before experimenting with custom kernels. Distinguish:

| Kernel family | What the comparison can test | Confounders to retain |
|---|---|---|
| Distribution kernel | Practical supported defaults and integration | Vendor patches, config, firmware, package updates |
| Mainline release | Upstream behavior at a known release/commit | Different config/toolchain can outweigh version differences |
| Long-term supported (LTS) branch | Stability/maintenance policy and backported fixes | "LTS" is not a preemption mode or a latency guarantee |
| Custom config/build | A specific preemption/timer/isolation choice | Build flags, drivers, config changes and administrator error |
| PREEMPT_RT-enabled build | More preemptible kernel paths and threaded interrupt behavior | Driver support, RT patch level on older trees, power/throughput cost |

Use the **same lab commit, compiler, build flags and binary** across same-architecture
kernel boots if possible. Across architectures keep the same source, optimization
intent, job count, audio period and work parameter; record that the binaries differ.
Do not use `-march=native` on one machine and generic code on another without
calling that a separate experiment.

On boot A:

```sh
make
python3 -m schedlab run audio --repetitions 5 --label "machine-A / boot-A / audio" \
  --out results/boot-a-audio
python3 -m schedlab run fairness --repetitions 5 --label "machine-A / boot-A / shares" \
  --out results/boot-a-fairness
```

After manually choosing boot B, keeping the files:

```sh
python3 -m schedlab replay results/boot-a-audio/run.json \
  --label "machine-A / boot-B / audio" --out results/boot-b-audio
python3 -m schedlab compare results/boot-a-audio results/boot-b-audio \
  --out results/boot-comparison
```

Replay reads only a validated preset, repetition count and CPU choice, not saved
commands. It uses the current implementation; check lab version, source commit
and binary hash before attributing a difference to the kernel. Privileged consent
must be renewed. On another machine, remap an unavailable CPU with `--cpu N`,
and explain the physical placement in `--label`.

The manifest records kernel release/version/command line, architecture, identity
and topology, effective allowed CPUs, config availability, cgroup ancestors,
limits, CPU-frequency hints, thermals, clocksource and virtualization hints.
The native JSON records compiler and C++ language version. Raw monotonic timestamps
are meaningful **within a boot/run**; do not subtract timestamps from different
machines or reboots. Compare durations and distributions instead.

Keep a small experimental notebook in your own results storage:

| Controlled fact | Record |
|---|---|
| Machine/boot | Board or system identifier, kernel commit/package, firmware/microcode |
| Code | Source commit, dirty changes if any, compiler, flags, binary hash |
| Work | Preset/version, passes, jobs, period, number and kind of contenders |
| Placement | Logical IDs plus physical core/socket/NUMA/capacity relationship |
| Operating state | AC/battery, cooling, initial temperature, governor/EPP, background services |
| Limits | Container/cpuset/ancestor quota, capabilities, RTPRIO/MEMLOCK/RTTIME |
| Evidence | Raw data, report, sample count/repetitions, tracing or actual XRUN logs if used |

Alternate A/B/A where practical; allow thermal recovery; repeat at different
times. Do not mix a warm cache result, cold boot result and throttled battery
result into one "kernel comparison". A short observed maximum is not a WCET bound.
Rare-tail claims need much longer controlled runs and a justified system model.

## 2. Compile-time support is not the active mode

```sh
python3 -m schedlab doctor --out results/kernel-inventory.json
uname -a
cat /proc/cmdline
```

The inspector tries `/proc/config.gz`, `/boot/config-$(uname -r)`, then
`/lib/modules/$(uname -r)/build/.config`. It saves the source and hash, or explicit
unavailable reasons. Installed build files may be stale; verify them against the
running image. Missing config entries are **unknown/not present in that config**,
not automatically disabled. `CONFIG_IKCONFIG=y` plus `CONFIG_IKCONFIG_PROC=y`
can expose `/proc/config.gz`; many distributions omit or restrict it.

`CONFIG_PREEMPT_DYNAMIC=y` means the preemption model can be selected at boot
(and on some builds changed through debugfs). A compile-time default is not proof
of the current mode. Where readable, `/sys/kernel/debug/sched/preempt` identifies
the current choice; `/proc/cmdline` supplies boot intent. The tool does not mount
debugfs or write there. `/sys/kernel/realtime` is another hint where provided.
If these are absent, report the active mode as unknown rather than inferring it
from the kernel filename.

## 3. Preemption modes and relevant config

The names and dependencies below are verified against the
[upstream v6.12 preemption Kconfig](https://github.com/torvalds/linux/blob/v6.12/kernel/Kconfig.preempt),
[init Kconfig](https://github.com/torvalds/linux/blob/v6.12/init/Kconfig),
and linked kernel documentation. Dependencies and menu names vary in later/vendor
trees; use the Kconfig/help **from the exact source you build**. v6.12 is a
documentation reference, not a requirement for the unprivileged labs.

| Options / mode | What it affects; dependencies / availability | Tradeoff and useful experiment |
|---|---|---|
| `CONFIG_PREEMPT_NONE` | No forced preemption within ordinary kernel paths; user tasks still preempt | Throughput-oriented; inspect audio wake tails under actual syscall/device load |
| `CONFIG_PREEMPT_VOLUNTARY` | Adds voluntary kernel rescheduling points; architecture must permit preemption | Intermediate latency/overhead; compare the same mixed workload |
| `CONFIG_PREEMPT` | Preempts more kernel execution outside protected sections | Lower latency potential with overhead; not elimination of IRQ-off/raw-lock regions |
| `CONFIG_PREEMPT_DYNAMIC` | Depends on architecture `HAVE_PREEMPT_DYNAMIC`; v6.12 excludes PREEMPT_RT | Same binary can choose none/voluntary/full; active choice must be recorded |
| `CONFIG_PREEMPT_RT` | v6.12: `EXPERT && ARCH_SUPPORTS_RT`; PI-aware sleeping locks and forced threading for most IRQs | Can improve tails; driver/architecture support and low-level unpreemptible paths still matter |
| `CONFIG_PREEMPT_LAZY` | Exists in later kernel lines/configurations; not a v6.12 selectable option | Deferred lower-urgency preemption is version-specific; inspect exact Kconfig rather than assuming a universal mode |
| `CONFIG_HIGH_RES_TIMERS` | High-resolution timer infrastructure with architecture/clock-event support | Finer releases, but actual timer/interrupt latency still measured separately |
| `CONFIG_HZ`, `CONFIG_HZ_100/250/300/1000` | Scheduler tick selection available per architecture/config | Higher tick rate can add overhead; HZ does not by itself set hrtimer precision |
| `CONFIG_HZ_PERIODIC`, `CONFIG_NO_HZ_IDLE` | Periodic ticks versus tick suppression while idle | Power savings versus idle-entry/wake overhead; record idle states and wake tails |
| `CONFIG_NO_HZ_FULL` | Adaptive ticks on selected CPUs with supported context tracking; requires housekeeping | Helps selected jitter-sensitive workloads, not a universal desktop optimization |
| `CONFIG_RCU_NOCB_CPU`, `CONFIG_CPU_ISOLATION` | RCU callback offload and isolation support | Boot/runtime setup still required; helpers need capacity elsewhere |
| `CONFIG_SMP`, `CONFIG_SCHED_SMT`, `CONFIG_SCHED_MC`, `CONFIG_SCHED_CLUSTER` | Multicore/topology-aware scheduling; availability is architecture-specific | Use physical topology, not CPU-number arithmetic, for affinity comparisons |
| `CONFIG_NUMA`, `CONFIG_NUMA_BALANCING` | Node topology and automatic page/task placement; v6.12 balancing requires SMP/NUMA/MIGRATION and excludes PREEMPT_RT | First touch, migration and remote-memory costs affect tails; compare matched placement |
| `CONFIG_CGROUPS`, `CONFIG_CGROUP_SCHED`, `CONFIG_FAIR_GROUP_SCHED` | Group framework and fair CPU weighting | Hierarchy changes who shares; inspect all ancestors |
| `CONFIG_CFS_BANDWIDTH` | Depends on FAIR_GROUP_SCHED; supplies fair bandwidth limiting | `cpu.max` throttling can worsen periodic response |
| `CONFIG_RT_GROUP_SCHED` | Separate FIFO/RR group budgeting, under CGROUP_SCHED | Not cgroup-v2 cpu.max for RT; can block RT admission in groups without configured RT bandwidth |
| `CONFIG_CPUSETS` | Under cgroups, depends on SMP in v6.12 | Effective CPU/node masks constrain affinity; exclusivity/partitions need additional setup |
| `CONFIG_MEMCG`, `CONFIG_BLK_CGROUP` | Memory and block-I/O accounting/control; block controller requires BLOCK | Limits can introduce reclaim or I/O waits; compare controller events and application timings |
| `CONFIG_CGROUP_WRITEBACK` | Depends on MEMCG and BLK_CGROUP | Needed for supported writeback attribution; filesystem/device support still matters |
| `CONFIG_BFQ_GROUP_IOSCHED`, `CONFIG_BLK_DEV_THROTTLING` | Actual I/O weight/throttling mechanisms, beyond generic BLK_CGROUP | `io.weight` does not work identically with every I/O scheduler |
| `CONFIG_IRQ_FORCED_THREADING`, `CONFIG_RT_MUTEXES` | IRQ threading and kernel PI infrastructure; often selected/hidden rather than independent user knobs | Trace actual IRQ/dependency chains; do not treat checking a symbol as tuning the entire chain |
| `CONFIG_UCLAMP_TASK`, `CONFIG_UCLAMP_TASK_GROUP` | Utilization hints; group option requires task clamps and CGROUP_SCHED | Can affect frequency/capacity decisions; not a bandwidth cap or reservation |
| `CONFIG_CPU_FREQ`, `CONFIG_CPU_FREQ_GOV_SCHEDUTIL`, `CONFIG_ENERGY_MODEL` | Frequency selection and energy-aware placement infrastructure | Board/driver/policy differences; measure cooling, energy and latency together |
| `CONFIG_SCHED_DEBUG`, `CONFIG_SCHEDSTATS`, `CONFIG_PSI` | Scheduler introspection, statistics and pressure information | Observability can add overhead; keep instrumentation consistent |
| `CONFIG_POSIX_TIMERS` | POSIX clock/timer support, including thread CPU clock needed here | Minimal custom kernels disabling it are outside the timing-core baseline |
| `CONFIG_SCHED_AUTOGROUP` | Automatic groups often based on sessions | Nice mainly compares peers within a group; grouping can dominate shares |
| `CONFIG_SCHED_CLASS_EXT` | In mainline 6.12; depends on BPF_SYSCALL, BPF_JIT and DEBUG_INFO_BTF there | Needs a loaded scheduler to take effect; inspect state/ops and exact BPF scheduler version |

PREEMPT_RT reached mainline support in 6.12 for supported architectures; older
RT builds commonly use a matching -rt patch series. Do not combine an arbitrary
patch with an unrelated kernel or assume a vendor kernel's support from its
release number. Most IRQs become threaded, but exceptions and hard-IRQ/raw-lock
sections remain. FIFO/RR already exist on ordinary Linux without PREEMPT_RT.

`NO_HZ_FULL` does not mean "no interrupts". A CPU with multiple runnable tasks,
CPU-time timers or tracing requirements may still need ticks. RCU callbacks,
unbound workqueues, timers and device IRQs need deliberate placement. This lab's
CPU-clock measurements themselves are part of the measurement configuration.

Do not disable the system's RT throttling to make a chart look better. Report
`sched_rt_runtime_us` / `sched_rt_period_us` as found. A quota or throttling event
is evidence about the environment, not a reason to silently alter it.

## 4. Hardware and architecture: measure the actual machine

| Platform | Record and test | Avoid |
|---|---|---|
| x86-64 Intel/AMD | Actual model/microcode, sockets, NUMA, SMT siblings, cache sharing, frequency/thermal policy, firmware/SMI sensitivity | Assuming all logical CPUs are independent identical cores |
| Hybrid x86 systems | Which cores are performance/efficiency types, available capacity/topology hints, scheduler placement and power policy | Treating core type as implied by CPU number or every Intel/AMD processor as hybrid |
| arm64 | Board/SoC, CPU implementer/part IDs, homogeneous versus heterogeneous clusters, capacities, shared cache/memory and firmware | Calling every arm64 chip "big.LITTLE"; an SBC and a server have very different constraints |
| RISC-V | Board, ISA/hart identity, clocksource, SBI/firmware, timer implementation and kernel support on that board | Inferring latency, PREEMPT_RT support or timer quality from the ISA name alone |
| Virtual machine / container | Host scheduling, vCPU pinning if known, CPU quotas, exposed features, steal/interference; namespaces | Assuming root in a container grants host scheduling privileges or that QEMU timings represent hardware |

The inspector uses `/proc/cpuinfo` and sysfs, not an x86-only instruction. It
records effective allowed CPUs, physical package/core IDs, thread siblings,
visible NUMA node membership, capacity and frequency/governor hints. Some fields
are unavailable by design on some systems. Frequency readings are snapshots,
not the actual frequency of every measured instruction. Thermal files may be
absent, permission-restricted or board-specific; milli-degrees are common but
check the interface.

Use `lscpu`, `lscpu -e`, `numactl --hardware`, and
`cat /sys/devices/system/clocksource/clocksource0/current_clocksource` as optional
read-only corroboration. Tool availability and output vary; the lab doesn't need
them. Keep raw identities rather than normalizing away differences.

Suggested paired placement study:

1. Audio and background on the same logical CPU.
2. Different SMT siblings of the same physical core, if any.
3. Distinct physical cores sharing a last-level cache.
4. Distinct clusters/sockets or NUMA nodes, with explicit memory placement.
5. Different capacity-core types, when present, with identical work.

Hold worker count and work constant within a pair. Then run the worker-count
experiment separately. More runnable workers can worsen latency through cache
eviction, memory bandwidth, interconnect contention or shared thermal limits even
without competing on the same logical CPU. Record first-touch policy before
attributing a NUMA result to "the scheduler".

Kernel comparison **on one machine** and hardware comparison **across machines**
are different experiments. Cross-machine results necessarily include CPU,
memory, firmware, drivers and often toolchain differences; do not claim a pure
architecture effect.

## 5. Optional advanced tracks, not automatic changes

**IRQ/dependency chain:** identify the audio/device IRQ and relevant threads;
read `/proc/interrupts`, visible `/proc/irq/*/smp_affinity_list`, and `ps -eLo
pid,tid,cls,rtprio,pri,ni,psr,comm`. On RT kernels inspect threaded IRQ priorities.
A callback waiting on a lower-priority IRQ/helper can suffer even at priority 99.
Trace the chain before any administrator changes priorities or routing.

**Isolation:** compare ordinary affinity with an explicitly configured partition
on a separate boot, recording cpuset partitions, `isolcpus`, `nohz_full`,
`rcu_nocbs`, housekeeping CPUs and IRQ routes. Reserve enough housekeeping
capacity. Some boot parameters (notably legacy domain isolation) are difficult
to undo without reboot. The lab only records these values.

**Power/thermal/uclamp:** establish steady temperatures and a fixed power source,
record governor/driver/EPP, then compare an administrator-selected alternate
policy as a separate experiment. `schedutil`, Intel/AMD pstate modes and
heterogeneous SoC policies differ. `uclamp.min/max` guide utilization-sensitive
decisions; neither is `cpu.max`, a guaranteed clock, or a hard RT reservation.
Tight latency can trade battery life and thermals for responsiveness.

**sched_ext:** record `CONFIG_SCHED_CLASS_EXT`, BPF requirements,
`/sys/kernel/sched_ext/state`, visible `root/ops`, and the BPF scheduler build.
Configuration alone does not activate it. Depending on switch-partial flags,
even tasks whose reported policy is OTHER/BATCH/IDLE may be scheduled by
sched_ext. Therefore requested/effective policy alone does not prove which fair
implementation ran. Loading/removing a BPF scheduler is explicitly outside this
lab's automation; on a managed test boot, reuse fairness/mixed and compare.

**Native versus emulated evidence:** native x86-64/arm64 CI tests validate output
and API behavior, not hardware latency. RISC-V/arm64 cross jobs only compile.
QEMU user/system emulation, if you add it, is a separate correctness technique;
label its results emulated, never RT measurements on that ISA.

## References

- [Kernel real-time differences](https://docs.kernel.org/core-api/real-time/differences.html)
- [NO_HZ and RCU implications](https://docs.kernel.org/timers/no_hz.html)
- [Kernel parameters](https://docs.kernel.org/admin-guide/kernel-parameters.html)
- [CPU isolation](https://docs.kernel.org/admin-guide/kernel-per-CPU-kthreads.html)
- [CFS bandwidth](https://docs.kernel.org/scheduler/sched-bwc.html)
- [RT group scheduling](https://docs.kernel.org/scheduler/sched-rt-group.html)
- [Utilization clamping](https://docs.kernel.org/scheduler/sched-util-clamp.html)
- [Energy-aware scheduling](https://docs.kernel.org/scheduler/sched-energy.html)
- [CPU performance scaling](https://docs.kernel.org/admin-guide/pm/cpufreq.html)
- [sched_ext](https://docs.kernel.org/scheduler/sched-ext.html)
- [RISC-V hardware probing](https://docs.kernel.org/arch/riscv/hwprobe.html)
- [CPU topology](https://docs.kernel.org/admin-guide/cputopology.html)
