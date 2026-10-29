"""Read-only, init-system-independent provenance. Missing data stays explicit."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time

CONFIG_KEYS = [
    "CONFIG_PREEMPT_NONE",
    "CONFIG_PREEMPT_VOLUNTARY",
    "CONFIG_PREEMPT",
    "CONFIG_PREEMPT_DYNAMIC",
    "CONFIG_PREEMPT_RT",
    "CONFIG_PREEMPT_LAZY",
    "CONFIG_HIGH_RES_TIMERS",
    "CONFIG_HZ",
    "CONFIG_HZ_100",
    "CONFIG_HZ_250",
    "CONFIG_HZ_300",
    "CONFIG_HZ_1000",
    "CONFIG_NO_HZ_IDLE",
    "CONFIG_NO_HZ_FULL",
    "CONFIG_RCU_NOCB_CPU",
    "CONFIG_CPU_ISOLATION",
    "CONFIG_SMP",
    "CONFIG_SCHED_SMT",
    "CONFIG_SCHED_MC",
    "CONFIG_SCHED_CLUSTER",
    "CONFIG_NUMA",
    "CONFIG_NUMA_BALANCING",
    "CONFIG_CGROUPS",
    "CONFIG_CGROUP_SCHED",
    "CONFIG_FAIR_GROUP_SCHED",
    "CONFIG_RT_GROUP_SCHED",
    "CONFIG_CFS_BANDWIDTH",
    "CONFIG_CPUSETS",
    "CONFIG_MEMCG",
    "CONFIG_BLK_CGROUP",
    "CONFIG_CGROUP_WRITEBACK",
    "CONFIG_IRQ_FORCED_THREADING",
    "CONFIG_RT_MUTEXES",
    "CONFIG_UCLAMP_TASK",
    "CONFIG_UCLAMP_TASK_GROUP",
    "CONFIG_ENERGY_MODEL",
    "CONFIG_CPU_FREQ",
    "CONFIG_CPU_FREQ_GOV_SCHEDUTIL",
    "CONFIG_SCHED_DEBUG",
    "CONFIG_SCHEDSTATS",
    "CONFIG_PSI",
    "CONFIG_SCHED_CLASS_EXT",
    "CONFIG_BPF",
    "CONFIG_BPF_SYSCALL",
    "CONFIG_BPF_JIT",
    "CONFIG_DEBUG_INFO_BTF",
    "CONFIG_POSIX_TIMERS",
    "CONFIG_IKCONFIG",
    "CONFIG_IKCONFIG_PROC",
    "CONFIG_SCHED_AUTOGROUP",
    "CONFIG_HZ_PERIODIC",
    "CONFIG_BFQ_GROUP_IOSCHED",
    "CONFIG_BLK_DEV_THROTTLING",
]


def read(path):
    try:
        return {"status": "ok", "value": Path(path).read_text().strip()}
    except (OSError, UnicodeError) as exc:
        return {"status": "unavailable", "reason": str(exc)}


def decode_mount_path(value):
    for old, new in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(old, new)
    return value


def kernel_config(release):
    attempts = []
    for path in (
        Path("/proc/config.gz"),
        Path("/boot") / ("config-" + release),
        Path("/lib/modules") / release / "build/.config",
    ):
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt") as stream:
                    text = stream.read()
            else:
                text = path.read_text()
        except (OSError, UnicodeError, EOFError) as exc:
            attempts.append({"path": str(path), "reason": str(exc)})
            continue
        values = {}
        for line in text.splitlines():
            if line.startswith("CONFIG_") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
            elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
                values[line.split()[1]] = "n"
        return {
            "status": "ok",
            "source": str(path),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "options": {key: values.get(key, "unknown/not in this config") for key in CONFIG_KEYS},
            "caution": "Installed build configs can be stale; verify against this boot.",
        }
    return {"status": "unavailable", "attempts": attempts, "options": dict.fromkeys(CONFIG_KEYS, "unknown")}


def unified_cgroup():
    membership = read("/proc/self/cgroup")
    mounts = read("/proc/self/mountinfo")
    result = {"membership": membership, "mountinfo": mounts, "ancestors": []}
    if membership["status"] != "ok" or mounts["status"] != "ok":
        return result
    paths = [line[3:] for line in membership["value"].splitlines() if line.startswith("0::")]
    if not paths:
        result["status"] = "unavailable"
        result["reason"] = "No unified cgroup v2 membership"
        return result
    for line in mounts["value"].splitlines():
        left, right = line.split(" - ", 1)
        if right.split()[0] != "cgroup2":
            continue
        fields = left.split()
        mount = Path(decode_mount_path(fields[4]))
        root = Path(decode_mount_path(fields[3]))
        try:
            relative = Path(paths[0]).relative_to(root)
        except ValueError:
            continue
        current = mount / relative
        if ".." in current.parts:
            continue
        result.update(status="ok", path=str(current), mount=str(mount))
        while True:
            names = (
                "cpu.max",
                "cpu.weight",
                "cpu.stat",
                "cpu.pressure",
                "cpuset.cpus.effective",
                "cpuset.mems.effective",
                "memory.high",
                "memory.max",
                "memory.events",
                "io.max",
                "io.weight",
                "cgroup.controllers",
                "cgroup.subtree_control",
            )
            result["ancestors"].append(
                {"path": str(current), "limits": {name: read(current / name) for name in names}}
            )
            if current == mount:
                return result
            current = current.parent
    result.update(status="unavailable", reason="Cgroup path is hidden by mount/cgroup namespace")
    return result


def parse_cpu_list(text):
    """Kernel CPU list syntax ("0-3,8") -> sorted CPU numbers; malformed parts are ignored."""
    cpus = set()
    for part in str(text).strip().split(","):
        low, _, high = part.partition("-")
        if low.isdigit() and (not high or high.isdigit()):
            cpus.update(range(int(low), int(high or low) + 1))
    return sorted(cpus)


def smt_siblings(cpu):
    """Other hardware threads of `cpu`'s core, empty without SMT or topology information."""
    siblings = read(Path("/sys/devices/system/cpu") / f"cpu{cpu}" / "topology/thread_siblings_list")
    return [other for other in parse_cpu_list(siblings.get("value", "")) if other != cpu]


