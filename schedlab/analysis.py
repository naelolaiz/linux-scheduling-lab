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


def run_values(run):
    """The comparable numbers of one successful run, in display units."""
    metrics = run.get("metrics") or {}
    core = run.get("core") or {}
    lock = core.get("lock") or {}
    fraction = metrics.get("miss_fraction")
    cpu_ns = metrics.get("cpu_total_ns")
    return {
        "response_p50_ms": _milliseconds(metrics, "response_ns", "p50"),
        "response_p99_ms": _milliseconds(metrics, "response_ns", "p99"),
        "response_max_ms": _milliseconds(metrics, "response_ns", "max"),
        "wake_p99_ms": _milliseconds(metrics, "wake_lateness_ns", "p99"),
        "wall_p99_ms": _milliseconds(metrics, "wall_processing_ns", "p99"),
        "miss_percent": fraction * 100 if fraction is not None else None,
        "throughput_jobs_s": metrics.get("throughput_jobs_s"),
        "cpu_ms": cpu_ns / 1e6 if cpu_ns is not None else None,
        "minor_faults": core.get("minor_faults"),
        "involuntary_cs": core.get("involuntary_cs"),
        "high_wait_ms": lock["high_wait_ns"] / 1e6 if lock.get("high_wait_ns") is not None else None,
    }


def _mode(run):
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
        cases.append(
            {
                "case": name,
                "mode": mode,
                "policy": effective.get("policy") or (runs[0].get("parameters") or {}).get("policy", "other"),
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


def _value(cases, case, key):
    for summary in cases:
        if summary["case"] == case:
            return summary["medians"].get(key)
    return None


def _ms(value):
    return "n/a" if value is None else f"{format_number(value)} ms"


def _percent(value):
    return "n/a" if value is None else f"{value:.1f}%"


def _periodic_comparison(cases, labelled, intro):
    """'intro: label p99 (misses), ...' for periodic cases that were measured."""
    parts, data = [], {}
    for case, label in labelled:
        p99 = _value(cases, case, "response_p99_ms")
        if p99 is None:
            continue
        misses = _value(cases, case, "miss_percent")
        parts.append(f"{label} {_ms(p99)} ({_percent(misses)} missed)")
        data[case] = {"response_p99_ms": p99, "miss_percent": misses}
    if len(parts) < 2:
        return []
    best = min(data, key=lambda case: data[case]["response_p99_ms"])
    label = dict(labelled)[best]
    return [_finding(f"{intro}: {'; '.join(parts)}. Lowest tail: {label}.", cases=data)]


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
    for run in runs:
        requested = str(run["cgroup"]["requested"].get("cpu.max", ""))
        quota, _, period = requested.partition(" ")
        metrics = run.get("metrics") or {}
        if not quota.isdigit() or not period.isdigit() or not metrics.get("elapsed_ns"):
            continue
        used = metrics.get("cpu_total_ns", 0) / metrics["elapsed_ns"]
        cap = int(quota) / int(period)
        findings.append(
            _finding(
                f"{run.get('label')}: cpu.max {quota}/{period} capped the leaf at {cap:.0%} of one CPU; "
                f"it used {used:.1%}.",
                label=run.get("label"),
                used=used,
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
                ": FIFO runs each task to completion."
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
        )
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
        )
    ],
    "rt": [
        lambda _m, cases: _periodic_comparison(
            cases,
            [(f"rt-audio-{p}", p) for p in ("other", "fifo", "rr")],
            "Audio response p99 against the same background, by audio policy",
        )
    ],
    "sharing": [_sharing],
    "deadline": [_deadline],
    "locks": [_locks],
    "memory": [_memory],
    "cgroup": [_cgroup],
}


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
    cpu = manifest.get("selected_cpu")
    governor = ((environment.get("cpu_topology") or {}).get(str(cpu)) or {}).get("cpufreq/scaling_governor")
    if (
        isinstance(governor, dict)
        and governor.get("status") == "ok"
        and governor.get("value") != "performance"
    ):
        caveats.append(
            f"CPU {cpu} uses the '{governor['value']}' cpufreq governor: frequency changes add variance."
        )
    for case in cases:
        metric = case["metric"]
        if metric and case["ok"] > 1 and metric["min"] > 0 and metric["max"] / metric["min"] > 1.5:
            caveats.append(
                f"{case['case']}: {metric['label']} varied {metric['max'] / metric['min']:.1f}× across "
                "repetitions."
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
            label = f"{metric['label']} ({metric['better']} is better)"
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
                plot,
            ]
        )
    return rows


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


CASE_HEADER = ["case", "mode", "policy", "ok", "primary metric", "median", "range", ""]


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
            ["Case", "Mode", "Policy", "OK", "Primary metric", "Median", "Range", "Plot"],
            rows,
            ["---", "---", "---", "--:", "---", "--:", "--:", "---"],
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
