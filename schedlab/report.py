"""Self-contained HTML: no network requests, JS libraries or plotting dependencies."""

import html
import json
from pathlib import Path
import statistics


def write_report(manifest, path):
    rows = []
    successful = [run for run in manifest["runs"] if run["status"] == "ok" and run["metrics"]["samples"]]
    maximum = max((r["metrics"]["wall_processing_ns"]["max"] for r in successful), default=1) or 1
    charts = []
    for run in manifest["runs"]:
        label = html.escape(run["label"])
        status = html.escape(run["status"])
        if status != "ok":
            rows.append("<tr><td>%s</td><td>%s</td><td colspan='7'>%s</td></tr>" %
                        (label, status, html.escape(run.get("reason", ""))))
            continue
        m = run["metrics"]
        core = run["core"]
        if not m["samples"]:
            rows.append("<tr><td>%s</td><td>ok</td><td>0</td><td colspan='6'>"
                        "No job started within the measurement window; throughput 0 jobs/s. "
                        "This may be starvation, not a missing result.</td></tr>" % label)
            continue
        misses = m["deadline_misses"]
        effective = core["effective"]
        description = "%s / nice %d / RT %d / CPUs %s" % (
            effective["policy"], effective["nice"], effective["priority"], effective["affinity"])
        link = html.escape(run.get("artifact_prefix", ""), quote=True) + ".csv"
        rows.append(
            "<tr><td><a href='%s'>%s</a></td><td>%s</td><td>%d</td>"
            "<td>%.3f / %.3f / %.3f</td><td>%.3f</td><td>%.3f</td><td>%s</td>"
            "<td>%.1f</td><td>%s</td></tr>" %
            (link, label, status, m["samples"], m["wall_processing_ns"]["p50"] / 1e6,
             m["wall_processing_ns"]["p99"] / 1e6, m["wall_processing_ns"]["max"] / 1e6,
             m["response_ns"]["p99"] / 1e6, m["wake_lateness_ns"]["p99"] / 1e6,
             "n/a" if misses is None else "%d (%.1f%%)" % (misses, 100*m["miss_fraction"]),
             m["throughput_jobs_s"], html.escape(description)))
        # All bars use a shared scale, preserving honest comparisons between runs.
        bars = []
        for index, (key, color) in enumerate((("max", "#8796a5"), ("p99", "#e7a330"), ("p50", "#2879b8"))):
            width = 560 * m["wall_processing_ns"][key] / maximum
            bars.append("<rect x='0' y='%d' width='%.2f' height='9' fill='%s'/>" %
                        (index*11, width, color))
        charts.append("<div class='chart'><b>%s</b><svg viewBox='0 0 560 34' role='img' "
                      "aria-label='Wall processing maximum, p99 and median'>%s</svg></div>" %
                      (label, "".join(bars)))
    groups = {}
    for run in successful:
        groups.setdefault(run["case"], []).append(run)
    repeat_rows = []
    for case, runs in groups.items():
        repeat_rows.append("<tr><td>%s</td><td>%d</td><td>%.3f</td><td>%.3f</td></tr>" % (
            html.escape(case), len(runs),
            statistics.median(r["metrics"]["wall_processing_ns"]["p99"] for r in runs)/1e6,
            max(r["metrics"]["wall_processing_ns"]["max"] for r in runs)/1e6))
    environment = manifest.get("environment", {})
    summary = environment.get("system", {})
    page = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Linux scheduling lab report</title>
<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:1250px;margin:2em auto;padding:0 1em;color:#202b36;background:#fbfcfd}
h1,h2{line-height:1.2}table{border-collapse:collapse;width:100%%;font-size:.86em}
th,td{text-align:left;padding:.6em;border-bottom:1px solid #ccd3da;vertical-align:top}
th{background:#e8eef3}.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}
.note{border-left:4px solid #e7a330;padding:1em;background:#fff7e7}
.chart{display:grid;grid-template-columns: minmax(180px,1fr) 2fr;gap:1em;margin:.6em 0;font-size:.85em}
svg{width:100%%;max-height:40px}a{color:#145c94}
</style>
<h1>Linux scheduling lab</h1>
<p>%(title)s</p><p>%(system)s</p>
<p class="note">These are observed samples, not worst-case execution-time guarantees.
Synthetic audio misses are not hardware XRUNs. Short runs and hosted CI do not prove hard RT.
Offline response/lateness is cumulative from the shared release, not a periodic deadline.
Results include instrumentation overhead and real host interference.</p>
<h2>Individual runs</h2><div class="scroll"><table>
<tr><th>Run / raw CSV</th><th>Status</th><th>Samples</th><th>Wall ms p50 / p99 / max</th>
<th>Response p99 ms</th><th>Wake p99 ms</th><th>Misses</th><th>Jobs/s</th><th>Effective scheduler</th></tr>
%(rows)s</table></div>
<h2>Wall-processing distributions</h2>
<p>Common scale; gray = observed maximum, amber = p99, blue = median. Raw timestamps are in nanoseconds.</p>
%(charts)s
<h2>Repetitions (not pooled samples)</h2><table>
<tr><th>Case</th><th>Runs</th><th>Median of per-run p99 (ms)</th><th>Largest observation (ms)</th></tr>
%(repeats)s</table>
<h2>Provenance and parameters</h2><p>The run.json alongside this report is the machine-readable source.
Inspect source environment, skips, commands, cgroup counters and raw artifacts before drawing conclusions.</p>
<details><summary>Full manifest</summary><pre>%(manifest)s</pre></details>
</html>""" % {
        "title": html.escape(manifest.get("title", manifest.get("preset", "comparison")) +
                             " | status: " + manifest.get("status", "comparison")),
        "system": html.escape(json.dumps(summary, sort_keys=True)),
        "rows": "".join(rows), "charts": "".join(charts), "repeats": "".join(repeat_rows),
        "manifest": html.escape(json.dumps(manifest, indent=2, sort_keys=True)),
    }
    Path(path).write_text(page)


def compare(paths, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    combined = {"schema_version": 1, "preset": "comparison", "runs": [], "sources": [],
                "title": "Cross-run comparison: check equivalent parameters, binary and hardware before attribution."}
    for index, path in enumerate(paths):
        source = Path(path)
        if source.is_dir():
            source = source / "run.json"
        manifest = json.loads(source.read_text())
        if manifest.get("schema_version") != 1:
            raise ValueError("unsupported manifest schema: %s" % source)
        combined["sources"].append({"path": str(source.resolve()), "manifest": manifest})
        for original in manifest["runs"]:
            run = dict(original)
            run["label"] = "%d: %s" % (index+1, run["label"])
            run["case"] = "%d: %s" % (index+1, run["case"])
            if "artifact_prefix" in run:
                # Absolute file URI also works when the report is opened from a different directory.
                run["artifact_prefix"] = (source.parent / run["artifact_prefix"]).resolve().as_uri()
            combined["runs"].append(run)
    (output / "run.json").write_text(json.dumps(combined, indent=2) + "\n")
    write_report(combined, output / "report.html")
    return output
