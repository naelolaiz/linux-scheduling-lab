"""Per-case summaries, preset-aware findings and caveats from saved manifests.

Reads JSON only and runs nothing, so results from other machines and CI can be analyzed anywhere.
"""

import html
import json
from pathlib import Path
import statistics

# CFS/EEVDF load weights for nice -20..19, in the rows of kernel/sched/core.c sched_prio_to_weight.
# fmt: off
NICE_WEIGHTS = (
    88761, 71755, 56483, 46273, 36291,
    29154, 23254, 18705, 14949, 11916,
    9548, 7620, 6100, 4904, 3906,
    3121, 2501, 1991, 1586, 1277,
    1024, 820, 655, 526, 423,
    335, 272, 215, 172, 137,
    110, 87, 70, 56, 45,
    36, 29, 23, 18, 15,
)
# fmt: on
IDLE_WEIGHT = 3  # SCHED_IDLE tasks use WEIGHT_IDLEPRIO.

# Primary metric per workload mode: (value key, label, unit, which direction is better).
PRIMARY = {
    "audio": ("response_p99_ms", "response p99", "ms", "lower"),
    "video": ("response_p99_ms", "response p99", "ms", "lower"),
    "offline": ("throughput_jobs_s", "throughput", "jobs/s", "higher"),
    "lock": ("high_wait_ms", "high-priority wait", "ms", "lower"),
    "load": ("load_percent", "achieved load", "% of its CPUs", None),
}
OPTIMIZED_BUILDS = ("Release", "RelWithDebInfo", "MinSizeRel")
BAR_WIDTH = 24
EIGHTHS = " ▏▎▍▌▋▊▉"


def load_manifest(path):
    """A run directory or manifest path -> (manifest path, manifest)."""
    source = Path(path)
    if source.is_dir():
        source = source / "run.json"
    manifest = json.loads(source.read_text())
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError(f"unsupported manifest schema: {source}")
    return source, manifest


def build_summary(build):
    """One line naming the compiler, build type and any sanitizers of a native build."""
    if not isinstance(build, dict) or build.get("status") == "unavailable":
        return "unknown native build"
    compiler = (
        f"{build.get('compiler_id', 'compiler')} {build.get('compiler_version', build.get('compiler', ''))}"
    )
    parts = [compiler.strip(), build.get("build_type") or "no build type"]
    if build.get("sanitize"):
        parts.append(f"sanitize={build['sanitize']}")
    return " ".join(parts)


def load_weight(policy, nice):
    if policy == "idle":
        return IDLE_WEIGHT
    if isinstance(nice, int) and -20 <= nice <= 19:
        return NICE_WEIGHTS[nice + 20]
    return None


def _milliseconds(metrics, name, key):
    value = (metrics.get(name) or {}).get(key)
    return value / 1e6 if value is not None else None


def _share(part, whole):
    """part/whole clamped to [0, 1]; tick-sampled busy time can slightly exceed its window."""
    if part is None or not whole:
        return None
    return min(1.0, max(0.0, part / whole))


def contention_values(core):
    """Runqueue wait and who else used the measured CPU and its SMT siblings, from "contention"."""
    contention = core.get("contention") or {}
    if contention.get("status") != "ok":
        return {"wait_ms": None, "others_share": None, "sibling_busy_share": None}
    window = contention.get("window_ns")
    siblings = [_share(busy, window) for busy in contention.get("smt_siblings_busy_ns") or []]
    siblings = [share for share in siblings if share is not None]
    wait = contention.get("runqueue_wait_ns")
    return {
        "wait_ms": wait / 1e6 if wait is not None else None,
        "others_share": _share(contention.get("cpu_others_run_ns"), window),
        "sibling_busy_share": sum(siblings) / len(siblings) if siblings else None,
    }


def load_values(core):
    """What a sched-load generator achieved: CPU time (its own and its children's) per CPU it used,
    and for I/O load, which mostly blocks, the rate it wrote."""
    elapsed_ns = core.get("elapsed_ns") or 0
    capacity = elapsed_ns * len(core.get("cpus") or [])
    used = (core.get("cpu_ns") or 0) + (core.get("children_cpu_ns") or 0)
    work = core.get("work") or {}
    written = work.get("count") if work.get("unit") == "bytes_written" else None
    return {
        "load_percent": 100 * used / capacity if capacity else None,
        "write_mib_s": written / 2**20 / (elapsed_ns / 1e9) if written is not None and elapsed_ns else None,
    }


def run_values(run):
    """The comparable numbers of one successful run, in display units."""
    metrics = run.get("metrics") or {}
    core = run.get("core") or {}
    if run.get("role") == "load":
        return load_values(core)
    lock = core.get("lock") or {}
    fraction = metrics.get("miss_fraction")
    cpu_ns = metrics.get("cpu_total_ns")
    elapsed_ns = metrics.get("elapsed_ns")
    cpu_share = cpu_ns / elapsed_ns if cpu_ns is not None and elapsed_ns else None
    throughput = metrics.get("throughput_jobs_s")
    return {
        **contention_values(core),
        # Jobs per second of CPU time actually received: separates clock/SMT/cache speed from share.
        "jobs_per_cpu_s": throughput / cpu_share if throughput is not None and cpu_share else None,
        "response_p50_ms": _milliseconds(metrics, "response_ns", "p50"),
        "response_p99_ms": _milliseconds(metrics, "response_ns", "p99"),
        "response_max_ms": _milliseconds(metrics, "response_ns", "max"),
        "wake_p99_ms": _milliseconds(metrics, "wake_lateness_ns", "p99"),
        "wall_p99_ms": _milliseconds(metrics, "wall_processing_ns", "p99"),
        "miss_percent": fraction * 100 if fraction is not None else None,
        "throughput_jobs_s": metrics.get("throughput_jobs_s"),
        "cpu_ms": cpu_ns / 1e6 if cpu_ns is not None else None,
        # Fraction of one CPU the measured thread ran during its measurement window.
        "cpu_share": cpu_share,
        "minor_faults": core.get("minor_faults"),
        "involuntary_cs": core.get("involuntary_cs"),
        "high_wait_ms": lock["high_wait_ns"] / 1e6 if lock.get("high_wait_ns") is not None else None,
    }


def _mode(run):
    if run.get("role") == "load":
        return "load"
    core = run.get("core") or {}
    return str(core.get("mode") or (run.get("parameters") or {}).get("mode") or "unknown")


def _median(values):
    present = [value for value in values if value is not None]
    return statistics.median(present) if present else None


