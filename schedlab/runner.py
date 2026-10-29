"""Bounded subprocess orchestration. RT measurement always stays in native code."""

import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from . import __version__
from .cgroups import Unavailable, cpu_groups
from .environment import capture, memory_available_mb, read, smt_siblings, system_load
from .metrics import summarize
from .report import write_report

ROOT = Path(__file__).resolve().parent.parent
# A CMakePresets.json build directory; point elsewhere to use another compiler/preset.
NATIVE_DIR = Path(os.environ.get("SCHEDLAB_NATIVE_DIR") or ROOT / "build/default").resolve()
BINARY = NATIVE_DIR / "sched-work"
LOAD_BINARY = NATIVE_DIR / "sched-load"
PRESETS = (
    "quick",
    "offline",
    "fairness",
    "audio",
    "video",
    "mixed",
    "affinity",
    "rt",
    "sharing",
    "deadline",
    "locks",
    "memory",
    "cgroup",
    "load-audio",
    "load-offline",
    "load-placement",
    "load-priority",
    "load-external",
)
# Background load kinds that need no privilege; "rt" is added only with --allow-rt.
LOAD_KINDS = ("cpu", "wakeups", "bursts", "memory", "fork", "pipe", "futex", "io")
PREWARM_MS = 200
# sched-load options a generator spec may set beyond kind, CPUs, policy and nice.
GENERATOR_OPTIONS = ("threads", "priority", "memory_mb", "io_mb", "io_mb_per_s", "duty_percent")


def terminate_group(process):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.25)
    # The guardian may have exited while a descendant still owns a pipe.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=2)


def load_args(load_binary, generator, output, prefix, repeat, allow_rt):
    """The sched-load command for a generator spec; it runs until stopped or 10 s at most."""
    args = [
        str(load_binary),
        "--kind",
        generator["kind"],
        "--cpus",
        ",".join(map(str, generator["cpus"])),
        "--policy",
        generator.get("policy", "other"),
        "--nice",
        str(generator.get("nice", 0)),
        "--max-duration-ms",
        "10000",
        "--seed",
        str(repeat),
        "--output",
        str(output / prefix),
    ]
    for key in GENERATOR_OPTIONS:
        if key in generator:
            args.extend(["--" + key.replace("_", "-"), str(generator[key])])
    if allow_rt and generator["kind"] == "rt":
        args.append("--allow-rt")
    return args


def parse_native(stdout, returncode):
    """A native program's one JSON object, checked against its exit status."""
    core = json.loads(stdout)
    if not isinstance(core, dict) or core.get("schema_version") != 1:
        raise ValueError("unexpected native result schema")
    state = core["status"]
    if state not in ("ok", "skipped", "error"):
        raise ValueError("unknown native status")
    if (state == "ok" and returncode != 0) or (state == "skipped" and returncode != 77):
        raise ValueError("exit status contradicts result")
    return core


def start_generators(generators, output, repeat, allow_rt, load_binary):
    started = []
    for spec in generators:
        generator = spec["generator"]
        prefix = f"{spec['case']}-r{repeat}"
        args = load_args(load_binary, generator, output, prefix, repeat, allow_rt)
        # "own" gives the load its own session and therefore its own autogroup, like a build
        # started from another terminal; "same" keeps it in the lab's session.
        session = (
            {"start_new_session": True} if generator.get("session") == "own" else {"preexec_fn": os.setpgrp}
        )
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT, **session
        )
        started.append((process, spec, prefix, args))
    return started


def stop_generators(started, output, repeat):
    """SIGTERM each generator, collect what it achieved, and note any that ended on its own."""
    records = []
    for process, spec, prefix, args in started:
        ended_early = process.poll() is not None
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        record = {
            "case": spec["case"],
            "label": prefix,
            "repetition": repeat,
            "role": "load",
            "parameters": spec,
            "command": args,
        }
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            terminate_group(process)
            stdout, stderr = process.communicate(timeout=2)
            record.update(status="error", reason="load generator ignored SIGTERM; process group killed")
            records.append(record)
            continue
        record.update(returncode=process.returncode, stderr=stderr)
        (output / (prefix + ".stdout")).write_text(stdout)
        try:
            core = parse_native(stdout, process.returncode)
            record.update(status=core["status"], core=core)
            if core["status"] != "ok":
                record["reason"] = core["reason"]
            elif ended_early:
                record.update(status="error", reason="load generator stopped before the measurement ended")
        except (ValueError, KeyError, TypeError) as exc:
            record.update(status="error", reason=f"invalid/incomplete load result: {exc}", stdout=stdout)
        records.append(record)
        for leftover in output.glob(prefix + ".load-io-*.tmp"):
            leftover.unlink(missing_ok=True)
    return records