def memory_available_mb():
    """MemAvailable from /proc/meminfo in MiB, or None."""
    meminfo = read("/proc/meminfo")
    for line in meminfo.get("value", "").splitlines():
        name, _, value = line.partition(":")
        if name == "MemAvailable" and value.split() and value.split()[0].isdigit():
            return int(value.split()[0]) // 1024
    return None


def system_load():
    """Load average (with the instantaneous runnable/total task count) and CPU pressure stall info."""
    return {"loadavg": read("/proc/loadavg"), "cpu_pressure": read("/proc/pressure/cpu")}


def native_build(binary):
    """Compiler, flags, sanitizers and libraries that the native binary reports for its own build."""
    try:
        completed = subprocess.run(
            [str(binary), "--build-info"], capture_output=True, text=True, timeout=5, check=False
        )
        data = json.loads(completed.stdout)
        if completed.returncode == 0 and data.get("status") == "ok" and isinstance(data.get("build"), dict):
            return data["build"]
        reason = completed.stderr.strip() or "unexpected --build-info result"
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as exc:
        reason = str(exc)
    return {"status": "unavailable", "reason": reason}


def capture(binary=None):
    release = platform.release()
    allowed = sorted(os.sched_getaffinity(0))
    cpu_info = read("/proc/cpuinfo")
    identities = []
    if cpu_info["status"] == "ok":
        for block in cpu_info["value"].split("\n\n"):
            fields = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
            fields = {k.strip(): v.strip() for k, v in fields.items()}
            identities.append(
                {
                    k: fields[k]
                    for k in (
                        "processor",
                        "vendor_id",
                        "model name",
                        "CPU implementer",
                        "CPU architecture",
                        "CPU variant",
                        "CPU part",
                        "CPU revision",
                        "isa",
                        "uarch",
                        "mmu",
                        "hart",
                    )
                    if k in fields
                }
            )
    cpus = {}
    for cpu in allowed:
        base = Path("/sys/devices/system/cpu") / f"cpu{cpu}"
        files = (
            "topology/physical_package_id",
            "topology/die_id",
            "topology/core_id",
            "topology/thread_siblings_list",
            "topology/core_cpus_list",
            "topology/cluster_id",
            "topology/core_type",
            "cpu_capacity",
            "cpufreq/scaling_driver",
            "cpufreq/scaling_governor",
            "cpufreq/scaling_cur_freq",
            "cpufreq/cpuinfo_max_freq",
            "cpufreq/cpuinfo_min_freq",
            "cpufreq/energy_performance_preference",
        )
        cpus[str(cpu)] = {name: read(base / name) for name in files}
        cpus[str(cpu)]["numa_nodes"] = [node.name for node in sorted(base.glob("node[0-9]*"))]
        cpus[str(cpu)]["caches"] = [
            {name: read(cache / name) for name in ("level", "type", "size", "shared_cpu_list")}
            for cache in sorted((base / "cache").glob("index[0-9]*"))
        ]
    limits = {}
    for name in ("RLIMIT_RTPRIO", "RLIMIT_RTTIME", "RLIMIT_MEMLOCK", "RLIMIT_NICE"):
        value = getattr(resource, name, None)
        limits[name] = list(resource.getrlimit(value)) if value is not None else "unavailable"
    result = {
        "system": {
            "kernel": release,
            "version": platform.version(),
            "architecture": platform.machine(),
            "hostname": platform.node(),
            "python": sys.version,
            "libc_hint": platform.libc_ver(),
            "os_release": read("/etc/os-release"),
        },
        "allowed_cpus": allowed,
        "cpu_identity": identities or cpu_info,
        "cpu_topology": cpus,
        "limits": limits,
        "uid": os.getuid(),
        "clock": {
            "name": "CLOCK_MONOTONIC",
            "python_implementation": time.get_clock_info("monotonic").implementation,
            "resolution_seconds": time.get_clock_info("monotonic").resolution,
        },
        "process_status": read("/proc/self/status"),
        "kernel_config": kernel_config(release),
        "runtime": {
            path: read(path)
            for path in (
                "/proc/cmdline",
                "/sys/kernel/realtime",
                "/sys/kernel/debug/sched/preempt",
                "/sys/devices/system/cpu/online",
                "/sys/devices/system/cpu/isolated",
                "/sys/devices/system/cpu/nohz_full",
                "/sys/devices/system/cpu/smt/active",
                "/sys/devices/system/clocksource/clocksource0/current_clocksource",
                "/sys/devices/system/clocksource/clocksource0/available_clocksource",
                "/proc/sys/kernel/sched_rt_runtime_us",
                "/proc/sys/kernel/sched_rt_period_us",
                "/proc/sys/kernel/sched_rr_timeslice_ms",
                "/proc/sys/kernel/sched_autogroup_enabled",
                "/proc/sys/kernel/numa_balancing",
                "/sys/kernel/sched_ext/state",
                "/sys/kernel/sched_ext/root/ops",
                "/sys/kernel/sched_ext/enable_seq",
                "/sys/hypervisor/type",
                "/sys/class/dmi/id/product_name",
                "/sys/firmware/devicetree/base/model",
            )
        },
        "thermal": {str(p): read(p) for p in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp"))},
        "numa_distances": {
            str(p): read(p) for p in sorted(Path("/sys/devices/system/node").glob("node*/distance"))
        },
        "cgroup": unified_cgroup(),
        "load": system_load(),
        "privacy": "Contains hostname, paths, command line and process metadata; review before sharing.",
    }
    if binary:
        result["binary_sha256"] = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
        result["native_build"] = native_build(binary)
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2, check=False
        )
        result["git_commit"] = completed.stdout.strip() if completed.returncode == 0 else "unavailable"
        state = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=2, check=False
        )
        result["git_worktree_status"] = state.stdout.strip() if state.returncode == 0 else "unavailable"
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["git_commit"] = {"status": "unavailable", "reason": str(exc)}
    return result
