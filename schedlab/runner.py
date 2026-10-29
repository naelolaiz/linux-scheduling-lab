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
from .environment import capture, read, system_load
from .metrics import summarize
from .report import write_report

ROOT = Path(__file__).resolve().parent.parent
# A CMakePresets.json build directory; point elsewhere to use another compiler/preset.
NATIVE_DIR = Path(os.environ.get("SCHEDLAB_NATIVE_DIR") or ROOT / "build/default").resolve()
BINARY = NATIVE_DIR / "sched-work"
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
)


def terminate_group(process):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.25)
    # The guardian may have exited while a descendant still owns a pipe.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=2)


def run_group(specs, output, repeat, allow_rt=False, binary=BINARY, groups=None, timeout=18):
    processes = []
    results = []
    start = time.monotonic_ns() + 800_000_000
    deadline = time.monotonic() + timeout
    try:
        for index, spec in enumerate(specs):
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
            }
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
                results.append(record)
                continue
            record.update(returncode=process.returncode, stderr=stderr)
            (output / (prefix + ".stdout")).write_text(stdout)
            try:
                core = json.loads(stdout)
                if not isinstance(core, dict) or core.get("schema_version") != 1:
                    raise ValueError("unexpected native result schema")
                state = core["status"]
                if state not in ("ok", "skipped", "error"):
                    raise ValueError("unknown native status")
                if (state == "ok" and process.returncode != 0) or (
                    state == "skipped" and process.returncode != 77
                ):
                    raise ValueError("exit status contradicts result")
                record.update(status=state, core=core)
                if state == "ok":
                    if core["effective"]["policy"] != spec.get("policy", "other"):
                        raise ValueError("requested/effective policy mismatch")
                    record["metrics"] = summarize(output / (prefix + ".csv"), core)
                else:
                    record["reason"] = core["reason"]
            except (ValueError, KeyError, TypeError, OSError) as exc:
                record.update(
                    status="error", reason=f"invalid/incomplete native result: {exc}", stdout=stdout
                )
            results.append(record)
    finally:
        for process, _, _, _ in processes:
            terminate_group(process)
    return results


def cases(preset, cpu, other_cpu):
    def offline(name, **kwargs):
        return dict(case=name, mode="offline", cpu=cpu, jobs=80, work=8, **kwargs)

    def periodic(name, mode="audio", **kwargs):
        return dict(case=name, mode=mode, cpu=cpu, jobs=30, work=8, **kwargs)

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
            groups = cases(preset, cpu, other_cpu)
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