def summarize_cases(manifest):
    """Runs grouped by case in manifest order, with medians over successful repetitions."""
    grouped = {}
    for run in manifest.get("runs", []):
        grouped.setdefault(str(run.get("case", "unknown")), []).append(run)
    cases = []
    for name, runs in grouped.items():
        ok = [run for run in runs if run.get("status") == "ok"]
        statuses = {}
        for run in runs:
            status = str(run.get("status", "error"))
            statuses[status] = statuses.get(status, 0) + 1
        values = [run_values(run) for run in ok]
        medians = {key: _median(value[key] for value in values) for key in values[0]} if values else {}
        mode = _mode(ok[0] if ok else runs[0])
        effective = ((ok[0].get("core") or {}).get("effective") or {}) if ok else {}
        metric = None
        if mode in PRIMARY and values:
            key, label, unit, better = PRIMARY[mode]
            present = [value[key] for value in values if value[key] is not None]
            if present:
                metric = {
                    "key": key,
                    "label": label,
                    "unit": unit,
                    "better": better,
                    "median": statistics.median(present),
                    "min": min(present),
                    "max": max(present),
                }
        parameters = runs[0].get("parameters") or {}
        generator = parameters.get("generator") or {}
        cases.append(
            {
                "case": name,
                "mode": mode,
                "policy": effective.get("policy")
                or generator.get("policy")
                or parameters.get("policy", "other"),
                "ok": len(ok),
                "total": len(runs),
                "statuses": statuses,
                "metric": metric,
                "medians": medians,
            }
        )
    return cases


def _ok_by_repetition(manifest, case):
    return {
        run.get("repetition"): run
        for run in manifest.get("runs", [])
        if run.get("case") == case and run.get("status") == "ok"
    }


def _finding(text, **data):
    return {"text": text, "data": data}


def _case(cases, name):
    return next((summary for summary in cases if summary["case"] == name), None)


def _value(cases, case, key):
    summary = _case(cases, case)
    return summary["medians"].get(key) if summary else None


def _ms(value):
    return "n/a" if value is None else f"{format_number(value)} ms"


def _percent(value):
    return "n/a" if value is None else f"{value:.1f}%"


def _periodic_comparison(cases, labelled, intro):
    """'intro: label p99 (misses), ...'; names a lowest case only if its range is clearly apart."""
    parts, data = [], {}
    for case, label in labelled:
        summary = _case(cases, case)
        metric = summary["metric"] if summary else None
        if not metric or metric["key"] != "response_p99_ms":
            continue
        misses = summary["medians"].get("miss_percent")
        parts.append(f"{label} {_ms(metric['median'])} ({_percent(misses)} missed)")
        data[case] = {"label": label, "median": metric["median"], "min": metric["min"], "max": metric["max"]}
    if len(parts) < 2:
        return []
    best = min(data, key=lambda case: data[case]["median"])
    if all(data[best]["max"] < other["min"] for case, other in data.items() if case != best):
        verdict = f"Lowest tail: {data[best]['label']}, below the others in every repetition."
    else:
        verdict = "Their ranges across repetitions overlap, so none is clearly lowest."
    return [_finding(f"{intro}: {'; '.join(parts)}. {verdict}", cases=data)]


def _rt_audio(_manifest, cases):
    """RT audio against the SCHED_OTHER case; FIFO and RR only differ between equal-priority RT tasks."""
    other = _case(cases, "rt-audio-other")
    base = other["metric"] if other else None
    measured = {}
    for policy in ("fifo", "rr"):
        summary = _case(cases, f"rt-audio-{policy}")
        if summary and summary["metric"]:
            measured[policy] = summary
    if not base or not measured:
        return []
    parts = []
    for policy, summary in measured.items():
        median = summary["metric"]["median"]
        ratio = f", {base['median'] / median:.1f}× lower" if median > 0 else ""
        parts.append(f"{_ms(median)} as {policy}{ratio}")
    misses = ", ".join(
        f"{label} {_percent(summary['medians'].get('miss_percent'))}"
        for label, summary in (("other", other), *measured.items())
    )
    return [
        _finding(
            f"Against the same SCHED_OTHER background, audio response p99 went from {_ms(base['median'])} "
            f"as other to {' and '.join(parts)} (deadline misses: {misses}). With a single RT task on the "
            "CPU, FIFO and RR behave the same; RR only time-slices between RT tasks of equal priority (see "
            "the sharing preset), so any gap between them here is run-to-run variation.",
            other_ms=base["median"],
            rt_ms={policy: summary["metric"]["median"] for policy, summary in measured.items()},
        )
    ]


def _background(cases, labelled, subject):
    """The offline contender's throughput beside the CPU time it received, to explain any gap."""
    rows = []
    for case, label in labelled:
        throughput = _value(cases, case, "throughput_jobs_s")
        share = _value(cases, case, "cpu_share")
        if throughput is not None and share is not None:
            rows.append((label, throughput, share))
    if len(rows) < 2:
        return []
    throughputs = [throughput for _, throughput, _ in rows]
    shares = [share for _, _, share in rows]
    listed = "; ".join(f"{label} {format_number(t)} jobs/s on {s:.0%} of a CPU" for label, t, s in rows)
    if min(throughputs) <= 0 or max(throughputs) / min(throughputs) < 1.15:
        verdict = "its throughput hardly changed."
    elif min(shares) <= 0 or max(shares) / min(shares) >= 1.1:
        verdict = "the throughput gap follows the CPU time it received."
    else:
        verdict = (
            "it received about the same CPU time each time, so the gap is speed per CPU-second "
            "(clock frequency, a busy SMT sibling or caches), not scheduling."
        )
    return [
        _finding(
            f"Background contender {subject}: {listed}; {verdict}",
            background={label: {"throughput_jobs_s": t, "cpu_share": s} for label, t, s in rows},
        )
    ]


def _alone_vs_contended(manifest, cases):
    preset = manifest.get("preset")
    alone = _value(cases, f"{preset}-alone", "response_p99_ms")
    contended = _value(cases, f"{preset}-contended", "response_p99_ms")
    if alone is None or contended is None:
        return []
    misses = (
        _value(cases, f"{preset}-alone", "miss_percent"),
        _value(cases, f"{preset}-contended", "miss_percent"),
    )
    ratio = f" ({contended / alone:.1f}×)" if alone > 0 else ""
    return [
        _finding(
            f"Sharing its CPU with a busy offline contender moved {preset} response p99 from "
            f"{_ms(alone)} to {_ms(contended)}{ratio}; deadline misses went from "
            f"{_percent(misses[0])} to {_percent(misses[1])}.",
            alone_ms=alone,
            contended_ms=contended,
        )
    ]


