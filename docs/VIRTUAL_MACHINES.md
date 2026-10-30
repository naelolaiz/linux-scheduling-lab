# Kernels and scenarios in virtual machines

`schedlab vm` runs the unchanged presets inside QEMU/KVM guests. Each guest boots a
pinned distribution kernel or a kernel.org release built with options you choose,
with the boot parameters, CPU topology, host interference and sched_ext scheduler
of the experiment. It answers questions that would otherwise need reboots, a
second machine or root on the host. Which preemption model gives these audio jobs
the shortest tail? What does `nohz_full` change? How does a guest fare when the
host is busy? The host's scheduler settings, bootloader and kernel are never
touched.

A guest is a measurement of the **guest**: each vCPU is an ordinary thread on the
host, so timings include host scheduling and virtualization exits. Compare VM runs
with each other, under the same host conditions, and not with bare-metal results.
[Kernel and hardware comparisons](KERNELS_AND_HARDWARE.md) on real boots remain the
reference for absolute latency.

## 1. Requirements and what is stored where

- **podman** and **KVM**: `/dev/kvm` must be usable by your user, usually through the
  `kvm` group. Containers run rootless; the lab passes your groups into them
  (`--group-add keep-groups`) so the guest can use KVM. x86-64 hosts only.
- **Images**, built from [`vm/Containerfile`](../vm/Containerfile) on first use and
  then reused from podman's cache: `localhost/schedlab-vm` (QEMU plus the guest,
  about 300 MB) and, only when something is built from source,
  `localhost/schedlab-vm-builder`.
