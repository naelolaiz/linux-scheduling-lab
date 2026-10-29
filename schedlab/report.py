"""Self-contained, interactive HTML reports with no network dependencies."""

import html
import json
from pathlib import Path
import re
import statistics
from urllib.parse import urlsplit

TEMPLATE = Path(__file__).with_name("report_template.html")


def _milliseconds(distribution, key):
    value = distribution.get(key) if distribution else None
    return value / 1e6 if value is not None else None


def _artifact_link(run):
    prefix = str(run.get("artifact_prefix", ""))
    if not prefix or any(ord(character) < 32 for character in prefix):
        return None
    target = prefix + ".csv"
    try:
        parsed = urlsplit(target)
    except ValueError:
        return None
    if parsed.scheme not in ("", "file") or (not parsed.scheme and target.startswith("//")):
        return None
    return target


def _view(run, index):
    metrics = run.get("metrics", {}) if run.get("status") == "ok" else {}
    core = run.get("core", {})
    effective = core.get("effective", {})
    wall = metrics.get("wall_processing_ns", {})
    misses = metrics.get("deadline_misses")
    miss_fraction = metrics.get("miss_fraction")
    p50 = _milliseconds(wall, "p50")
    p99 = _milliseconds(wall, "p99")
    affinity = effective.get("affinity", [])
    return {
        "index": index,
        "label": str(run.get("label", f"run {index + 1}")),
        "case": str(run.get("case", "unknown")),
        "repetition": run.get("repetition"),
        "status": str(run.get("status", "error")),
        "reason": str(run.get("reason", "")),
        "link": _artifact_link(run),
        "policy": str(effective.get("policy", "unavailable")),
        "nice": effective.get("nice"),
        "priority": effective.get("priority"),
        "affinity": affinity if isinstance(affinity, list) else [],
        "mode": str(core.get("mode", run.get("parameters", {}).get("mode", "unknown"))),
        "samples": metrics.get("samples", 0),
        "wall_p50_ms": p50,
        "wall_p95_ms": _milliseconds(wall, "p95"),
        "wall_p99_ms": p99,
        "wall_max_ms": _milliseconds(wall, "max"),
        "cpu_p99_ms": _milliseconds(metrics.get("cpu_processing_ns", {}), "p99"),
        "response_p99_ms": _milliseconds(metrics.get("response_ns", {}), "p99"),
        "wake_p99_ms": _milliseconds(metrics.get("wake_lateness_ns", {}), "p99"),
        "throughput": metrics.get("throughput_jobs_s"),
        "deadline_misses": misses,
        "miss_percent": miss_fraction * 100 if miss_fraction is not None else None,
        "tail_ratio": p99 / p50 if p50 not in (None, 0) and p99 is not None else None,
        "minor_faults": core.get("minor_faults"),
        "major_faults": core.get("major_faults"),
        "voluntary_cs": core.get("voluntary_cs"),
        "involuntary_cs": core.get("involuntary_cs"),
        "rusage_available": core.get("rusage_available"),
    }


def _number(value, digits=3, suffix=""):
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


def _run_rows(views):
    rows = []
    for run in views:
        label = html.escape(run["label"])
        if run["link"]:
            label = f"<a href='{html.escape(run['link'], quote=True)}'>{label}</a>"
        nice = run["nice"] if run["nice"] is not None else "n/a"
        priority = run["priority"] if run["priority"] is not None else "n/a"
        cpus = ",".join(map(str, run["affinity"])) or "n/a"
        scheduler = f"{run['policy']} · nice {nice} · RT {priority} · CPU {cpus}"
        if run["status"] == "ok" and run["samples"]:
            misses = (
                "n/a"
                if run["deadline_misses"] is None
                else f"{run['deadline_misses']} · {run['miss_percent']:.1f}%"
            )
            values = (
                str(run["samples"]),
                _number(run["wall_p50_ms"]),
                _number(run["wall_p99_ms"]),
                _number(run["wall_max_ms"]),
                _number(run["response_p99_ms"]),
                _number(run["wake_p99_ms"]),
                misses,
                _number(run["throughput"], 1),
                html.escape(scheduler),
            )
        elif run["status"] == "ok":
            values = (
                "0",
                "n/a",
                "n/a",
                "n/a",
                "n/a",
                "n/a",
                "n/a",
                "0.0",
                "No jobs started in the measurement window",
            )
        else:
            values = (
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                html.escape(run["reason"] or "No reason recorded"),
            )
        status = html.escape(run["status"])
        cells = "".join(f"<td>{value}</td>" for value in values)
        rows.append(
            f"<tr data-run-index='{run['index']}'><td>{label}</td>"
            f"<td><span class='status status-{status}'>{status}</span></td>{cells}</tr>"
        )
    return "".join(rows)