def _cpu_share(first, second):
    first_ns = (first.get("metrics") or {}).get("cpu_total_ns")
    second_ns = (second.get("metrics") or {}).get("cpu_total_ns")
    if first_ns is None or second_ns is None or first_ns + second_ns <= 0:
        return None
    return first_ns / (first_ns + second_ns)


def _fairness(manifest, _cases):
    findings = []
    names = (str(run.get("case", "")) for run in manifest.get("runs", []))
    controls = dict.fromkeys(name for name in names if name.startswith("fair-") and name.endswith("-control"))
    for control_case in controls:
        candidate_case = control_case.removesuffix("control") + "candidate"
        controls_by_rep = _ok_by_repetition(manifest, control_case)
        candidates_by_rep = _ok_by_repetition(manifest, candidate_case)
        common = sorted(controls_by_rep.keys() & candidates_by_rep.keys(), key=str)
        shares = [_cpu_share(candidates_by_rep[rep], controls_by_rep[rep]) for rep in common]
        shares = [share for share in shares if share is not None]
        if not shares:
            continue
        candidate = (candidates_by_rep[common[0]].get("core") or {}).get("effective") or {}
        control = (controls_by_rep[common[0]].get("core") or {}).get("effective") or {}
        policy, nice = candidate.get("policy"), candidate.get("nice")
        weights = (load_weight(policy, nice), load_weight(control.get("policy"), control.get("nice")))
        model = weights[0] / sum(weights) if None not in weights else None
        measured = statistics.median(shares)
        expected = f" (load-weight model {model:.1%})" if model is not None else ""
        findings.append(
            _finding(
                f"{policy} at nice {nice} against other at nice {control.get('nice')} on one CPU received "
                f"{measured:.1%} of their combined CPU time{expected}.",
                case=candidate_case,
                measured_share=measured,
                model_share=model,
            )
        )
    if findings:
        findings.append(
            _finding(
                "The model assumes both tasks stay runnable on the same CPU in one autogroup; short "
                "windows, wakeup placement and other load on that CPU move the measured share."
            )
        )
    return findings


def _cgroup(manifest, _cases):
    findings = []
    runs = [run for run in manifest.get("runs", []) if run.get("status") == "ok" and run.get("cgroup")]
    weight_runs = {}
    for run in runs:
        case = str(run.get("case"))
        if case.startswith("cgroup-weight-"):
            weight_runs.setdefault(run.get("repetition"), {})[case] = run
    shares, model = [], None
    for pair in weight_runs.values():
        if len(pair) != 2:
            continue
        first, second = (pair[case] for case in sorted(pair))
        share = _cpu_share(second, first)
        weights = [int(run["cgroup"]["requested"].get("cpu.weight", 0)) for run in (first, second)]
        if share is not None and sum(weights) > 0:
            shares.append(share)
            model = weights[1] / sum(weights)
            weight_text = f"cpu.weight {weights[1]} against {weights[0]}"
    if shares:
        findings.append(
            _finding(
                f"The leaf with {weight_text} received {statistics.median(shares):.1%} of the shared CPU "
                f"(weight model {model:.1%}).",
                measured_share=statistics.median(shares),
                model_share=model,
            )
        )
    quotas = {}
    for run in runs:
        requested = str(run["cgroup"]["requested"].get("cpu.max", ""))
        quota, _, period = requested.partition(" ")
        metrics = run.get("metrics") or {}
        if quota.isdigit() and period.isdigit() and metrics.get("elapsed_ns"):
            used = metrics.get("cpu_total_ns", 0) / metrics["elapsed_ns"]
            quotas.setdefault((str(run.get("case")), quota, period), []).append(used)
    for (case, quota, period), used in quotas.items():
        cap = int(quota) / int(period)
        findings.append(
            _finding(
                f"{case}: cpu.max {quota}/{period} capped the leaf at {cap:.0%} of one CPU; it used "
                f"{statistics.median(used):.1%}.",
                case=case,
                used=statistics.median(used),
            )
        )
    return findings


def _sharing(manifest, _cases):
    findings = []
    for policy in ("fifo", "rr"):
        first = _ok_by_repetition(manifest, f"sharing-{policy}-0")
        second = _ok_by_repetition(manifest, f"sharing-{policy}-1")
        overlaps, quantum = [], None
        for rep in first.keys() & second.keys():
            pair = [(first[rep].get("metrics") or {}), (second[rep].get("metrics") or {})]
            starts = [metrics.get("first_start_ns") for metrics in pair]
            finishes = [metrics.get("last_finish_ns") for metrics in pair]
            if None in starts or None in finishes:
                continue
            overlaps.append((min(finishes) - max(starts)) / 1e6)
            quantum = ((first[rep].get("core") or {}).get("effective") or {}).get("rr_quantum_ns")
        if not overlaps:
            continue
        overlap = statistics.median(overlaps)
        if overlap > 0:
            text = f"{policy}: the two equal-priority tasks on one CPU overlapped for {_ms(overlap)}"
            text += (
                f", time-sliced in {_ms(quantum / 1e6)} quanta."
                if policy == "rr" and quantum
                else ", which FIFO run-to-completion does not predict; check placement and blocking."
            )
        else:
            text = (
                f"{policy}: the two equal-priority tasks on one CPU ran back-to-back, {_ms(-overlap)} apart"
            )
            text += (
                ": FIFO runs each task to completion, so their throughput differs only because one ran first."
                if policy == "fifo"
                else "; each may have finished within one RR quantum."
            )
        findings.append(_finding(text, policy=policy, overlap_ms=overlap))
    return findings


def _locks(manifest, _cases):
    findings = []
    for protocol in ("none", "inherit"):
        runs = list(_ok_by_repetition(manifest, f"lock-{protocol}").values())
        locks = [(run.get("core") or {}).get("lock") or {} for run in runs]
        locks = [
            lock for lock in locks if None not in (lock.get("low_finish_ns"), lock.get("medium_finish_ns"))
        ]
        if not locks:
            continue
        inverted = sum(lock["medium_finish_ns"] < lock["low_finish_ns"] for lock in locks)
        wait = _median(lock.get("high_wait_ns", 0) / 1e6 for lock in locks)
        meaning = (
            "priority inversion: medium work ran while the high-priority thread waited"
            if inverted
            else "the lock holder finished first"
        )
        findings.append(
            _finding(
                f"Mutex protocol {protocol}: the high-priority thread waited {_ms(wait)}; the medium "
                f"thread finished before the low-priority lock holder in {inverted}/{len(locks)} runs "
                f"({meaning}).",
                protocol=protocol,
                inverted=inverted,
                runs=len(locks),
            )
        )
    return findings


