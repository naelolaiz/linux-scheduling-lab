# Optional permissions, resource controls and hardware experiments

These are **manual, capability-gated tracks**, not part of the quickstart or CI.
Use noncritical equipment and inspect each tool's version and `--help`. Missing
hardware, permissions or kernel features means "not tested", not a simulated
success. The [tutorial](TUTORIAL.md) lists the automated coverage precisely.

## Permissions and safety

The default test suite and presets need no privilege. The program never calls
sudo, changes capabilities, installs packages, or writes global tuning files.
RT presets require `--allow-rt` even if you are root. FIFO/RR require a selected
worker CPU plus another allowed CPU for the guardian. DEADLINE retains its
original affinity and is limited to 20% requested utilization.

Inspect, without changing anything:

```sh
python3 -m schedlab doctor
ulimit -r
ulimit -l
ulimit -R
```

The `ulimit` flags above are Bash examples; another shell may use different flags.
`RLIMIT_RTPRIO` can allow bounded FIFO/RR priority increases; `RLIMIT_NICE`
controls permitted nice improvements; `RLIMIT_MEMLOCK` controls unprivileged
locked memory. `CAP_SYS_NICE` permits wider scheduling operations and DEADLINE
normally requires it. `CAP_IPC_LOCK` can bypass ordinary memlock limits. A
capability in a container/user namespace may not be sufficient for the host.
Seccomp, cgroups and kernel configuration add independent restrictions.