- **The store**, a podman volume named `schedlab-vm`: each kernel's image and
  config (15–25 MB per kernel; the packages they come from are deleted after
  extraction), kernel.org tarballs used for builds (about 150 MB each) and
  sched_ext schedulers, built or [downloaded](#published-builds). Set
  `SCHEDLAB_VM_STORE=/some/directory` to use a directory instead; CI does this to
  cache it.
- **Results** are ordinary run directories, so `schedlab analyze`, `report` and
  `compare` work unchanged. Each also has a `vm/` directory: `vm.json` (kernel
  record, boot parameters, QEMU version and arguments, topology, pinning, host
  load), `guest.json` (what PID 1 did), `console.log` and the job's own log.

Nothing is written to the checkout or your home directory. To remove everything:
`podman volume rm schedlab-vm` and
`podman image rm localhost/schedlab-vm localhost/schedlab-vm-builder`.

**Sources:** [`podman-run(1)`](https://docs.podman.io/en/latest/markdown/podman-run.1.html)
for `--device` and `--group-add keep-groups`;
[`podman-volume(1)`](https://docs.podman.io/en/latest/markdown/podman-volume.1.html);
[KVM](https://docs.kernel.org/virt/kvm/index.html).

## 2. First run

```sh
python3 -m schedlab vm kernels
python3 -m schedlab vm run quick --kernel fedora-44 --repetitions 1 --out results/vm-quick
python3 -m schedlab analyze results/vm-quick
```

`uv run poe vm …` is the same command. The first run builds the image and downloads
the kernel; later runs start in a few seconds. The guest's root filesystem is a
single initramfs: Alpine, Python, a static build of the native programs, the lab,
the job, and the kernel's config at `/boot/config-<release>` so the manifest
records it. The kernel boots in under a second without modules, disks or network.
PID 1 ([`schedlab/vm/guest.py`](../schedlab/vm/guest.py)) mounts `/proc`, `/sys`,
cgroup v2 and debugfs, runs the preset as root, and sends the results back as one
checksummed frame on the second serial port. Then the guest powers off. Root lets
the manifest record the active preemption model from debugfs.

**Sources:** [QEMU system emulation invocation](https://www.qemu.org/docs/master/system/invocation.html),
[direct Linux boot](https://www.qemu.org/docs/master/system/linuxboot.html),
[initramfs](https://docs.kernel.org/filesystems/ramfs-rootfs-initramfs.html).

## 3. Kernels

| Name | Kernel | HZ | What it is for |
|---|---|--:|---|
| `ubuntu-5.4` | Ubuntu 20.04, 5.4.0-216 | 250 | The lab's baseline: CFS before EEVDF, voluntary preemption, no runtime switch |
| `debian-6.1` | Debian 12, 6.1.190 | 250 | The last CFS long-term kernel; `preempt=none/voluntary/full` at boot |
| `rocky-9`, `rocky-9-rt` | Rocky 9.8, 5.14.0-687 (and its `kernel-rt`) | 1000 | An enterprise kernel: a 5.14 base with years of backports; the RT build has lazy preemption |
| `debian-6.12` | Debian 13, 6.12.113 | 250 | EEVDF; `preempt=` at boot |
| `debian-6.12-rt` | Debian 13, 6.12.113 PREEMPT_RT | 250 | The first mainline release with PREEMPT_RT |
| `rocky-10`, `rocky-10-rt` | Rocky 10.2, 6.12.0-211 (and its `kernel-rt`) | 1000 | The same base as Debian's 6.12 with enterprise backports, including a newer sched_ext |
| `debian-7.2` | Debian 13 backports, 7.2.11 | 250 | Lazy preemption by default, `preempt=` at boot |
| `debian-7.2-rt` | Debian 13 backports, 7.2.11 PREEMPT_RT | 250 | A current RT kernel |
| `fedora-44` | Fedora 44, 7.2.13 | 1000 | The default: lazy preemption, `preempt=` at boot, sched_ext, steal-time accounting |

Each package is pinned by SHA-256 with a fallback URL that keeps it after its
distribution moves on: snapshot.debian.org, Launchpad, Fedora's Koji build system
(which keeps the unsigned build, so both digests are pinned) and Rocky's vault.
These are the distributions' own builds, with their configs and patches.

**sched_ext** needs a kernel with `CONFIG_SCHED_CLASS_EXT` and a BPF scheduler
whose kfunc declarations match the kernel's BTF. The lab uses the kernel's
example schedulers (`scx_simple`, `scx_qmap`, `scx_central` from
`tools/sched_ext`), downloaded as [published builds](#published-builds) or
built on first use. For Fedora 44 they come from the matching kernel.org
release; for Rocky 10 they come from Rocky's own patched source package,
because Rocky's 6.12 carries a backported sched_ext that no kernel.org release
matches. The Debian 7.2 kernels enable sched_ext, but no scheduler can load on
them. Their BTF was generated with pahole 1.30, which leaves the implicit
argument of 7.x kfuncs such as `scx_bpf_select_cpu_dfl` in the public prototype;
pahole 1.31 or newer is needed. The lab's builder compiles pahole 1.32 for the same
reason. On Rocky's RT kernel `scx_qmap` is refused (`bpf_timer cannot be used for
PREEMPT_RT`), while `scx_simple` runs.

**Source kernels** are kernel.org releases (7.2.13, 7.2.11 and 6.18.59 are
pinned; 7.2.11 matches the Debian 7.2 kernels, so the lab's config can be compared
with Debian's) built with the options their names spell:

```text
linux-VERSION[-none|-voluntary|-full|-lazy|-rt][-hz100|-hz250|-hz300|-hz1000][-nohzfull][-scx]
```

```sh
python3 -m schedlab vm build linux-7.2.13-hz1000
python3 -m schedlab vm run audio --kernel linux-7.2.13-rt-hz1000 --out results/vm-rt-hz1000
```

A build starts from the x86-64 `defconfig` and the kernel's own `kvm_guest.config`.
It adds what the lab reads (config at `/proc/config.gz`, schedstats, PSI, steal-time
accounting, cgroup CPU controls), then applies the named options and checks that
`make olddefconfig` kept every one of them. An option this release cannot provide
stops the build with an explicit message, so nothing is silently substituted. A
named preemption model is fixed at build time (`PREEMPT_DYNAMIC` off). A build takes
5–15 minutes and a few GB of temporary space inside the builder container. `-scx`
adds BTF debug information, which roughly doubles both. `vm run`, `matrix` and `test`
download a missing kernel when it is [published](#published-builds) and build it
otherwise; `vm build` always builds from source, ahead of time.

**Sources:** [kernel.org releases](https://www.kernel.org/category/releases.html),
[kernel build configuration](https://docs.kernel.org/kbuild/kconfig.html),
[preemption models](https://docs.kernel.org/core-api/real-time/differences.html),
[snapshot.debian.org](https://snapshot.debian.org/).

### Published builds

Builds take minutes to hours, so the lab publishes the ones its matrices need as
assets of its
[`kernels` release](https://github.com/naelolaiz/linux-scheduling-lab/releases/tag/kernels):
the three `timer-hz` kernels, `linux-7.2.13-scx`, and the sched_ext schedulers for
`fedora-44`, `rocky-10` and `rocky-10-rt`. `vm run`, `matrix`, `test` and `fetch`
download these instead of building them, check each against the SHA-256 pinned in
`PREBUILT` in [`catalog.py`](../schedlab/vm/catalog.py), and build from source when
the download fails. `vm kernels` lists them. A downloaded build's record names the
asset it came from (`prebuilt`), and so does `vm/vm.json` in every run that boots
it.

The builds are reproducible: the same source, options and builder packages give
the same bytes on any machine. Instead of the build machine's time, user and host,
a kernel is stamped with its source's release date, a fixed user and host, and
build number 1. Every build runs in the same directory, so paths in debug and BPF
information match, and the record holds no times. To check a published build
yourself:

```sh
python3 -m schedlab vm verify linux-7.2.13-hz1000
```

This rebuilds it in the builder container, packs it the way the release does and
compares the result with the pinned SHA-256. When they differ, it lists the record
fields that differ from the published build's, such as the image's SHA-256 or a
package version. The record lists the toolchain: the builder's gcc, binutils,
clang, LLVM, musl, busybox, Python and xz packages, and pahole. Alpine updates
packages within a release branch, so a rebuild months later can differ only
because a tool changed, and the record shows which one.

The [kernels workflow](../.github/workflows/kernels.yml) rebuilds each pinned build
on a GitHub-hosted runner and publishes it only if it reproduces the pinned
SHA-256, which comes from a separate `vm verify` run. Every published asset was
therefore built twice, on different machines, with identical results. Each asset
also has a build provenance attestation that names the workflow run and the commit
that built it:

```sh
gh attestation verify linux-7.2.13-hz1000-<digest>.tar.xz --repo naelolaiz/linux-scheduling-lab
```

The release also carries the corresponding source, as the GPL asks of anyone who
distributes kernel binaries. That means the kernel source tarballs the builds used
(kernel.org releases, and Rocky's own tarball for its schedulers), each build's
config inside its asset, and `schedlab-<commit>.tar.gz`, the lab at the commit
that published them. Its `vm/Containerfile` and `schedlab/vm/build.py` are the
build recipe.

**Changing a published build.** A new pinned release, build option or builder
package changes a build's digest. `vm verify NAME` prints the new SHA-256, which
goes into `PREBUILT`. Pull requests that change the recipe (`build.py`,
`vm/Containerfile` or the workflow) rebuild every published build in CI and fail
when one no longer reproduces its pin. Running the workflow by hand publishes the
pinned builds that the release lacks. An asset's name carries the first 16 hex
digits of its digest, so a new build never replaces one that an older checkout
pins.

**Sources:** [reproducible kernel builds](https://docs.kernel.org/kbuild/reproducible-builds.html),
[`SOURCE_DATE_EPOCH`](https://reproducible-builds.org/specs/source-date-epoch/),
[artifact attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations),
[releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases),
[GPL version 2, section 3](https://www.gnu.org/licenses/old-licenses/gpl-2.0.html#section3).

## 4. Matrices

A matrix runs one preset in each VM of a named set, then prints the analysis of
all of them, including a same-case comparison that names what differs (kernel,
preemption model, HZ, VM configuration):

```sh
python3 -m schedlab vm matrix preemption audio --out results/vm-preemption
python3 -m schedlab vm matrix kernels fairness --out results/vm-kernels
```

| Matrix | VMs | Try it with | Look for |
|---|---|---|---|
| `kernels` | 5.4, 6.1, 6.12 and 7.2, default boot | `fairness`, `mixed` | CFS versus EEVDF shares and wake-up latency |
| `distributions` | Rocky 9 and 10 against Debian 6.12; Debian and Fedora 7.2 | `audio`, `fairness` | What vendor configs (HZ 250 or 1000) and backports change at the same base release |
| `preemption` | Rocky 10 with `preempt=none`, `voluntary`, `full`, `lazy`, then Rocky 10 PREEMPT_RT | `audio`, `load-audio` | Response tails under kernel-heavy load (`fork`, `pipe`, `io`) |
| `rt` | Rocky 9 and 10, Debian 6.12 and 7.2, each ordinary and PREEMPT_RT | `rt --allow-rt`, `locks --allow-rt` | FIFO tails and priority inheritance on RT kernels |
| `isolation` | Measured CPU 2 shared, with `nohz_full`, and with `isolcpus` | `audio`, `load-audio` | Whether isolation removes the other tasks' wait and ticks |
| `topology` | 4 cores, 2 SMT cores, 2 NUMA nodes | `load-placement`, `affinity` | How the scheduler places work on emulated SMT and NUMA |
| `neighbour` | Pinned vCPUs: quiet host, then host CPU or wake-up load on the same CPUs | `quick`, `audio` | Steal time: host load the guest's scheduler cannot see |
| `sched_ext` | Fedora 44's fair scheduler, then the `scx_simple` and `scx_qmap` BPF schedulers | `fairness`, `mixed` | What replacing the fair class does to shares and tails |
| `timer-hz` | One release built with HZ 100, 250 and 1000 | `audio`, `fairness` | Tick-rate effects on wake-ups and time slices |

Matrix entries run one after another under the same host conditions; repeat the
whole matrix, and keep the host idle, before reading small differences.

Which `preempt=` models a kernel offers depends on its release and architecture:
7.x kernels on x86 keep only `full` and `lazy` and ignore `none` and `voluntary`,
which is why the `preemption` matrix uses Rocky 10, a 6.12 kernel that offers all
four. Every run records the model that actually ran (from debugfs), and the
analysis warns when it differs from the one requested.

## 5. Options for a single VM

| Option | Meaning |
|---|---|
| `--kernel NAME` | A catalog or source kernel (default `fedora-44`) |
| `--boot "..."` | Extra kernel parameters, e.g. `preempt=full`, `nohz_full=2-3 rcu_nocbs=2-3`, `threadirqs` |
| `--smp N`, `--threads 2`, `--numa N` | vCPU count, SMT threads per core, and NUMA nodes (local distance 10, remote 20) |
| `--memory MiB` | Guest memory (default 2048) |
| `--cpu N` | The measured guest CPU; the job may use every online CPU, including ones `isolcpus` removed from init |
| `--pin auto` or `--pin 4-7` | One host CPU per vCPU, set on QEMU's vCPU threads. `auto` takes whole cores from the top of the allowed set, both hardware threads of a core when the guest has SMT |
| `--neighbour KIND` | Host-side `sched-load` (`cpu`, `wakeups`, `bursts`, `memory`) on the pinned CPUs while the guest runs; needs `--pin` |
| `--scx NAME` | Run the job under a sched_ext example scheduler (`scx_simple`, `scx_qmap`, `scx_central`) on `fedora-44`, `rocky-10`, `rocky-10-rt` or a `-scx` source kernel |
| `--accel tcg` | Software emulation when KVM is unavailable: checks behaviour, timings are meaningless |
| `--allow-rt` | RT inside the guest. The guest's FIFO tasks can starve only the guest; its vCPUs stay ordinary host threads |

**Sources:** [QEMU `-smp` and `-numa`](https://www.qemu.org/docs/master/system/invocation.html),
[QMP `query-cpus-fast`](https://www.qemu.org/docs/master/interop/qemu-qmp-ref.html),
[kernel parameters](https://docs.kernel.org/admin-guide/kernel-parameters.html),
[NO_HZ](https://docs.kernel.org/timers/no_hz.html),
[sched_ext](https://docs.kernel.org/scheduler/sched-ext.html).

## 6. A root sandbox for the opt-in checks

```sh
python3 -m schedlab vm test --kernel debian-7.2-rt
```

This runs the whole test suite as root in a guest, with the RT, DEADLINE and
delegated-cgroup opt-in checks enabled (`SCHEDLAB_TEST_RT=1`,
`SCHEDLAB_TEST_DEADLINE=1` and a `cpu`-enabled subtree of the guest's cgroup root).
It needs neither sudo nor RT limits on the host, unlike the container recipes in
[advanced experiments](ADVANCED.md#opt-in-checks-in-containers), which test the
host kernel itself. The log is `vm/test.log`.

## 7. Reading VM results

The analysis adds a `vm:` line to each run and these caveats:

- **vCPUs are host threads.** Unpinned vCPUs can move between host CPUs and SMT
  siblings during a run. `--pin` fixes each vCPU to one host CPU, but the host
  still runs its own work there.
- **Steal time** is the share of the measured vCPU's time the host gave to
  something else. The contention column shows it as `steal N%`, separately from
  other guest tasks (`others`). In the `neighbour` matrix it is the finding;
  elsewhere it is a caveat. The Debian and Ubuntu kernels lack
  `CONFIG_PARAVIRT_TIME_ACCOUNTING`, so they also count stolen time as the task's
  own CPU time; the analysis says so when it applies. Fedora, Rocky and source
  kernels keep it apart, which is why the `neighbour` matrix uses Fedora 44.
- **Host load** before and after the run is recorded, and a busy host is named the
  same way a busy machine is on bare metal.
- **Clocks:** guests use `kvm-clock`; there is no cpufreq in the guest, so the
  host's governor still shapes throughput.

What transfers to bare metal is the direction of a difference between kernels or
boot parameters measured the same way, such as a preemption model that shortens a
tail under kernel-heavy load. Absolute latencies, rare-tail bounds and anything
that depends on real devices or interrupts do not.

**Sources:** [`/proc/stat` steal time](https://man7.org/linux/man-pages/man5/proc_stat.5.html),
[KVM paravirtual clock](https://docs.kernel.org/virt/kvm/x86/msr.html),
and the lab's [VM implementation](../schedlab/vm/).