def _memory(_manifest, cases):
    parts, data = [], {}
    for strategy in ("demand", "prefault", "mlock"):
        faults = _value(cases, f"memory-{strategy}", "minor_faults")
        p99 = _value(cases, f"memory-{strategy}", "response_p99_ms")
        if faults is None and p99 is None:
            continue
        faults_text = "n/a" if faults is None else f"{faults:.0f}"
        parts.append(f"{strategy} {faults_text} minor faults, p99 {_ms(p99)}")
        data[strategy] = {"minor_faults": faults, "response_p99_ms": p99}
    if not parts:
        return []
    return [_finding(f"During the measured jobs: {'; '.join(parts)}.", strategies=data)]


def _offline(_manifest, cases):
    values = {
        policy: _value(cases, f"offline-{policy}", "throughput_jobs_s")
        for policy in ("other", "batch", "idle")
    }
    values = {policy: value for policy, value in values.items() if value is not None}
    if len(values) < 2:
        return []
    spread = (
        (max(values.values()) - min(values.values())) / max(values.values()) if max(values.values()) else 0
    )
    listed = ", ".join(f"{policy} {format_number(value)} jobs/s" for policy, value in values.items())
    return [
        _finding(
            f"Alone on one CPU, throughput was {listed} (spread {spread:.1%}); without competition the "
            "policy itself changes little.",
            throughput=values,
        )
    ]


def _deadline(manifest, cases):
    runs = list(_ok_by_repetition(manifest, "deadline-audio").values())
    if not runs:
        return []
    effective = (runs[0].get("core") or {}).get("effective") or {}
    budget = "/".join(_ms(effective.get(key, 0) / 1e6) for key in ("runtime_ns", "deadline_ns", "period_ns"))
    return [
        _finding(
            f"SCHED_DEADLINE with runtime/deadline/period {budget}: response p99 "
            f"{_ms(_value(cases, 'deadline-audio', 'response_p99_ms'))}, "
            f"{_percent(_value(cases, 'deadline-audio', 'miss_percent'))} of jobs missed their deadline."
        )
    ]


def _periodic_health(_manifest, cases):
    findings = []
    for case in cases:
        misses = case["medians"].get("miss_percent")
        p99 = case["medians"].get("response_p99_ms")
        if case["mode"] in ("audio", "video") and misses is not None:
            findings.append(
                _finding(
                    f"{case['case']}: {100 - misses:.1f}% of jobs finished before the next release; "
                    f"response p99 {_ms(p99)}.",
                    case=case["case"],
                )
            )
    return findings


PLACEMENT_TEXT = {
    "same": "on the measured CPU",
    "sibling": "on its SMT sibling",
    "others": "on the other cores",
    "all": "on every CPU",
}


def _ratio(value, reference):
    return value / reference if value is not None and reference else None


def _generators(manifest):
    """Generator spec of every load case, in manifest order."""
    specs = {}
    for run in manifest.get("runs", []):
        if run.get("role") == "load":
            specs.setdefault(str(run.get("case")), (run.get("parameters") or {}).get("generator") or {})
    return specs


def _priority_text(policy, nice):
    return f"nice {nice}" if policy == "other" else policy.upper()


def _scenario_text(generator):
    priority = _priority_text(generator.get("policy", "other"), generator.get("nice", 0))
    session = "its own session" if generator.get("session") == "own" else "the lab's session"
    placement = PLACEMENT_TEXT.get(generator.get("placement"), "")
    return f"{generator.get('kind')} load {placement} ({priority}, {session})"


def _scenarios(manifest, cases):
    """Each measured case under load beside its idle baseline and what the generator achieved."""
    rows = []
    for load_case, generator in _generators(manifest).items():
        measured = _case(cases, load_case.removesuffix("-load"))
        baseline = _case(cases, generator.get("baseline", ""))
        if not measured or not baseline or not measured["metric"] or not baseline["metric"]:
            continue
        load = _case(cases, load_case)
        rows.append(
            {
                "measured": measured,
                "baseline": baseline,
                "generator": generator,
                "ratio": _ratio(measured["metric"]["median"], baseline["metric"]["median"]),
                "speed": _ratio(
                    measured["medians"].get("jobs_per_cpu_s"), baseline["medians"].get("jobs_per_cpu_s")
                ),
                "load_percent": load["medians"].get("load_percent") if load else None,
                "write_mib_s": load["medians"].get("write_mib_s") if load else None,
            }
        )
    return rows


def _versus_baseline(manifest, cases):
    findings = []
    for row in _scenarios(manifest, cases):
        measured, baseline = row["measured"], row["baseline"]
        metric, now, before = measured["metric"], measured["medians"], baseline["medians"]
        old, new = format_number(baseline["metric"]["median"]), format_number(metric["median"])
        change = f"{old} → {new} {metric['unit']}"
        if row["ratio"] is not None:
            change += f" ({row['ratio']:.2f}×)"
        details = []
        if measured["mode"] in ("audio", "video"):
            details.append(
                f"misses {_percent(before.get('miss_percent'))} → {_percent(now.get('miss_percent'))}"
            )
        else:
            if now.get("cpu_share") is not None:
                details.append(f"it ran {now['cpu_share']:.0%} of the window")
            if row["speed"] is not None:
                details.append(f"speed per CPU-second {row['speed']:.2f}×")
        if now.get("wait_ms") is not None:
            details.append(f"waited {format_number(now['wait_ms'])} ms for its CPU")
        if now.get("others_share") is not None:
            details.append(f"others ran {now['others_share']:.0%} of that CPU")
        if now.get("sibling_busy_share") is not None and before.get("sibling_busy_share") is not None:
            details.append(
                f"SMT sibling busy {before['sibling_busy_share']:.0%} → {now['sibling_busy_share']:.0%}"
            )
        if row["write_mib_s"] is not None:
            details.append(f"the load wrote {format_number(row['write_mib_s'])} MiB/s with fsync")
        elif row["load_percent"] is not None:
            details.append(f"the load kept its CPUs {row['load_percent']:.0f}% busy")
        findings.append(
            _finding(
                f"{measured['case']} with {_scenario_text(row['generator'])}: {metric['label']} {change}; "
                f"{', '.join(details)}.",
                case=measured["case"],
                baseline=baseline["case"],
                ratio=row["ratio"],
                speed=row["speed"],
                load_percent=row["load_percent"],
            )
        )
    return findings