def _repeat_rows(views):
    groups = {}
    for run in views:
        if run["status"] == "ok" and run["wall_p99_ms"] is not None:
            groups.setdefault(run["case"], []).append(run)
    rows = []
    for case, runs in sorted(groups.items()):
        p99s = [run["wall_p99_ms"] for run in runs]
        maxima = [run["wall_max_ms"] for run in runs]
        median, low, high, worst = statistics.median(p99s), min(p99s), max(p99s), max(maxima)
        rows.append(
            f"<tr><td>{html.escape(case)}</td><td>{len(runs)}</td><td>{median:.3f}</td>"
            f"<td>{low:.3f}&ndash;{high:.3f}</td><td>{worst:.3f}</td></tr>"
        )
    return "".join(rows) or "<tr><td colspan='5'>No successful repetitions to summarize.</td></tr>"


def _summary(views):
    successful = [run for run in views if run["status"] == "ok"]
    measured = [run for run in successful if run["wall_p99_ms"] is not None]
    periodic = [run for run in successful if run["deadline_misses"] is not None]
    total_misses = sum(run["deadline_misses"] for run in periodic)
    periodic_samples = sum(run["samples"] for run in periodic)
    return {
        "RUNS": str(len(views)),
        "SUCCESSFUL": str(len(successful)),
        "P99": _number(statistics.median(run["wall_p99_ms"] for run in measured), 3, " ms")
        if measured
        else "n/a",
        "MISSES": f"{total_misses} / {periodic_samples}" if periodic else "n/a",
    }


def _script_json(value):
    return (
        json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _render(template, values):
    pattern = re.compile(r"@@([A-Z0-9_]+)@@")
    return pattern.sub(lambda match: values[match.group(1)], template)


def write_report(manifest, path):
    views = [_view(run, index) for index, run in enumerate(manifest.get("runs", []))]
    environment = manifest.get("environment", {})
    system = environment.get("system", {})
    title = str(manifest.get("title", manifest.get("preset", "Scheduling comparison")))
    summary = _summary(views)
    status = str(manifest.get("status", "comparison"))
    architecture = system.get("architecture", "unknown architecture")
    kernel = system.get("kernel", "unknown kernel")
    host = system.get("hostname", "unknown host")
    values = {
        "TITLE": html.escape(title),
        "PAGE_TITLE": html.escape(title + " · Linux scheduling lab"),
        "STATUS": html.escape(status),
        "STATUS_CLASS": html.escape(
            status if status in ("ok", "skipped", "error", "interrupted") else "comparison"
        ),
        "SYSTEM": html.escape(f"{architecture} · Linux {kernel} · {host}"),
        "ROWS": _run_rows(views),
        "REPEATS": _repeat_rows(views),
        "MANIFEST": html.escape(json.dumps(manifest, indent=2, sort_keys=True)),
        "REPORT_DATA": _script_json({"title": title, "status": status, "runs": views}),
        **summary,
    }
    Path(path).write_text(_render(TEMPLATE.read_text(), values))


def compare(paths, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    combined = {
        "schema_version": 1,
        "preset": "comparison",
        "runs": [],
        "sources": [],
        "title": (
            "Cross-run comparison: verify equivalent parameters, binaries and hardware before attribution."
        ),
    }
    for index, path in enumerate(paths):
        source = Path(path)
        if source.is_dir():
            source = source / "run.json"
        manifest = json.loads(source.read_text())
        if manifest.get("schema_version") != 1:
            raise ValueError(f"unsupported manifest schema: {source}")
        combined["sources"].append({"path": str(source.resolve()), "manifest": manifest})
        for original in manifest["runs"]:
            run = dict(original)
            run["label"] = f"{index + 1}: {run['label']}"
            run["case"] = f"{index + 1}: {run['case']}"
            if "artifact_prefix" in run:
                # Absolute file URI also works when the report moves to a comparison directory.
                run["artifact_prefix"] = (source.parent / run["artifact_prefix"]).resolve().as_uri()
            combined["runs"].append(run)
    (output / "run.json").write_text(json.dumps(combined, indent=2) + "\n")
    write_report(combined, output / "report.html")
    return output
