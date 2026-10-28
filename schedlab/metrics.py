"""Raw integer nanoseconds in; explicitly named, per-run statistics out."""

import csv
import math
from pathlib import Path


def quantile(values, q):
    """Linear interpolation between sorted observations (including endpoints)."""
    if not values:
        raise ValueError("quantile requires at least one observation")
    if not 0 <= q <= 1:
        raise ValueError("q must be between zero and one")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def distribution(values):
    if not values:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {"n": len(values), "p50": quantile(values, .5),
            "p95": quantile(values, .95), "p99": quantile(values, .99), "max": max(values)}


def summarize(path, core):
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        expected = ["index", "release_ns", "start_ns", "finish_ns", "cpu_ns"]
        if reader.fieldnames != expected:
            raise ValueError("unexpected CSV columns")
        rows = [{key: int(value) for key, value in row.items()} for row in reader]
    if len(rows) != core["samples"] or (not rows and not core.get("window_ns")):
        raise ValueError("CSV sample count differs from native result")
    for index, row in enumerate(rows):
        if row["index"] != index or row["start_ns"] < row["release_ns"] or row["finish_ns"] < row["start_ns"]:
            raise ValueError("invalid timestamp order or sample index")
        if row["cpu_ns"] < 0:
            raise ValueError("negative CPU time")
    period = core["period_ns"]
    wall = [r["finish_ns"] - r["start_ns"] for r in rows]
    response = [r["finish_ns"] - r["release_ns"] for r in rows]
    wake = [r["start_ns"] - r["release_ns"] for r in rows]
    # Offline jobs share a release: response then means cumulative completion, not a periodic deadline.
    deadline = core.get("deadline", {}).get("deadline_us", 0) * 1000
    if core["effective"]["policy"] != "deadline":
        deadline = period
    misses = sum(value > deadline for value in response) if period else None
    elapsed = core.get("window_ns") or (rows[-1]["finish_ns"] - rows[0]["release_ns"])
    completed = sum(r["finish_ns"] <= rows[0]["release_ns"]+elapsed for r in rows) if rows else 0
    return {
        "samples": len(rows), "wall_processing_ns": distribution(wall),
        "cpu_processing_ns": distribution([r["cpu_ns"] for r in rows]),
        "response_ns": distribution(response), "wake_lateness_ns": distribution(wake),
        "deadline_misses": misses, "miss_fraction": misses / len(rows) if period else None,
        "elapsed_ns": elapsed, "cpu_total_ns": sum(r["cpu_ns"] for r in rows),
        "completed_within_window": completed,
        "throughput_jobs_s": completed * 1e9 / elapsed if elapsed else 0,
        "first_start_ns": rows[0]["start_ns"] if rows else None,
        "last_finish_ns": rows[-1]["finish_ns"] if rows else None,
    }