def _load_audio_summary(manifest, cases):
    worst = {}
    for row in _scenarios(manifest, cases):
        policy = row["measured"]["policy"]
        if row["ratio"] is not None and (policy not in worst or row["ratio"] > worst[policy]["ratio"]):
            worst[policy] = row
    if not worst:
        return []
    parts = [
        f"{policy} audio {row['ratio']:.1f}× under {row['generator'].get('kind')} load"
        for policy, row in worst.items()
    ]
    text = f"Worst response-tail change against its idle baseline: {'; '.join(parts)}."
    if "fifo" in worst and "other" in worst:
        text += (
            " RT priority removes competition for the audio thread's own CPU, but not the SMT, cache, "
            "memory-bandwidth and clock effects that load on other CPUs still has."
        )
    return [_finding(text, worst={policy: row["ratio"] for policy, row in worst.items()})]


def _placement_summary(manifest, cases):
    findings = []
    for row in _scenarios(manifest, cases):
        measured, placement = row["measured"], row["generator"].get("placement")
        others = measured["medians"].get("others_share") or 0
        wait = measured["medians"].get("wait_ms") or 0
        if placement in ("sibling", "others") and others > 0.1:
            # Offline work pays in CPU share; periodic work only pays when it has to wait to run.
            if measured["mode"] == "offline":
                cost = f"{others:.0%} of its own CPU"
            elif wait >= 1:
                between = f"others ran {others:.0%} of its CPU between jobs"
                cost = f"{format_number(wait)} ms of runqueue wait ({between})"
            else:
                continue
            findings.append(
                _finding(
                    f"Load {PLACEMENT_TEXT[placement]} still cost {measured['case']} {cost}: tasks displaced "
                    "from the busy CPUs moved onto its CPU.",
                    case=measured["case"],
                )
            )
        speed, share = row["speed"], measured["medians"].get("cpu_share")
        if placement == "sibling" and speed is not None and speed < 0.9 and share is not None and share > 0.9:
            findings.append(
                _finding(
                    f"On its SMT sibling the load took no CPU time from {measured['case']} yet cut its speed "
                    f"to {speed:.2f}× per CPU-second: the two hardware threads share one core.",
                    case=measured["case"],
                )
            )
    return findings


def _priority_summary(manifest, cases):
    """Offline CPU share against a background thread on its CPU, per priority and session."""
    shares = {}
    for row in _scenarios(manifest, cases):
        generator, measured = row["generator"], row["measured"]
        if measured["mode"] == "offline" and measured["medians"].get("cpu_share") is not None:
            key = (generator.get("policy", "other"), generator.get("nice", 0))
            shares.setdefault(key, {})[generator.get("session", "own")] = measured["medians"]["cpu_share"]
    if not shares:
        return []
    findings = []
    differences = []
    for (policy, nice), by_session in shares.items():
        weights = (load_weight("other", 0), load_weight(policy, nice))
        model = weights[0] / sum(weights) if None not in weights else None
        parts = [
            f"{share:.0%} in {'its own session' if session == 'own' else 'the lab session'}"
            for session, share in sorted(by_session.items(), reverse=True)
        ]
        model_text = f" (load-weight model for one group: {model:.0%})" if model is not None else ""
        findings.append(
            _finding(
                f"Against one CPU-bound thread ({_priority_text(policy, nice)}) on its CPU, offline kept "
                f"{' and '.join(parts)}{model_text}.",
                policy=policy,
                nice=nice,
                shares=by_session,
                model_share=model,
            )
        )
        if {"same", "own"} <= by_session.keys():
            differences.append(abs(by_session["same"] - by_session["own"]))
    if differences:
        autogroup = _read_value(
            ((manifest.get("environment") or {}).get("runtime") or {}).get(
                "/proc/sys/kernel/sched_autogroup_enabled"
            )
        )
        if max(differences) < 0.05:
            text = (
                "The background's session made no difference, so autogroup did not group these tasks "
                f"(sched_autogroup_enabled={autogroup or 'unknown'}). Autogroup only applies to tasks in "
                "the root cgroup; under systemd user slices or containers, cgroups decide instead."
            )
        else:
            text = (
                "Its own session changed the share: with autogroup, each session competes as a group, so a "
                "background job's nice value no longer ranks it against tasks of other sessions."
            )
        findings.append(_finding(text, autogroup=autogroup, largest_difference=max(differences)))
    return findings


def _external(manifest, cases):
    findings = []
    for case in cases:
        medians = case["medians"]
        if case["mode"] == "load" or medians.get("others_share") is None:
            continue
        findings.append(
            _finding(
                f"{case['case']}: other work ran {medians['others_share']:.0%} of the measured CPU, "
                + (
                    f"its SMT sibling was {medians['sibling_busy_share']:.0%} busy, "
                    if medians.get("sibling_busy_share") is not None
                    else ""
                )
                + f"and the task waited {format_number(medians.get('wait_ms') or 0)} ms for a CPU.",
                case=case["case"],
            )
        )
    return findings


PRESET_FINDINGS = {
    "quick": [_periodic_health],
    "offline": [_offline],
    "fairness": [_fairness],
    "audio": [_alone_vs_contended],
    "video": [_alone_vs_contended],
    "mixed": [
        lambda _m, cases: _periodic_comparison(
            cases,
            [(f"mixed-audio-{p}", f"{p} background") for p in ("other", "batch", "idle")],
            "Audio response p99 by background policy",
        ),
        lambda _m, cases: _background(
            cases,
            [(f"mixed-background-{p}", p) for p in ("other", "batch", "idle")],
            "by its own policy",
        ),
    ],
    "affinity": [
        lambda _m, cases: _periodic_comparison(
            cases,
            [
                ("affinity-shared-audio", "shared CPU"),
                ("affinity-separated-audio", "separate CPUs"),
                ("affinity-two-workers-audio", "two contenders"),
            ],
            "Audio response p99 by placement",
        ),
        lambda _m, cases: _background(
            cases,
            [
                ("affinity-shared-background", "sharing the audio CPU"),
                ("affinity-separated-background", "on its own CPU"),
            ],
            "by placement",
        ),
    ],
    "rt": [
        _rt_audio,
        lambda _m, cases: _background(
            cases,
            [(f"rt-background-{p}", f"audio {p}") for p in ("other", "fifo", "rr")],
            "by audio policy",
        ),
    ],
    "sharing": [_sharing],
    "deadline": [_deadline],
    "locks": [_locks],
    "memory": [_memory],
    "cgroup": [_cgroup],
    "load-audio": [_load_audio_summary, _versus_baseline],
    "load-offline": [_versus_baseline],
    "load-placement": [_placement_summary, _versus_baseline],
    "load-priority": [_priority_summary, _versus_baseline],
    "load-external": [_external],
}