**API/command sources:** [`getrlimit(2)`](https://man7.org/linux/man-pages/man2/getrlimit.2.html)
defines RTPRIO/NICE/MEMLOCK/RTTIME and their units/semantics;
[`capabilities(7)`](https://man7.org/linux/man-pages/man7/capabilities.7.html)
defines the separate capability checks; [Bash's `ulimit` builtin](https://www.gnu.org/software/bash/manual/html_node/Bash-Builtins.html)
documents these shell flags. For DEADLINE-specific requirements, use
[`sched_setattr(2)`](https://man7.org/linux/man-pages/man2/sched_setattr.2.html),
not a FIFO-only permission recipe.

For Gentoo/OpenRC with PAM, an administrator can create a **dedicated lab group**
and use that group's limits in `/etc/security/limits.d/`. A narrowly scoped
example configuration, to review rather than apply automatically:

```text
@schedlab  -  rtprio   20
@schedlab  -  memlock  65536
```

Memlock is in KiB here. The login path must actually invoke `pam_limits.so`;
check Gentoo's PAM/pambase configuration. Existing sessions do not inherit a
new login's limits. An OpenRC system is not required to run systemd-logind.
Do not grant all users priority 99 or unlimited memory just for these demos.

**Gentoo/PAM sources:** [`pam_limits(8)`](https://man7.org/linux/man-pages/man8/pam_limits.8.html)
and [`limits.conf(5)`](https://man7.org/linux/man-pages/man5/limits.conf.5.html)
describe session application, group rules and KiB memlock units.
[Gentoo PAM](https://wiki.gentoo.org/wiki/PAM) and
[OpenRC integration](https://wiki.gentoo.org/wiki/OpenRC) describe the distribution
context; verify the actual login stack rather than assuming a service manager
installs or applies the limits.

Setting capabilities on the whole Python interpreter is particularly broad and
is **not recommended**. If an administrator authorizes a dedicated native binary,
review its code and file ownership, record the capabilities (`getcap` if available),
and remove them after the experiment. Rebuilding replaces a capability-bearing
binary; do not assume permissions survived. Running the harness as root is not
required and increases the impact of mistakes.

The native guardian and Python watchdog are independent layers. RT limits are
never raised beyond inherited hard limits. A watchdog is not protection against
a kernel lockup, bad firmware or a hostile external RT task starving its CPU.
Do not run RT experiments on equipment controlling a critical service/device.

**Safety implementation/further reading:** the [native guardian](../native/work.cpp)
and [harness process-group cleanup](../schedlab/runner.py) define this lab's
bounds. [`kill(2)`](https://man7.org/linux/man-pages/man2/kill.2.html) and
[`waitpid(2)`](https://man7.org/linux/man-pages/man2/waitpid.2.html) describe signal
delivery and reaping; neither is a promise of recovery from a kernel/firmware hang.

## Delegated cgroups without assuming systemd

Cgroup v2 is a kernel interface; OpenRC and containers can use it without systemd.
An administrator must provide a **delegated subtree** that is writable by the
learner, with the intended controllers already enabled for children. Creating
a writable directory alone is not sufficient: the cgroup migration permission
check includes the relevant common ancestor's `cgroup.procs`. The "no internal
process" and controller/domain rules also apply.

Check the delegation read-only:

```sh
PARENT=/path/to/your/delegated/subtree
cat "$PARENT/cgroup.controllers"
cat "$PARENT/cgroup.subtree_control"
cat "$PARENT/cgroup.type"
```

Do not paste commands that enable controllers at `/sys/fs/cgroup` globally.
The CPU preset creates only owned leaves, writes their CPU controls, records
counters and removes those leaves after children exit. It leaves the parent's
controller state unchanged.

For the following manual experiments, use a **new leaf for each trial** and do
not reuse a leaf occupied by another process. This shell pattern is deliberately
bounded and never recursively deletes a cgroup:

```sh
# Set PARENT to your existing delegation first.
LEAF=$(mktemp -d "$PARENT/schedlab-manual.XXXXXX")
trap 'rmdir -- "$LEAF"' EXIT
```

If `rmdir` fails, inspect `cgroup.procs` and child directories; do not delete or
kill anything unrelated to the trial. A real error must be investigated, not
hidden behind `|| true`.

**Sources and further reading:** [cgroup v2 delegation](https://docs.kernel.org/admin-guide/cgroup-v2.html#delegation)
and [controller rules](https://docs.kernel.org/admin-guide/cgroup-v2.html#controlling-controllers)
cover migration permissions, domain constraints and controller enablement.
[OpenRC's cgroup support](https://wiki.gentoo.org/wiki/OpenRC#CGroups_support)
is an init-specific integration option, not a prerequisite for the kernel API.
The [lab helper](../schedlab/cgroups.py) shows exactly which owned leaves it creates
and which parent settings it deliberately does not change.

### Memory: pressure is not only an OOM limit

Requires `memory` already enabled for children. In your new leaf:

```sh
printf '25165824\n' > "$LEAF/memory.high"
printf '67108864\n' > "$LEAF/memory.max"
mkdir -p results/memory-control
python3 -m schedlab.exec "$LEAF" "$PWD/build/sched-work" \
  --mode audio --jobs 30 --memory demand --output results/memory-control/probe \
  > results/memory-control/native.json
cat "$LEAF/memory.events"
cat "$LEAF/memory.current"
```

The example's 24 MiB high / 64 MiB max are **trial values**, not recommended system
defaults. Repeat with `memory.high=max` in a fresh leaf using identical work.
Record both counters and latency. It is valid to observe no pressure if this
small workload fits below the threshold.

`memory.high` drives reclaim/throttling; `memory.max` is a hard boundary that can
lead to a **cgroup-local OOM**. A low limit may kill the worker/guardian, which is a
failed experiment, not a scheduler skip. Locked pages cannot simply be reclaimed
to satisfy pressure. Do not add host-wide swap stress or drop page caches.

**Sources:** [v2 memory interfaces/events](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files)
for `memory.high`, `memory.max` and local OOM behavior;
[pressure stall information](https://docs.kernel.org/accounting/psi.html)
for pressure measurements; [`mlock(2)`](https://man7.org/linux/man-pages/man2/mlock.2.html)
for locked-memory behavior. Compare counters with the unchanged native work,
not just the configured limit.

### Placement: cpuset CPU and NUMA-node eligibility

Requires `cpuset` enabled, writable `cpuset.cpus` and `cpuset.mems`, and a topology
you understand. Read the parent's effective sets:

```sh
cat "$PARENT/cpuset.cpus.effective"
cat "$PARENT/cpuset.mems.effective"
```

In a new leaf, set `cpuset.mems` to an allowed node list and `cpuset.cpus` to an
allowed CPU list **before moving the task**. Example placeholders:

```sh
# CPU_LIST and NODE_LIST must be selected from the effective parent sets.
printf '%s\n' "$NODE_LIST" > "$LEAF/cpuset.mems"
printf '%s\n' "$CPU_LIST" > "$LEAF/cpuset.cpus"
cat "$LEAF/cpuset.cpus.effective"
cat "$LEAF/cpuset.mems.effective"
```

Launch a non-RT native run through `python3 -m schedlab.exec` as above. Compare
first-touch placement on one node with a deliberately selected alternative.
Optional `numactl --hardware` and `numastat -p PID` help inspect allocation.
`numactl --membind=NODE` can fail under container security rules even when CPU
affinity works; record that failure. Eligibility is not exclusivity:
cpuset partitions, housekeeping and IRQ placement are separate administrator work.

**Sources and tools:** [v2 cpuset interfaces/partitions](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpuset)
define effective CPU/node sets; [`numactl(8)`](https://man7.org/linux/man-pages/man8/numactl.8.html)
documents placement policies; [`numastat(8)`](https://man7.org/linux/man-pages/man8/numastat.8.html)
documents allocation counters. [NUMA memory policy](https://docs.kernel.org/admin-guide/mm/numa_memory_policy.html)
explains placement semantics; topology and policy availability are platform-specific.

### I/O: ionice, writeback and actual storage

This track needs optional `fio` and util-linux `ionice`, a disposable directory on
a suitable filesystem, and space for two 32 MiB files. It writes regular files,
**never a raw device**. Do not run it on latency-critical/shared storage.

```sh
mkdir -p results/io
fio --name=baseline --filename=results/io/baseline.bin --size=32m \
  --rw=write --bs=64k --ioengine=sync --direct=0 --end_fsync=1 \
  --output-format=json --output=results/io/baseline.json
ionice -c 3 fio --name=idle --filename=results/io/idle.bin --size=32m \
  --rw=write --bs=64k --ioengine=sync --direct=0 --end_fsync=1 \
  --output-format=json --output=results/io/idle.json
```

This alone is a no-contention control; repeat paired trials with a bounded
competitor on the **same physical device** to investigate shares. I/O priority
depends on the block scheduler/device path. Inspect
`/sys/block/DEVICE/queue/scheduler` after correctly identifying the backing
device (`findmnt -T results/io`, `lsblk`); do not infer it from a filename.

**Command sources:** [fio's options and statistics](https://fio.readthedocs.io/en/latest/fio_doc.html),
[`ionice(1)`](https://man7.org/linux/man-pages/man1/ionice.1.html),
[`findmnt(8)`](https://man7.org/linux/man-pages/man8/findmnt.8.html), and
[`lsblk(8)`](https://man7.org/linux/man-pages/man8/lsblk.8.html).
In particular, fio documents the `size`, `direct`, `end_fsync` and JSON-output
options used here; check the installed fio version before extending the command.

Buffered writes may mostly measure page-cache acceptance until writeback/fsync.
CPU scheduling, syscall latency, queue depth, filesystem writeback and device
completion are different quantities. Retain fio's completion/sync statistics.
Direct I/O, when supported, is a **separate** experiment with alignment/cache
semantics; do not compare it against buffered I/O and attribute the difference
only to priority.

For a delegated `io` controller, a new leaf's `io.max` accepts a device major:minor
plus limits such as `wbps=10485760`; `io.weight` controls supported weighted
service. Select the correct device and controller mechanism first. Example
format, not a device selection:

```text
MAJOR:MINOR wbps=10485760
```

Record `io.stat`, `io.pressure` and ancestor limits. `io.weight` depends on a
supporting mechanism such as BFQ; `io.max` uses block throttling. Buffered
writeback attribution also depends on filesystem/cgroup support. A setting's
presence does not prove that the particular storage path honored it.
Cleanup removes only the two named trial files and the owned empty leaf.

**Controller/storage sources:** [cgroup I/O interfaces and writeback attribution](https://docs.kernel.org/admin-guide/cgroup-v2.html#io)
and [BFQ's group-scheduling requirements](https://docs.kernel.org/block/bfq-iosched.html).
For the buffering distinction, see [`fsync(2)`](https://man7.org/linux/man-pages/man2/fsync.2.html)
and the `O_DIRECT` discussion in [`open(2)`](https://man7.org/linux/man-pages/man2/open.2.html);
filesystem/device support and alignment requirements vary.

## Real audio and video

### Audio: measure a real XRUN, do not rename a synthetic miss

The native WAV is suitable as a signal source, but listening to it does not
exercise the measured scheduling path. On a prepared audio machine:

1. Record interface/driver, USB/PCI topology, sample rate, buffer/period sizes,
   server/ALSA configuration and kernel/environment manifest.
2. Use the actual application's callback timing and XRUN counter. JACK exposes
   XRUN callbacks/logs; PipeWire's `pw-top` displays errors and graph timings.
   ALSA applications should record errors such as `-EPIPE` and recovery events.
   Check the meaning of your tool's counter rather than assuming every "error"
   is an identical hardware underrun.
3. Run a fixed-length recording or playback, then repeat with the same bounded
   offline competitor and changed scheduling policy/placement.
4. Record callbacks, device/server XRUN counts and elapsed test length together.
   Missing callback deadlines and device XRUNs can be correlated, not equated.
5. Restore only changes you explicitly made; keep recovery and audio outputs safe.

Optional ALSA `speaker-test`, JACK, PipeWire and application-specific tools are
not bundled. They may play audible sound; choose low volume and a safe output.
The lab never opens a microphone or audio device automatically.

**Device/API sources:** [ALSA PCM states, `-EPIPE` and recovery](https://www.alsa-project.org/alsa-doc/alsa-lib/pcm.html),
[JACK process/XRUN callback registration](https://jackaudio.org/api/group__ClientCallbacks.html),
and [PipeWire `pw-top` counters](https://docs.pipewire.org/page_man_pw-top_1.html).
The [`speaker-test` manual](https://manpages.debian.org/bookworm/alsa-utils/speaker-test.1.en.html)
describes that optional sound-producing tool. These sources define actual
device/server observations; the synthetic lab does not implement those APIs.

### Video: optional real codec comparison

With ffmpeg installed, generate a small finite input and limit encoder threads:

```sh
mkdir -p results/codec
ffmpeg -nostdin -f lavfi -i testsrc2=size=320x180:rate=30 \
  -t 2 -threads 1 -c:v mpeg4 -q:v 5 -y results/codec/control.avi
nice -n 10 ffmpeg -nostdin -f lavfi -i testsrc2=size=320x180:rate=30 \
  -t 2 -threads 1 -c:v mpeg4 -q:v 5 -y results/codec/nice10.avi
```

The commands overwrite only these named artifacts. Preserve ffmpeg's timing,
codec/build version and frame count, and add an equivalent same-CPU competitor
for an actual nice comparison. Encoder speed/quality settings must match.
This is a manual application experiment, not another native preset.

**Sources and further reading:** [ffmpeg command-line options](https://ffmpeg.org/ffmpeg.html),
[generated video sources including `testsrc2`](https://ffmpeg.org/ffmpeg-filters.html),
[codec/thread options](https://ffmpeg.org/ffmpeg-codecs.html), and
[GNU `nice`](https://www.gnu.org/software/coreutils/manual/html_node/nice-invocation.html).
Build-enabled codecs and their threading capabilities differ; the linked codec
documentation does not imply every installed encoder supports every option.

## cyclictest and rtla: locating kernel latency

These optional tools complement the workloads; they do not measure their DSP.
On an authorized noncritical test host, after selecting a worker CPU and keeping
another CPU for housekeeping, a bounded single-thread cyclictest example is:

```sh
# CPU is one effective allowed CPU selected using the environment inventory.
cyclictest --affinity="$CPU" --threads=1 -p 10 --interval=1000 \
  --duration=5 --quiet
```

Check your rt-tests version's help first; privileges, memory-lock defaults and
option behavior vary. A periodic sleeper can help measure dispatch/timer latency
but does not perform equivalent audio work. Report the duration and sample count
with its histogram/maximum, not just "max latency".

`rtla timerlat` / `rtla osnoise` can distinguish IRQ/thread latency or sources of
interference on kernels with the relevant tracing support. Inspect
`rtla timerlat top --help` and select a short duration and explicit CPU set.
These tracing tools may create kernel threads/change tracing state and need
privilege; they are **not automatically executed**. Keep the tool's saved trace
and configuration, and stop it cleanly. Do not infer firmware or IRQ attribution
from the workload's wall-time spike alone.

**Sources and further reading:** the [Debian bookworm `cyclictest(8)` manual](https://manpages.debian.org/bookworm/rt-tests/cyclictest.8.en.html)
documents the short priority option `-p`, affinity, duration and histogram options;
the example uses an unsuffixed five-second duration. The
[rtla timerlat](https://docs.kernel.org/tools/rtla/rtla-timerlat.html) and
[rtla osnoise](https://docs.kernel.org/tools/rtla/rtla-osnoise.html) manuals describe
their distinct measurements and required tracing support. Check the installed
rt-tests/rtla version and running kernel rather than mixing options from different releases.

## Optional systemd integration (not required)

On a system that actually uses a user systemd manager, an administrator/user
may choose a transient scope as an alternative interface to cgroup controls:

```sh
systemd-run --user --scope -p CPUWeight=100 \
  python3 -m schedlab run offline --repetitions 1 --out results/systemd-weight
systemd-run --user --scope -p CPUQuota=20% \
  python3 -m schedlab run offline --repetitions 1 --out results/systemd-quota
```

This caps/groups the **whole harness and its descendants**, unlike the delegated
CPU preset's separate sibling groups. User-manager/controller delegation may be
unavailable. The service manager is merely one frontend; do not present these
commands as OpenRC requirements or as FIFO/RR bandwidth controls.

Do not place `CPUSchedulingPolicy=fifo` on the entire harness. Its safety guardian
must begin as OTHER; only the native worker deliberately enters RT. RT limits can
be supplied through service settings on systems using that frontend, but that is
an administration decision, not a dependency of this project.

**Sources:** [`systemd-run`](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)
for user scopes; [`systemd.resource-control`](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)
for CPUWeight, CPUQuota and delegation; [`systemd.exec`](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
for scheduling/limit settings. These are current upstream manuals: use the
installed version's manual for property availability. The underlying
[v2 CPU controller contract](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpu)
still determines which scheduling classes a control affects.

## Further references

- [cgroup v2 delegation and controllers](https://docs.kernel.org/admin-guide/cgroup-v2.html)
- [capabilities(7)](https://man7.org/linux/man-pages/man7/capabilities.7.html)
- [pam_limits(8)](https://man7.org/linux/man-pages/man8/pam_limits.8.html)
- [ionice(1)](https://man7.org/linux/man-pages/man1/ionice.1.html)
- [BFQ](https://docs.kernel.org/block/bfq-iosched.html)
- [rtla timerlat](https://docs.kernel.org/tools/rtla/rtla-timerlat.html)
- [rtla osnoise](https://docs.kernel.org/tools/rtla/rtla-osnoise.html)
- [ALSA PCM API](https://www.alsa-project.org/alsa-doc/alsa-lib/pcm.html)
- [PipeWire pw-top](https://docs.pipewire.org/page_man_pw-top_1.html)