def run_group(
    specs, output, repeat, allow_rt=False, binary=BINARY, groups=None, timeout=18, load_binary=LOAD_BINARY
):
    # A group runs its measured specs concurrently; generator specs add background load around
    # them, and skip specs record a case that cannot run here.
    group_cases = [spec["case"] for spec in specs]
    results = [
        {
            "case": spec["case"],
            "label": spec["case"],
            "repetition": repeat,
            "status": "skipped",
            "reason": spec["skip"],
        }
        for spec in specs
        if "skip" in spec
    ]
    measured = [spec for spec in specs if "skip" not in spec and "generator" not in spec]
    generators = [spec for spec in specs if "generator" in spec]
    if not measured:
        return results
    processes = []
    started = []
    measured_results = []
    start = time.monotonic_ns() + 800_000_000
    deadline = time.monotonic() + timeout
    try:
        # Generators start first, so the load is steady before the common release.
        started = start_generators(generators, output, repeat, allow_rt, load_binary)
        for index, spec in enumerate(measured):
            prefix = f"{spec['case']}-r{repeat}"
            options = {k: v for k, v in spec.items() if k != "case"}
            args = [str(binary), "--output", str(output / prefix), "--start-ns", str(start)]
            for key, value in options.items():
                args.extend(["--" + key.replace("_", "-"), str(value)])
            if allow_rt:
                args.append("--allow-rt")
            if groups:
                args = [sys.executable, "-m", "schedlab.exec", str(groups[index]), *args]
            # Single-threaded launcher: separate process groups, but preserve the session/autogroup.
            # setsid() would give every contender a separate autogroup and can defeat nice comparisons.
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=os.setpgrp,
                cwd=ROOT,
            )
            processes.append((process, spec, prefix, args))
        for process, spec, prefix, args in processes:
            record = {
                "case": spec["case"],
                "label": prefix,
                "repetition": repeat,
                "parameters": spec,
                "command": args,
                "artifact_prefix": prefix,
                "group_cases": group_cases,
            }
            if generators:
                record["load_case"] = generators[0]["case"]
            try:
                stdout, stderr = process.communicate(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                terminate_group(process)
                stdout, stderr = process.communicate(timeout=2)
                record.update(
                    status="error",
                    reason="harness wall timeout; process group killed",
                    stdout=stdout,
                    stderr=stderr,
                )
                measured_results.append(record)
                continue
            record.update(returncode=process.returncode, stderr=stderr)
            (output / (prefix + ".stdout")).write_text(stdout)
            try:
                core = parse_native(stdout, process.returncode)
                record.update(status=core["status"], core=core)
                if core["status"] == "ok":
                    if core["effective"]["policy"] != spec.get("policy", "other"):
                        raise ValueError("requested/effective policy mismatch")
                    record["metrics"] = summarize(output / (prefix + ".csv"), core)
                else:
                    record["reason"] = core["reason"]
            except (ValueError, KeyError, TypeError, OSError) as exc:
                record.update(
                    status="error", reason=f"invalid/incomplete native result: {exc}", stdout=stdout
                )
            measured_results.append(record)
        load_results = stop_generators(started, output, repeat)
        started = []
    finally:
        for process, _, _, _ in processes + started:
            terminate_group(process)
    # A measurement only counts as "under load" if every generator covered it.
    failed = next((load for load in load_results if load["status"] != "ok"), None) if generators else None
    for record in measured_results:
        if failed and record["status"] == "ok":
            status = "skipped" if failed["status"] == "skipped" else "error"
            record.update(status=status, reason=f"load generator {failed['status']}: {failed['reason']}")
            record.pop("metrics", None)
    return results + measured_results + (load_results if generators else [])


def cases(preset, cpu, other_cpu, allowed=(), siblings=(), allow_rt=False, memory_mb=256):
    def offline(name, **kwargs):
        return dict(case=name, mode="offline", cpu=cpu, jobs=80, work=8, **kwargs)

    def periodic(name, mode="audio", **kwargs):
        return dict(case=name, mode=mode, cpu=cpu, jobs=30, work=8, **kwargs)

    if preset.startswith("load-"):
        cpus = sorted(allowed) or sorted({cpu, other_cpu})

        # Comparisons against an idle baseline need the same starting clock, so every measured
        # workload busies its CPU for 200 ms before its first release.
        def warm_offline(name, **kwargs):
            return offline(name, prewarm_ms=PREWARM_MS, **kwargs)

        def warm_periodic(name, **kwargs):
            return periodic(name, prewarm_ms=PREWARM_MS, **kwargs)

        return load_cases(preset, cpu, cpus, siblings, allow_rt, memory_mb, warm_offline, warm_periodic)
    if preset == "quick":
        return [[offline("offline")], [periodic("audio")], [periodic("video", mode="video")]]
    if preset == "offline":
        return [[offline("offline-" + p, policy=p)] for p in ("other", "batch", "idle")]
    if preset == "fairness":
        pairs = (("other", 0), ("other", 10), ("batch", 0), ("idle", 0))
        return [
            [
                offline(f"fair-{p}-{nice}-control", policy="other", duration_ms=500),
                offline(f"fair-{p}-{nice}-candidate", policy=p, nice=nice, duration_ms=500),
            ]
            for p, nice in pairs
        ]
    if preset in ("audio", "video"):
        return [
            [periodic(preset + "-alone", mode=preset)],
            [periodic(preset + "-contended", mode=preset), offline(preset + "-background", duration_ms=1100)],
        ]
    if preset == "mixed":
        return [
            [periodic("mixed-audio-" + p), offline("mixed-background-" + p, policy=p, duration_ms=600)]
            for p in ("other", "batch", "idle")
        ]
    if preset == "affinity":
        separate = periodic("affinity-separated-audio")
        background = offline("affinity-separated-background", duration_ms=600)
        background["cpu"] = other_cpu
        return [
            [periodic("affinity-shared-audio"), offline("affinity-shared-background", duration_ms=600)],
            [separate, background],
            [
                periodic("affinity-two-workers-audio"),
                offline("affinity-two-workers-a", duration_ms=600),
                offline("affinity-two-workers-b", duration_ms=600),
            ],
        ]
    if preset == "rt":
        return [
            [periodic("rt-audio-" + p, policy=p, priority=10), offline("rt-background-" + p, duration_ms=600)]
            for p in ("other", "fifo", "rr")
        ]
    if preset == "sharing":
        groups = []
        for p in ("fifo", "rr"):
            group = []
            for index in range(2):
                group.append(
                    dict(
                        case=f"sharing-{p}-{index}",
                        mode="offline",
                        cpu=cpu,
                        jobs=600,
                        work=16,
                        policy=p,
                        priority=10,
                        timeout_ms=10000,
                    )
                )
            groups.append(group)
        return groups
    if preset == "deadline":
        # No affinity argument: kernel admission may require the entire root scheduling domain.
        return [
            [
                dict(
                    case="deadline-audio",
                    mode="audio",
                    jobs=30,
                    work=8,
                    policy="deadline",
                    runtime_us=1000,
                    deadline_us=10000,
                    period_us=10000,
                )
            ]
        ]
    if preset == "locks":
        return [
            [
                dict(
                    case="lock-" + protocol,
                    mode="lock",
                    cpu=cpu,
                    policy="fifo",
                    priority=10,
                    work=4,
                    protocol=protocol,
                )
            ]
            for protocol in ("none", "inherit")
        ]
    if preset == "memory":
        return [[periodic("memory-" + memory, memory=memory)] for memory in ("demand", "prefault", "mlock")]
    raise ValueError("unknown ordinary preset: " + preset)


def load_cases(preset, cpu, allowed, siblings, allow_rt, memory_mb, offline, periodic):
    """Measured workloads against sched-load scenarios; each preset includes its idle baselines."""
    sibling = sorted(set(siblings) - {cpu})
    placements = {
        "same": [cpu],  # the measured CPU itself: scheduling competition
        "sibling": sibling,  # its SMT sibling only: a shared core, no shared runqueue
        "others": [other for other in allowed if other != cpu and other not in sibling],  # other cores
        "all": list(allowed),  # everywhere, like an unconstrained parallel build
    }

    def loaded(measured, kind, placement, baseline, **generator):
        """[measured, generator] for one scenario, or a skip when it cannot run here."""
        cpus = placements[placement]
        if not cpus:
            return [
                dict(case=measured["case"], skip=f"no CPUs for the '{placement}' placement on this machine")
            ]
        if kind == "rt" and not allow_rt:
            return [dict(case=measured["case"], skip="RT background load needs --allow-rt")]
        if kind == "memory":
            generator.setdefault("memory_mb", memory_mb)
        if kind == "rt":
            generator.update(policy="fifo", priority=5)
        generator.setdefault("session", "own")
        spec = dict(kind=kind, placement=placement, cpus=cpus, baseline=baseline, **generator)
        return [measured, dict(case=measured["case"] + "-load", generator=spec)]

    if preset == "load-audio":
        groups = []
        for policy in ("other", "fifo"):
            if policy == "fifo" and not allow_rt:
                groups.append([dict(case="audio-fifo", skip="FIFO audio cases need --allow-rt")])
                continue
            rt = dict(policy="fifo", priority=10) if policy == "fifo" else {}
            baseline = f"audio-{policy}-idle"
            groups.append([periodic(baseline, **rt)])
            for kind in (*LOAD_KINDS, "rt"):
                # RT load stays off the measured CPU, so it never starves the measured task.
                placement = "others" if kind == "rt" else "all"
                groups.append(loaded(periodic(f"audio-{policy}-{kind}", **rt), kind, placement, baseline))
        return groups
    if preset == "load-offline":
        groups = [[offline("offline-idle", duration_ms=600)]]
        for kind in LOAD_KINDS:
            groups.append(loaded(offline(f"offline-{kind}", duration_ms=600), kind, "all", "offline-idle"))
        return groups
    if preset == "load-placement":
        groups = [[periodic("audio-idle")], [offline("offline-idle", duration_ms=600)]]
        for kind in ("cpu", "memory"):
            for placement in ("same", "sibling", "others", "all"):
                groups.append(loaded(periodic(f"audio-{kind}-{placement}"), kind, placement, "audio-idle"))
                measured = offline(f"offline-{kind}-{placement}", duration_ms=600)
                groups.append(loaded(measured, kind, placement, "offline-idle"))
        return groups
    if preset == "load-priority":
        # One CPU-bound thread on the measured CPU, at each priority, in the lab's session or its own.
        groups = [[periodic("audio-idle")], [offline("offline-idle", duration_ms=600)]]
        for label, policy, nice in (
            ("nice0", "other", 0),
            ("nice19", "other", 19),
            ("batch", "batch", 0),
            ("idle", "idle", 0),
        ):
            for session in ("same", "own"):
                scenario = dict(policy=policy, nice=nice, session=session, threads=1)
                measured = offline(f"offline-{label}-{session}", duration_ms=600)
                groups.append(loaded(measured, "cpu", "same", "offline-idle", **scenario))
                groups.append(
                    loaded(periodic(f"audio-{label}-{session}"), "cpu", "same", "audio-idle", **scenario)
                )
        return groups
    if preset == "load-external":
        # No generator: run while your own workload (a build, a game, a browser) is active.
        groups = [[periodic("audio-other")], [offline("offline", duration_ms=600)]]
        if allow_rt:
            groups.append([periodic("audio-fifo", policy="fifo", priority=10)])
        else:
            groups.append([dict(case="audio-fifo", skip="FIFO audio cases need --allow-rt")])
        return groups
    raise ValueError("unknown load preset: " + preset)


def run(preset, output, repetitions=3, cpu=None, allow_rt=False, parent=None, label=None):
    if not BINARY.is_file():
        raise ValueError(
            f"native binary missing: {BINARY}; build it with "
            "'cmake --preset default && cmake --build --preset default' "
            "(or 'uv run poe build'), or set SCHEDLAB_NATIVE_DIR"
        )
    allowed = sorted(os.sched_getaffinity(0))
    cpu = allowed[0] if cpu is None else cpu
    if cpu not in allowed:
        raise ValueError("--cpu must belong to the effective allowed CPU mask")
    other_cpu = next((candidate for candidate in allowed if candidate != cpu), cpu)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "lab_version": __version__,
        "preset": preset,
        "status": "running",
        "title": label or preset,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "repetitions": repetitions,
        "allow_rt": allow_rt,
        "selected_cpu": cpu,
        "comparison_parameters": {"preset": preset, "repetitions": repetitions, "cpu": cpu},
        "environment": capture(BINARY),
        "runs": [],
        "method": "Warm up native code; preallocate; common future release per group; "
        "alternate group order each repetition; preserve per-run distributions.",
    }
    try:
        if preset == "affinity" and len(allowed) < 2:
            manifest["runs"].append(
                {
                    "case": "affinity",
                    "label": "affinity",
                    "status": "skipped",
                    "reason": "at least two allowed CPUs required",
                }
            )
        elif preset == "cgroup":
            if parent is None:
                raise Unavailable("pass --cgroup-parent with an explicitly delegated v2 subtree")
            for repetition in range(1, repetitions + 1):
                for kind, controls in (
                    ("weight", [{"cpu.weight": "100"}, {"cpu.weight": "400"}]),
                    ("quota", [{"cpu.max": "max 100000"}, {"cpu.max": "20000 100000"}]),
                ):
                    with cpu_groups(parent, controls) as groups:
                        specs = [
                            dict(
                                case=f"cgroup-{kind}-{index}",
                                mode="offline",
                                cpu=cpu,
                                duration_ms=600,
                                work=8,
                            )
                            for index in range(2)
                        ]
                        results = run_group(specs, output, repetition, groups=groups)
                        for result, group, control in zip(results, groups, controls, strict=True):
                            result["cgroup"] = {
                                "requested": control,
                                "effective": {name: read(group / name) for name in control},
                                "cpu_stat_after": read(group / "cpu.stat"),
                                "cpu_pressure_after": read(group / "cpu.pressure"),
                            }
                        manifest["runs"].extend(results)
        else:
            if preset.startswith("load-") and preset != "load-external" and not LOAD_BINARY.is_file():
                raise ValueError(f"load generator missing: {LOAD_BINARY}; rebuild the native programs")
            # Memory load stays well inside what the machine has free.
            available = memory_available_mb()
            memory_mb = max(16, min(256, available // 10)) if available else 64
            groups = cases(preset, cpu, other_cpu, allowed, smt_siblings(cpu), allow_rt, memory_mb)
            manifest["load_settings"] = {"memory_mb": memory_mb, "smt_siblings": smt_siblings(cpu)}
            for repetition in range(1, repetitions + 1):
                ordered = groups if repetition % 2 else list(reversed(groups))
                for group in ordered:
                    manifest["runs"].extend(run_group(group, output, repetition, allow_rt))
    except Unavailable as exc:
        manifest["runs"].append({"case": preset, "label": preset, "status": "skipped", "reason": str(exc)})
    except (OSError, ValueError) as exc:
        manifest["runs"].append({"case": preset, "label": preset, "status": "error", "reason": str(exc)})
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        raise
    finally:
        if manifest["status"] != "interrupted":
            states = [result["status"] for result in manifest["runs"]]
            manifest["status"] = "error" if "error" in states else ("ok" if "ok" in states else "skipped")
        manifest["environment_after"] = {
            "cgroup": capture_counters(manifest["environment"]),
            "thermal": {path: read(path) for path in manifest["environment"]["thermal"]},
            "load": system_load(),
        }
        (output / "run.json").write_text(json.dumps(manifest, indent=2) + "\n")
        write_report(manifest, output / "report.html")
    return manifest


def capture_counters(environment):
    return [
        {
            "path": item["path"],
            "cpu.stat": read(Path(item["path"]) / "cpu.stat"),
            "memory.events": read(Path(item["path"]) / "memory.events"),
        }
        for item in environment["cgroup"]["ancestors"]
    ]