def _read_value(entry):
    """The value of an environment.read() record, or None when it was unavailable."""
    return str(entry["value"]) if isinstance(entry, dict) and entry.get("status") == "ok" else None


def _online_cpus(environment):
    """CPUs counted in /proc/loadavg: the online list, falling back to the allowed set."""
    online = _read_value((environment.get("runtime") or {}).get("/sys/devices/system/cpu/online"))
    count = 0
    for part in (online or "").split(","):
        low, _, high = part.partition("-")
        if low.isdigit() and (not high or high.isdigit()):
            count += int(high or low) - int(low) + 1
    return count or len(environment.get("allowed_cpus") or []) or 1


def _runnable_others(load):
    """Tasks runnable when the snapshot was taken, minus the reader itself (/proc/loadavg field 4)."""
    fields = (_read_value((load or {}).get("loadavg")) or "").split()
    running = fields[3].partition("/")[0] if len(fields) > 3 else ""
    return int(running) - 1 if running.isdigit() else None


def _busy_caveats(before, after):
    """Other work outside the lab, judged when no lab worker runs: just before and after the run."""
    cpus = _online_cpus(before)
    threshold = max(2, cpus // 4)
    counts = [_runnable_others(snapshot.get("load")) for snapshot in (before, after)]
    if not any(count is not None and count >= threshold for count in counts):
        return []
    load = _read_value((before.get("load") or {}).get("loadavg")) or ""
    pressure = _read_value((before.get("load") or {}).get("cpu_pressure")) or ""
    some = next((line for line in pressure.splitlines() if line.startswith("some")), "")
    avg = next((field.partition("=")[2] for field in some.split() if field.startswith("avg60=")), "")
    context = [f"1-minute load {load.split()[0]}"] if load else []
    context += [f"CPU pressure {avg}% over the last minute"] if avg else []
    moments = [
        f"{count} other {'task was' if count == 1 else 'tasks were'} runnable just {when} the run"
        for count, when in zip(counts, ("before", "after"), strict=True)
        if count is not None
    ]
    detail = f" ({'; '.join(context)})" if context else ""
    return [
        f"Other work was running on this {cpus}-CPU machine: {' and '.join(moments)}{detail}. It can share "
        "the measured CPU, its SMT sibling and the power budget, so tails and throughput describe a busy "
        "machine; repeat on an idle one before comparing."
    ]


def _shared_core_caveats(manifest, cases):
    """Cases that ran without any lab contender but still met other work on their core."""
    alone = {
        str(run.get("case"))
        for run in manifest.get("runs", [])
        if run.get("group_cases") == [run.get("case")] and not run.get("load_case")
    }
    shared = []
    for case in cases:
        if case["case"] not in alone:
            continue
        medians, signs = case["medians"], []
        if medians.get("wait_ms") is not None and medians["wait_ms"] >= 1:
            signs.append(f"waited {format_number(medians['wait_ms'])} ms")
        if case["mode"] == "offline" and (medians.get("others_share") or 0) > 0.05:
            signs.append(f"others ran {medians['others_share']:.0%} of its CPU")
        if (medians.get("sibling_busy_share") or 0) > 0.3:
            signs.append(f"its SMT sibling was {medians['sibling_busy_share']:.0%} busy")
        if signs:
            shared.append(f"{case['case']} ({', '.join(signs)})")
    if not shared:
        return []
    return [
        f"Cases meant to run alone met other work on their core: {'; '.join(shared)}. Their numbers include "
        "that interference, so they are not idle-machine baselines."
    ]


def _caveats(manifest, cases):
    caveats = []
    environment = manifest.get("environment") or {}
    repetitions = manifest.get("repetitions")
    if isinstance(repetitions, int) and repetitions < 3:
        plural = "" if repetitions == 1 else "s"
        caveats.append(
            f"Only {repetitions} repetition{plural}: differences smaller than the run-to-run spread are "
            "noise; use --repetitions 3 or more before drawing conclusions."
        )
    if manifest.get("status") == "interrupted":
        caveats.append("The run was interrupted, so later cases are missing.")
    build = environment.get("native_build")
    if environment and (not isinstance(build, dict) or build.get("status") == "unavailable"):
        caveats.append("The native build is not recorded; compare compilers and flags by hand.")
    elif isinstance(build, dict):
        if build.get("sanitize"):
            caveats.append(
                f"Built with sanitizers ({build['sanitize']}): these timings are not measurements."
            )
        if build.get("build_type") not in OPTIMIZED_BUILDS:
            caveats.append(f"Build type '{build.get('build_type') or 'none'}' is not the optimized default.")
    caveats.extend(_busy_caveats(environment, manifest.get("environment_after") or {}))
    caveats.extend(_shared_core_caveats(manifest, cases))
    cpu = manifest.get("selected_cpu")
    topology = (environment.get("cpu_topology") or {}).get(str(cpu)) or {}
    governor = _read_value(topology.get("cpufreq/scaling_governor"))
    if governor and governor != "performance":
        details = [
            f"{label} {value}"
            for label, value in (
                ("driver", _read_value(topology.get("cpufreq/scaling_driver"))),
                ("EPP", _read_value(topology.get("cpufreq/energy_performance_preference"))),
            )
            if value
        ]
        detail = f" ({', '.join(details)})" if details else ""
        caveats.append(
            f"CPU {cpu} uses the '{governor}' cpufreq governor{detail}: its clock follows load and power "
            "limits, which adds run-to-run variance, mostly to throughput."
        )
    unstable = [
        f"{case['case']} {case['metric']['max'] / case['metric']['min']:.1f}×"
        for case in cases
        if case["metric"]
        and case["ok"] > 1
        and case["metric"]["min"] > 0
        and case["metric"]["max"] / case["metric"]["min"] > 1.5
    ]
    if unstable:
        caveats.append(
            f"Primary metric varied by more than 1.5× across repetitions: {', '.join(unstable)}; compare "
            "these cases by their ranges, not their medians."
        )
    return caveats


def analyze_manifest(manifest, source=""):
    environment = manifest.get("environment") or {}
    system = environment.get("system") or {}
    identities = environment.get("cpu_identity")
    cpu_model = ""
    if isinstance(identities, list) and identities:
        first = identities[0]
        cpu_model = first.get("model name") or first.get("uarch") or first.get("isa") or ""
    cases = summarize_cases(manifest)
    findings = []
    for compute in PRESET_FINDINGS.get(manifest.get("preset"), []):
        findings.extend(compute(manifest, cases))
    statuses = {"ok": 0, "skipped": 0, "error": 0}
    not_measured = []
    for run in manifest.get("runs", []):
        status = str(run.get("status", "error"))
        statuses[status] = statuses.get(status, 0) + 1
        if status != "ok":
            not_measured.append(
                {"label": str(run.get("label", "")), "status": status, "reason": str(run.get("reason", ""))}
            )
    return {
        "source": str(source),
        "title": str(manifest.get("title") or manifest.get("preset") or "results"),
        "preset": str(manifest.get("preset", "unknown")),
        "status": str(manifest.get("status", "unknown")),
        "repetitions": manifest.get("repetitions"),
        "statuses": statuses,
        "system": {
            "architecture": system.get("architecture", "unknown"),
            "kernel": system.get("kernel", "unknown"),
            "cpu": cpu_model or "unknown CPU",
            "allowed_cpus": len(environment.get("allowed_cpus") or []),
        },
        "build": build_summary(environment.get("native_build")),
        "cases": cases,
        "findings": findings,
        "caveats": _caveats(manifest, cases),
        "not_measured": not_measured,
    }


def compare_analyses(analyses):
    """The same cases across runs of the same preset, plus what differs between the sources."""
    tables = []
    for preset in dict.fromkeys(analysis["preset"] for analysis in analyses):
        members = [index for index, analysis in enumerate(analyses) if analysis["preset"] == preset]
        if len(members) < 2:
            continue
        names = list(dict.fromkeys(case["case"] for index in members for case in analyses[index]["cases"]))
        rows = []
        for name in names:
            cells = []
            for index in members:
                case = next((case for case in analyses[index]["cases"] if case["case"] == name), None)
                cells.append(case["metric"] if case else None)
            reference = next((cell for cell in cells if cell), None)
            if reference:
                rows.append({"case": name, "metric": reference, "values": [c and c["median"] for c in cells]})
        tables.append({"preset": preset, "sources": [index + 1 for index in members], "rows": rows})
    differences = [
        label
        for label, value in (
            ("kernel", lambda a: a["system"]["kernel"]),
            ("architecture", lambda a: a["system"]["architecture"]),
            ("CPU model", lambda a: a["system"]["cpu"]),
            ("native build", lambda a: a["build"]),
        )
        if len({value(analysis) for analysis in analyses}) > 1
    ]
    return {"tables": tables, "differences": differences}


def analyze(paths):
    analyses = []
    for path in paths:
        source, manifest = load_manifest(path)
        analyses.append(analyze_manifest(manifest, source))
    comparison = compare_analyses(analyses) if len(analyses) > 1 else None
    return {
        "schema_version": 1,
        "runs": analyses,
        # Only runs of the same preset share cases to compare.
        "comparison": comparison if comparison and comparison["tables"] else None,
    }


def format_number(value):
    if value is None:
        return "n/a"
    magnitude = abs(value)
    digits = 0 if magnitude >= 100 else 1 if magnitude >= 10 else 2 if magnitude >= 1 else 3
    return f"{value:.{digits}f}"


def bar(value, maximum, width=BAR_WIDTH):
    """A horizontal Unicode bar scaled to `maximum`, in eighth-character steps."""
    if value is None or not maximum or maximum <= 0 or value <= 0:
        return ""
    eighths = max(1, round(value / maximum * width * 8))
    full, rest = divmod(eighths, 8)
    return "█" * full + (EIGHTHS[rest] if rest else "")


def _case_rows(analysis):
    """Display rows with bars scaled per metric across the run's cases."""
    maxima = {}
    for case in analysis["cases"]:
        metric = case["metric"]
        if metric:
            maxima[metric["key"]] = max(maxima.get(metric["key"], 0), metric["median"])
    rows = []
    for case in analysis["cases"]:
        metric = case["metric"]
        value = spread = plot = ""
        if metric:
            value = f"{format_number(metric['median'])} {metric['unit']}"
            if case["ok"] > 1:
                spread = f"{format_number(metric['min'])}–{format_number(metric['max'])}"
            plot = bar(metric["median"], maxima[metric["key"]])
            label = (
                f"{metric['label']} ({metric['better']} is better)" if metric["better"] else metric["label"]
            )
        else:
            label = "not measured" if not case["ok"] else "no primary metric"
        rows.append(
            [
                case["case"],
                case["mode"],
                str(case["policy"]),
                f"{case['ok']}/{case['total']}",
                label,
                value,
                spread,
                contention_text(case["medians"]),
                plot,
            ]
        )
    return rows


def contention_text(medians):
    """'wait 0.4 ms, others 9%, SMT 99%': runqueue wait, others' share of the CPU, sibling busy share."""
    parts = []
    if medians.get("wait_ms") is not None:
        parts.append(f"wait {format_number(medians['wait_ms'])} ms")
    if medians.get("others_share") is not None:
        parts.append(f"others {medians['others_share']:.0%}")
    if medians.get("sibling_busy_share") is not None:
        parts.append(f"SMT {medians['sibling_busy_share']:.0%}")
    return ", ".join(parts)


def _headline(analysis):
    statuses = analysis["statuses"]
    repetitions = analysis["repetitions"]
    counts = (
        f"{statuses.get('ok', 0)} ok, {statuses.get('skipped', 0)} skipped, {statuses.get('error', 0)} error"
    )
    reps = f" · {repetitions} repetition{'' if repetitions == 1 else 's'}" if repetitions else ""
    system = analysis["system"]
    machine = (
        f"{system['architecture']} · Linux {system['kernel']} · {system['cpu']} · "
        f"{system['allowed_cpus']} allowed CPUs"
    )
    return f"{analysis['preset']} · {analysis['status']} · {counts}{reps}", machine


def _text_table(header, rows):
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(str(cell).ljust(width) for cell, width in zip(header, widths, strict=True)).rstrip()]
    lines += [
        "  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
    return lines


CASE_HEADER = ["case", "mode", "policy", "ok", "primary metric", "median", "range", "contention", ""]


def render_text(result):
    lines = []
    for number, analysis in enumerate(result["runs"], 1):
        summary, machine = _headline(analysis)
        lines += [
            f"[{number}] {analysis['title']} ({analysis['source']})",
            f"    {summary}",
            f"    {machine}",
        ]
        lines += [f"    build: {analysis['build']}", ""]
        lines += ["    " + line for line in _text_table(CASE_HEADER, _case_rows(analysis))]
        lines += _text_notes(analysis)
        lines.append("")
    comparison = result.get("comparison")
    if comparison:
        lines += _text_comparison(result["runs"], comparison)
    return "\n".join(lines).rstrip() + "\n"


def _notes(analysis, include_not_measured=True):
    sections = [
        ("Findings", [finding["text"] for finding in analysis["findings"]]),
        ("Caveats", analysis["caveats"]),
    ]
    if include_not_measured:
        skipped = [
            f"{item['label']}: {item['status']}: {item['reason']}" for item in analysis["not_measured"]
        ]
        sections.append(("Not measured", skipped))
    return [(title, items) for title, items in sections if items]


def _text_notes(analysis):
    lines = []
    for title, items in _notes(analysis):
        lines += ["", f"    {title}"] + [f"    - {item}" for item in items]
    return lines


def render_findings_text(analysis):
    """Only findings and caveats, for the end of `schedlab run` (which already lists skips)."""
    lines = []
    for title, items in _notes(analysis, include_not_measured=False):
        lines += [f"{title}:"] + [f"- {item}" for item in items]
    return "\n".join(lines) + "\n" if lines else ""


def _text_comparison(analyses, comparison):
    lines = ["Comparison across runs (same preset and case; ratios use the first run listed)"]
    if comparison["differences"]:
        lines.append(f"    Sources differ in: {', '.join(comparison['differences'])}.")
    for table in comparison["tables"]:
        header = ["case", "metric", *[f"[{source}]" for source in table["sources"]], "ratio"]
        rows = []
        for row in table["rows"]:
            values = row["values"]
            first = next((value for value in values if value), None)
            last = values[-1]
            ratio = f"{last / first:.2f}×" if first and last is not None else ""
            unit = row["metric"]["unit"]
            rows.append(
                [row["case"], f"{row['metric']['label']} ({unit})", *map(format_number, values), ratio]
            )
        lines += ["", f"    {table['preset']}"] + ["    " + line for line in _text_table(header, rows)]
    titles = [
        f"    [{number}] {analysis['title']}: {analysis['build']}"
        for number, analysis in enumerate(analyses, 1)
    ]
    return [*lines, "", *titles]


def _md(text):
    """Escape text for a Markdown table cell or list item (GitHub job summaries render HTML)."""
    return html.escape(str(text), quote=False).replace("|", "\\|").replace("\n", " ")


def _md_table(header, rows, align):
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(align) + "|"]
    lines += ["| " + " | ".join(cells) + " |" for cells in rows]
    return lines


def _md_run(number, analysis):
    summary, machine = _headline(analysis)
    rows = []
    for cells in _case_rows(analysis):
        *text, plot = cells
        rows.append([_md(cell) for cell in text] + [f"`{plot}`" if plot else ""])
    lines = [
        f"**[{number}] {_md(analysis['title'])}** — `{_md(analysis['source'])}`  ",
        f"{_md(summary)}  ",
        f"{_md(machine)} · {_md(analysis['build'])}",
        "",
        *_md_table(
            ["Case", "Mode", "Policy", "OK", "Primary metric", "Median", "Range", "Contention", "Plot"],
            rows,
            ["---", "---", "---", "--:", "---", "--:", "--:", "---", "---"],
        ),
    ]
    for title, items in _notes(analysis):
        lines += ["", f"*{title}*", ""] + [f"- {_md(item)}" for item in items]
    return lines


def render_markdown(result):
    analyses = result["runs"]
    lines = ["## Scheduling lab analysis", ""]
    comparison = result.get("comparison")
    if comparison:
        lines += [
            "Same preset and case across runs; ratios compare the last run with the first. Shared or "
            "virtual machines make these examples, not performance rankings.",
            "",
        ]
        if comparison["differences"]:
            lines += [f"Sources differ in: {_md(', '.join(comparison['differences']))}.", ""]
        names = [f"[{number}] {_md(analysis['title'])}" for number, analysis in enumerate(analyses, 1)]
        lines += [f"Runs: {'; '.join(names)}.", ""]
        for table in comparison["tables"]:
            header = ["Case", "Metric", *[f"[{source}]" for source in table["sources"]], "Ratio"]
            rows = []
            for row in table["rows"]:
                values = row["values"]
                first = next((value for value in values if value), None)
                last = values[-1]
                ratio = f"{last / first:.2f}×" if first and last is not None else ""
                metric = f"{row['metric']['label']} ({row['metric']['unit']})"
                rows.append([_md(row["case"]), _md(metric), *map(format_number, values), ratio])
            align = ["---", "---", *["--:"] * len(table["sources"]), "--:"]
            lines += [f"### {_md(table['preset'])}", "", *_md_table(header, rows, align), ""]
    for number, analysis in enumerate(analyses, 1):
        body = _md_run(number, analysis)
        if len(analyses) > 1:
            count = len(analysis["findings"])
            summary = f"[{number}] {_md(analysis['title'])}: {count} finding{'' if count == 1 else 's'}"
            lines += ["<details>", f"<summary>{summary}</summary>", "", *body, "", "</details>", ""]
        else:
            lines += [*body, ""]
    return "\n".join(lines).rstrip() + "\n"


def render(result, output_format="text"):
    if output_format == "json":
        return json.dumps(result, indent=2) + "\n"
    if output_format == "markdown":
        return render_markdown(result)
    return render_text(result)


def findings_html(analysis):
    """List items for the HTML report: findings first, then caveats."""
    items = [
        f"<li><span class='insight-number'>{number}</span><div>{html.escape(finding['text'])}</div></li>"
        for number, finding in enumerate(analysis["findings"], 1)
    ]
    items += [
        f"<li class='caveat'><span class='insight-number'>!</span><div>{html.escape(caveat)}</div></li>"
        for caveat in analysis["caveats"]
    ]
    if not analysis["findings"]:
        items.insert(
            0,
            "<li><span class='insight-number'>&ndash;</span><div>No preset-specific comparison applies; "
            "use the charts and tables below.</div></li>",
        )
    return "".join(items)
