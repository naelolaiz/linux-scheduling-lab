import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

from schedlab.analysis import (
    analyze,
    analyze_manifest,
    bar,
    compare_analyses,
    contention_values,
    load_values,
    render,
)
from schedlab.cgroups import Unavailable, cpu_groups
from schedlab.environment import capture, kernel_config, native_build, parse_cpu_list, read
from schedlab.metrics import distribution, quantile, summarize
from schedlab.report import build_summary, compare, write_report
from schedlab.runner import BINARY, LOAD_BINARY, LOAD_KINDS, NATIVE_DIR, ROOT, cases, run, run_group

PIPELINE = NATIVE_DIR / "thread-pipeline"


class MetricsTests(unittest.TestCase):
    def test_interpolation(self):
        self.assertEqual(quantile([10, 0, 20], 0.5), 10)
        self.assertAlmostEqual(quantile([0, 100], 0.95), 95)
        self.assertEqual(quantile([42], 0.99), 42)
        self.assertEqual(distribution([2, 8])["max"], 8)
        self.assertEqual(distribution([])["n"], 0)
        self.assertIsNone(distribution([])["p99"])

    def test_invalid_quantile(self):
        for values, q in (([], 0.5), ([1], -1), ([1], 2)):
            with self.assertRaises(ValueError):
                quantile(values, q)

    def test_exact_metrics_and_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.csv"
            path.write_text("index,release_ns,start_ns,finish_ns,cpu_ns\n0,100,102,110,5\n1,110,114,123,6\n")
            core = {"samples": 2, "period_ns": 10, "effective": {"policy": "other"}}
            metric = summarize(path, core)
            self.assertEqual(metric["deadline_misses"], 1)  # Equality is on time.
            self.assertEqual(metric["miss_fraction"], 0.5)
            self.assertEqual(metric["cpu_total_ns"], 11)
            self.assertEqual(metric["response_ns"]["max"], 13)
            core.update(window_ns=20, period_ns=0)
            self.assertEqual(summarize(path, core)["completed_within_window"], 1)
            core["samples"] = 3
            with self.assertRaises(ValueError):
                summarize(path, core)

    def test_zero_work_is_not_fake_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.csv"
            path.write_text("index,release_ns,start_ns,finish_ns,cpu_ns\n")
            core = {"samples": 0, "period_ns": 0, "window_ns": 100, "effective": {"policy": "idle"}}
            metric = summarize(path, core)
            self.assertEqual(metric["throughput_jobs_s"], 0)
            self.assertIsNone(metric["wall_processing_ns"]["max"])

    def test_deadline_uses_relative_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.csv"
            path.write_text("index,release_ns,start_ns,finish_ns,cpu_ns\n0,0,0,6000,4000\n")
            core = {
                "samples": 1,
                "period_ns": 10000,
                "deadline": {"deadline_us": 5},
                "effective": {"policy": "deadline"},
            }
            self.assertEqual(summarize(path, core)["deadline_misses"], 1)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name) / "native"
        self.cpu = min(os.sched_getaffinity(0))

    def invoke(self, *args):
        return subprocess.run(
            [str(BINARY), "--output", str(self.prefix), *args], text=True, capture_output=True, timeout=18
        )

    def success(self, *args):
        result = self.invoke(*args)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = json.loads(result.stdout)
        self.assertEqual(data["status"], "ok")
        return data

    def test_policy_affinity_nice_and_csv(self):
        for policy in ("other", "batch", "idle"):
            data = self.success("--policy", policy, "--nice", "5", "--cpu", str(self.cpu), "--jobs", "4")
            self.assertEqual(data["requested"]["policy"], policy)
            self.assertEqual(data["effective"]["policy"], policy)
            self.assertEqual(data["effective"]["nice"], 5)
            self.assertEqual(data["effective"]["affinity"], [self.cpu])
            self.assertEqual(data["samples"], 4)
            self.assertEqual(summarize(self.prefix.with_suffix(".csv"), data)["samples"], 4)

    def test_audio_media(self):
        data = self.success("--mode", "audio", "--jobs", "5", "--period-us", "1000")
        with wave.open(str(self.prefix.with_suffix(".wav")), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 48000))
            self.assertEqual(wav.getnframes(), 240)
            self.assertNotEqual(set(wav.readframes(240)), {0})
        self.assertEqual(data["period_ns"], 1_000_000)
        metric = summarize(self.prefix.with_suffix(".csv"), data)
        self.assertGreaterEqual(metric["deadline_misses"], 0)

    def test_video_media_and_fixed_work(self):
        data = self.success("--mode", "video", "--jobs", "2", "--period-us", "1000")
        image = self.prefix.with_suffix(".pgm").read_bytes()
        header = b"P5\n160 90\n255\n"
        self.assertTrue(image.startswith(header))
        self.assertEqual(len(image) - len(header), 160 * 90)
        self.assertGreater(data["checksum"], 0)

    def test_video_frame_independent_of_warmup(self):
        self.success("--mode", "video", "--jobs", "1", "--work", "1", "--warmup", "0")
        expected = self.prefix.with_suffix(".pgm").read_bytes()
        self.success("--mode", "video", "--jobs", "1", "--work", "1", "--warmup", "1")
        self.assertEqual(self.prefix.with_suffix(".pgm").read_bytes(), expected)

    def test_prewarm_changes_no_output(self):
        plain = self.success("--mode", "video", "--jobs", "2", "--cpu", str(self.cpu))
        frame = self.prefix.with_suffix(".pgm").read_bytes()
        warm = self.success("--mode", "video", "--jobs", "2", "--cpu", str(self.cpu), "--prewarm-ms", "50")
        self.assertEqual(warm["checksum"], plain["checksum"])
        self.assertEqual(self.prefix.with_suffix(".pgm").read_bytes(), frame)

    def test_contention_window(self):
        contention = self.success("--jobs", "5", "--cpu", str(self.cpu))["contention"]
        if contention["status"] != "ok":
            self.skipTest(contention["reason"])
        self.assertGreater(contention["window_ns"], 0)
        self.assertGreaterEqual(contention["runqueue_wait_ns"], 0)
        self.assertEqual(contention["cpu"], self.cpu)
        self.assertLessEqual(contention["run_ns"], contention["window_ns"])
        self.assertGreaterEqual(contention["cpu_steal_ns"], 0)
        self.assertEqual(len(contention["smt_siblings"]), len(contention["smt_siblings_busy_ns"]))

    def test_fault_probe(self):
        demand = self.success("--mode", "audio", "--jobs", "4", "--period-us", "1000", "--memory", "demand")
        prefault = self.success(
            "--mode", "audio", "--jobs", "4", "--period-us", "1000", "--memory", "prefault"
        )
        self.assertGreater(demand["minor_faults"], prefault["minor_faults"])
        self.assertEqual(demand["checksum"], prefault["checksum"])

    def test_invalid_options(self):
        invalid = (
            ("--jobs", "0"),
            ("--work", "nope"),
            ("--policy", "imaginary"),
            ("--nice", "20"),
            ("--priority", "99"),
            ("--mode", "lock"),
            ("--mode", "audio", "--duration-ms", "1"),
            ("--start-ns", "-1"),
            ("--mode", "audio", "--jobs", "20000", "--period-us", "100000"),
            ("--policy", "deadline", "--mode", "audio", "--cpu", str(self.cpu)),
            ("--policy", "deadline", "--mode", "audio", "--runtime-us", "9000"),
            ("--policy", "deadline", "--mode", "audio", "--deadline-us", "20000"),
            ("--unknown", "1"),
            ("--allow-rt=yes",),
            ("stray-argument",),
            ("--jobs",),
        )
        for args in invalid:
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_option_value_forms_and_help(self):
        data = self.success("--jobs=3", "--policy=batch", "--nice", "2")
        self.assertEqual(
            (data["samples"], data["effective"]["policy"], data["effective"]["nice"]), (3, "batch", 2)
        )
        result = subprocess.run([str(BINARY), "--help"], text=True, capture_output=True, timeout=18)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Exit codes", result.stdout)

    def test_build_info_matches_results(self):
        result = subprocess.run([str(BINARY), "--build-info"], text=True, capture_output=True, timeout=18)
        self.assertEqual(result.returncode, 0, result.stderr)
        build = json.loads(result.stdout)["build"]
        for key in ("compiler_id", "compiler_version", "build_type", "cxx_flags", "standard_library", "libc"):
            self.assertIsInstance(build[key], str, key)
        self.assertGreaterEqual(build["cxx_standard"], 202002)
        self.assertIsInstance(build["cross_compiled"], bool)
        self.assertEqual(self.success("--jobs", "1")["build"], build)

    def test_rt_never_silently_falls_back(self):
        for policy in ("fifo", "rr", "deadline"):
            result = self.invoke("--policy", policy, "--mode", "audio", "--jobs", "1")
            self.assertEqual(result.returncode, 77)
            self.assertEqual(json.loads(result.stdout)["status"], "skipped")
            self.assertIn("allow-rt", json.loads(result.stdout)["reason"])
            self.assertFalse(self.prefix.with_suffix(".csv").exists())

    def test_native_watchdog(self):
        begin = time.monotonic()
        result = self.invoke("--mode", "audio", "--jobs", "1000", "--timeout-ms", "100")
        self.assertEqual(result.returncode, 1)
        self.assertIn("watchdog", json.loads(result.stdout)["reason"])
        self.assertLess(time.monotonic() - begin, 3)

    @unittest.skipUnless(Path("/dev/full").exists(), "requires the Linux full device")
    def test_stdout_failure_is_not_success(self):
        with Path("/dev/full").open("wb") as full:
            result = subprocess.run(
                [str(BINARY), "--output", str(self.prefix), "--jobs", "1"],
                stdout=full,
                stderr=subprocess.PIPE,
                text=True,
                timeout=18,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("standard output", result.stderr)

    def test_requested_policy_in_permission_skip(self):
        result = self.invoke("--nice", "-20", "--jobs", "1")
        if result.returncode == 0:
            self.assertEqual(json.loads(result.stdout)["effective"]["nice"], -20)
        else:
            self.assertEqual(result.returncode, 77, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["status"], "skipped")

    @unittest.skipUnless(os.environ.get("SCHEDLAB_TEST_RT") == "1", "opt-in RT checks disabled")
    def test_opt_in_rt(self):
        if len(os.sched_getaffinity(0)) < 2:
            self.skipTest("need two allowed CPUs")
        for policy in ("fifo", "rr"):
            data = self.success(
                "--policy", policy, "--cpu", str(self.cpu), "--mode", "audio", "--jobs", "5", "--allow-rt"
            )
            self.assertEqual(data["effective"]["priority"], 10)
        waits = {}
        for protocol in ("none", "inherit"):
            data = self.success(
                "--mode",
                "lock",
                "--policy",
                "fifo",
                "--cpu",
                str(self.cpu),
                "--allow-rt",
                "--protocol",
                protocol,
            )
            waits[protocol] = data["lock"]
        self.assertLess(waits["none"]["medium_finish_ns"], waits["none"]["low_finish_ns"])
        self.assertGreater(waits["inherit"]["medium_finish_ns"], waits["inherit"]["low_finish_ns"])

    @unittest.skipUnless(os.environ.get("SCHEDLAB_TEST_DEADLINE") == "1", "opt-in DEADLINE check disabled")
    def test_opt_in_deadline(self):
        data = self.success("--mode", "audio", "--policy", "deadline", "--jobs", "5", "--allow-rt")
        self.assertEqual(data["effective"]["affinity"], sorted(os.sched_getaffinity(0)))
        self.assertEqual(data["effective"]["runtime_ns"], 1_000_000)


class HarnessTests(unittest.TestCase):
    def test_case_contract(self):
        self.assertNotIn("cpu", cases("deadline", 1, 2)[0][0])
        for preset in (
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
        ):
            groups = cases(preset, 1, 2)
            labels = [spec["case"] for group in groups for spec in group]
            self.assertEqual(len(labels), len(set(labels)), preset)
            self.assertLessEqual(max(map(len, groups)), 3)

    def test_run_report_and_compare(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "quick"
            manifest = run("quick", output, repetitions=1)
            self.assertEqual([r["status"] for r in manifest["runs"]], ["ok"] * 3)
            self.assertEqual(manifest["environment"]["system"]["architecture"], os.uname().machine)
            self.assertTrue((output / "run.json").is_file())
            text = (output / "report.html").read_text()
            self.assertIn("not hardware XRUNs", text)
            self.assertIn("Schedlab / Result explorer", text)
            self.assertIn('id="ranking"', text)
            self.assertIn("const REPORT_DATA =", text)
            self.assertNotRegex(text, r"@@[A-Z0-9_]+@@")
            self.assertNotIn("<script src=", text)
            manifest["title"] = "<script>alert(1)</script>"
            manifest["runs"][0]["artifact_prefix"] = "javascript:alert(1)"
            write_report(manifest, root / "escaped.html")
            escaped = (root / "escaped.html").read_text()
            self.assertIn("&lt;script&gt;", escaped)
            self.assertNotIn("<script>alert(1)</script>", escaped)
            self.assertNotIn("href='javascript:", escaped)
            self.assertIn('id="findings-title"', text)
            self.assertIn("of jobs finished before the next release", text)
            compare([output, output / "run.json"], root / "comparison")
            combined = json.loads((root / "comparison/run.json").read_text())
            self.assertEqual(len(combined["runs"]), 6)
            self.assertTrue(combined["runs"][0]["artifact_prefix"].startswith("file:"))
            result = analyze([output, output / "run.json"])
            self.assertEqual(
                [row["case"] for row in result["comparison"]["tables"][0]["rows"]],
                ["offline", "audio", "video"],
            )
            self.assertIn("Comparison across runs", render(result, "text"))
            self.assertIn("<details>", render(result, "markdown"))
            self.assertEqual(json.loads(render(result, "json"))["schema_version"], 1)
            from schedlab.__main__ import main

            self.assertEqual(
                main(["analyze", str(output), "--out", str(root / "analysis.md"), "--format", "markdown"]), 0
            )
            self.assertIn("| audio |", (root / "analysis.md").read_text())
            with self.assertRaises(FileExistsError):
                run("quick", output, repetitions=1)

    def test_cgroup_no_delegation_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = run("cgroup", Path(directory) / "result", repetitions=1)
            self.assertEqual(manifest["runs"][0]["status"], "skipped")
            with self.assertRaises(Unavailable), cpu_groups(directory, [{"cpu.weight": "100"}]):
                self.fail("ordinary directory accepted as cgroup")
            for name, value in (
                ("cgroup.controllers", "cpu"),
                ("cgroup.subtree_control", "cpu"),
                ("cgroup.type", "domain"),
            ):
                (Path(directory) / name).write_text(value)
            with self.assertRaises(Unavailable), cpu_groups(directory, [{"cpu.weight": "100"}]):
                self.fail("lookalike files must not masquerade as real kernel controls")

    def test_harness_timeout_reaps_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "hang"
            pidfile = root / "child"
            # The parent records the child at once; the timeout leaves room for a slow, loaded machine.
            script.write_text(
                f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\n"
                "pid=os.fork()\n"
                f"if pid:\n Path({str(pidfile)!r}).write_text(str(pid))\n"
                "time.sleep(30)\n"
            )
            script.chmod(0o700)
            records = run_group([{"case": "hang"}], root, 1, binary=script, timeout=2)
            self.assertEqual(records[0]["status"], "error")
            self.assertIn("timeout", records[0]["reason"])
            pid = int(pidfile.read_text())
            state = Path(f"/proc/{pid}/stat")
            if state.exists():
                self.assertEqual(state.read_text().split()[2], "Z")  # Exited; awaiting init reaping.

    def test_project_version_matches_manifest_version(self):
        from schedlab import __version__

        text = (ROOT / "pyproject.toml").read_text()
        self.assertIn(f'\nversion = "{__version__}"\n', text)

    def test_cli_invalid(self):
        result = subprocess.run(
            [sys.executable, "-m", "schedlab", "run", "quick", "--repetitions", "0"],
            cwd=ROOT,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2)

    def test_replay_does_not_inherit_privilege_or_commands(self):
        from schedlab.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            source.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "preset": "deadline",
                        "repetitions": 1,
                        "selected_cpu": min(os.sched_getaffinity(0)),
                        "allow_rt": True,
                        "runs": [{"command": ["do-not-execute"]}],
                    }
                )
            )
            self.assertEqual(main(["replay", str(source), "--out", str(root / "output")]), 0)
            manifest = json.loads((root / "output/run.json").read_text())
            self.assertFalse(manifest["allow_rt"])
            self.assertEqual(manifest["runs"][0]["status"], "skipped")
            source.write_text(json.dumps({"schema_version": 1, "preset": "quick", "repetitions": 100000}))
            self.assertEqual(main(["replay", str(source), "--out", str(root / "bad")]), 1)

    def test_interrupt_manifest_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "interrupted"
            with (
                patch("schedlab.runner.run_group", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                run("quick", output, repetitions=1)
            self.assertEqual(json.loads((output / "run.json").read_text())["status"], "interrupted")
            self.assertIn("interrupted", (output / "report.html").read_text())

    @unittest.skipUnless(os.environ.get("SCHEDLAB_CGROUP_PARENT"), "opt-in cgroup delegation check disabled")
    def test_opt_in_cgroup(self):
        parent = Path(os.environ["SCHEDLAB_CGROUP_PARENT"])
        before = set(parent.iterdir())
        with tempfile.TemporaryDirectory() as directory:
            manifest = run("cgroup", Path(directory) / "run", repetitions=1, parent=parent)
            self.assertEqual([r["status"] for r in manifest["runs"]], ["ok"] * 4)
        self.assertEqual(set(parent.iterdir()), before)


def synthetic_run(case, repetition=1, policy="other", nice=0, mode="offline", core=None, **metrics):
    effective = {"policy": policy, "nice": nice, "rr_quantum_ns": 100_000_000}
    return {
        "case": case,
        "label": f"{case}-r{repetition}",
        "repetition": repetition,
        "status": "ok",
        "core": {"mode": mode, "effective": effective, **(core or {})},
        "metrics": metrics,
    }


class AnalysisTests(unittest.TestCase):
    def test_fairness_share_against_load_weights(self):
        manifest = {
            "schema_version": 1,
            "preset": "fairness",
            "repetitions": 1,
            "runs": [
                synthetic_run("fair-other-10-control", cpu_total_ns=900, throughput_jobs_s=90),
                synthetic_run("fair-other-10-candidate", nice=10, cpu_total_ns=100, throughput_jobs_s=10),
                synthetic_run("fair-idle-0-control", cpu_total_ns=995),
                synthetic_run("fair-idle-0-candidate", policy="idle", cpu_total_ns=5),
            ],
        }
        analysis = analyze_manifest(manifest)
        shares = {f["data"]["case"]: f["data"] for f in analysis["findings"] if "case" in f["data"]}
        self.assertAlmostEqual(shares["fair-other-10-candidate"]["measured_share"], 0.1)
        self.assertAlmostEqual(shares["fair-other-10-candidate"]["model_share"], 110 / (110 + 1024))
        self.assertAlmostEqual(shares["fair-idle-0-candidate"]["model_share"], 3 / (3 + 1024))
        self.assertTrue(any("1 repetition" in caveat for caveat in analysis["caveats"]))
        self.assertEqual(analysis["cases"][0]["metric"]["key"], "throughput_jobs_s")

    def test_priority_inversion_and_sharing_order(self):
        def lock(low, medium):
            return {"lock": {"low_finish_ns": low, "medium_finish_ns": medium, "high_wait_ns": 4_000_000}}

        locks = analyze_manifest(
            {
                "schema_version": 1,
                "preset": "locks",
                "runs": [
                    synthetic_run("lock-none", mode="lock", core=lock(200, 100)),
                    synthetic_run("lock-inherit", mode="lock", core=lock(100, 200)),
                ],
            }
        )
        self.assertEqual([f["data"]["inverted"] for f in locks["findings"]], [1, 0])
        sharing = analyze_manifest(
            {
                "schema_version": 1,
                "preset": "sharing",
                "runs": [
                    synthetic_run("sharing-fifo-0", first_start_ns=0, last_finish_ns=10_000_000),
                    synthetic_run("sharing-fifo-1", first_start_ns=10_500_000, last_finish_ns=20_000_000),
                    synthetic_run("sharing-rr-0", first_start_ns=0, last_finish_ns=19_000_000),
                    synthetic_run("sharing-rr-1", first_start_ns=1_000_000, last_finish_ns=20_000_000),
                ],
            }
        )
        overlap = {f["data"]["policy"]: f["data"]["overlap_ms"] for f in sharing["findings"]}
        self.assertLess(overlap["fifo"], 0)
        self.assertAlmostEqual(overlap["rr"], 18.0)

    def test_cgroup_weight_and_quota(self):
        def group(case, control, **metrics):
            run = synthetic_run(case, **metrics)
            run["cgroup"] = {"requested": control}
            return run

        analysis = analyze_manifest(
            {
                "schema_version": 1,
                "preset": "cgroup",
                "runs": [
                    group("cgroup-weight-0", {"cpu.weight": "100"}, cpu_total_ns=200),
                    group("cgroup-weight-1", {"cpu.weight": "400"}, cpu_total_ns=800),
                    group("cgroup-quota-1", {"cpu.max": "20000 100000"}, cpu_total_ns=19, elapsed_ns=100),
                ],
            }
        )
        weight, quota = analysis["findings"]
        self.assertAlmostEqual(weight["data"]["measured_share"], 0.8)
        self.assertAlmostEqual(weight["data"]["model_share"], 0.8)
        self.assertAlmostEqual(quota["data"]["used"], 0.19)

    def test_caveats_rendering_and_differences(self):
        build = {
            "compiler_id": "GNU",
            "compiler_version": "13",
            "build_type": "RelWithDebInfo",
            "sanitize": "thread",
        }
        manifest = {
            "schema_version": 1,
            "preset": "quick",
            "repetitions": 3,
            "environment": {"native_build": build},
            "runs": [synthetic_run("a|b", mode="audio", response_ns={"p99": 2e6}, miss_fraction=0.5)],
        }
        first = analyze_manifest(manifest, "one")
        self.assertTrue(any("not measurements" in caveat for caveat in first["caveats"]))
        markdown = render({"schema_version": 1, "runs": [first], "comparison": None}, "markdown")
        self.assertIn("a\\|b", markdown)
        second = dict(first, build="Clang 18 RelWithDebInfo")
        self.assertIn("native build", compare_analyses([first, second])["differences"])
        self.assertEqual(bar(5, 10, width=4), "██")
        self.assertEqual(bar(1, 80, width=1), "▏")
        self.assertEqual(bar(None, 10), "")

    def test_rt_policies_and_background_cpu_time(self):
        def audio(policy, p99_ms):
            return synthetic_run(
                f"rt-audio-{policy}", policy=policy, mode="audio", response_ns={"p99": p99_ms * 1e6}
            )

        def background(policy, jobs_s):
            return synthetic_run(
                f"rt-background-{policy}", throughput_jobs_s=jobs_s, cpu_total_ns=582e6, elapsed_ns=600e6
            )

        analysis = analyze_manifest(
            {
                "schema_version": 1,
                "preset": "rt",
                "runs": [
                    audio("other", 0.8),
                    background("other", 1277),
                    audio("fifo", 0.09),
                    background("fifo", 1823),
                    audio("rr", 0.07),
                    background("rr", 1828),
                ],
            }
        )
        rt, contender = (finding["text"] for finding in analysis["findings"])
        self.assertIn("FIFO and RR behave the same", rt)
        self.assertNotIn("Lowest tail", rt)
        self.assertIn("97% of a CPU", contender)
        self.assertIn("speed per CPU-second", contender)

    def test_lowest_tail_only_when_ranges_are_apart(self):
        def mixed(policy, *p99_ms):
            return [
                synthetic_run(
                    f"mixed-audio-{policy}", repetition, mode="audio", response_ns={"p99": value * 1e6}
                )
                for repetition, value in enumerate(p99_ms, 1)
            ]

        overlapping = analyze_manifest(
            {
                "schema_version": 1,
                "preset": "mixed",
                "runs": [*mixed("other", 1.0, 2.0), *mixed("idle", 1.5, 2.5)],
            }
        )
        self.assertIn("none is clearly lowest", overlapping["findings"][0]["text"])
        apart = analyze_manifest(
            {
                "schema_version": 1,
                "preset": "mixed",
                "runs": [*mixed("other", 2.0, 2.2), *mixed("idle", 1.0, 1.1)],
            }
        )
        self.assertIn("Lowest tail: idle background", apart["findings"][0]["text"])

    def test_busy_machine_and_frequency_caveats(self):
        def ok(value):
            return {"status": "ok", "value": value}

        def environment(loadavg):
            pressure = (
                "some avg10=30.00 avg60=25.50 avg300=10.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00"
            )
            return {
                "runtime": {"/sys/devices/system/cpu/online": ok("0-15")},
                "load": {"loadavg": ok(loadavg), "cpu_pressure": ok(pressure)},
                "cpu_topology": {
                    "0": {
                        "cpufreq/scaling_governor": ok("powersave"),
                        "cpufreq/scaling_driver": ok("amd-pstate-epp"),
                        "cpufreq/energy_performance_preference": ok("balance_performance"),
                    }
                },
            }

        def caveats(before, after):
            manifest = {
                "schema_version": 1,
                "preset": "quick",
                "repetitions": 3,
                "selected_cpu": 0,
                "environment": environment(before),
                "environment_after": {"load": environment(after)["load"]},
                "runs": [],
            }
            return analyze_manifest(manifest)["caveats"]

        busy = caveats("9.10 7.00 5.00 17/900 1", "8.00 7.00 5.00 2/900 1")
        expected = "16 other tasks were runnable just before the run and 1 other task was runnable just after"
        self.assertTrue(any(expected in c and "25.50%" in c for c in busy), busy)
        self.assertFalse(
            any("Other work" in c for c in caveats("0.30 0.20 0.10 2/900 1", "0.40 0.20 0.10 3/900 1"))
        )
        self.assertTrue(any("driver amd-pstate-epp, EPP balance_performance" in c for c in busy))


class EnvironmentTests(unittest.TestCase):
    def test_missing_is_explicit(self):
        self.assertEqual(read("/definitely-not-a-schedlab-file")["status"], "unavailable")
        with (
            patch("schedlab.environment.gzip.open", side_effect=PermissionError("denied")),
            patch("schedlab.environment.Path.read_text", side_effect=PermissionError("denied")),
        ):
            config = kernel_config("test-kernel")
        self.assertEqual(config["status"], "unavailable")
        self.assertEqual(config["options"]["CONFIG_PREEMPT_RT"], "unknown")

    def test_environment_shapes(self):
        data = capture(BINARY)
        self.assertEqual(data["allowed_cpus"], sorted(os.sched_getaffinity(0)))
        self.assertIn("CONFIG_PREEMPT_DYNAMIC", data["kernel_config"]["options"])
        self.assertIn("RLIMIT_RTTIME", data["limits"])
        self.assertEqual(len(data["binary_sha256"]), 64)
        self.assertIn("ancestors", data["cgroup"])
        self.assertIn("loadavg", data["load"])
        self.assertTrue(data["native_build"]["compiler_id"])
        self.assertIn(data["native_build"]["compiler_id"], build_summary(data["native_build"]))

    def test_unreadable_native_build_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "not-sched-work"
            script.write_text(f"#!{sys.executable}\nprint('not json')\n")
            script.chmod(0o700)
            build = native_build(script)
        self.assertEqual(build["status"], "unavailable")
        self.assertEqual(build_summary(build), "unknown native build")


class PipelineTests(unittest.TestCase):
    def test_bounded_pipeline_and_media(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.wav"
            for delay in (0, 50):
                result = subprocess.run(
                    [
                        str(PIPELINE),
                        "--output",
                        str(path),
                        "--buffers",
                        "20",
                        "--consumer-delay-ms",
                        str(delay),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["consumed"] + data["dropped"], 20)
                self.assertFalse(data["device_xruns_measured"])
                with wave.open(str(path), "rb") as wav:
                    self.assertEqual(wav.getnframes(), 20 * 480)
                    self.assertEqual(wav.getframerate(), 48000)
                    self.assertNotEqual(set(wav.readframes(480)), {0})

    def test_pipeline_bounds(self):
        for value in ("0", "101", "not-a-number"):
            result = subprocess.run(
                [str(PIPELINE), "--output", "/dev/null", "--buffers", value],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 2)

    @unittest.skipUnless(Path("/dev/full").exists(), "requires the Linux full device")
    def test_pipeline_write_failures_are_not_success(self):
        media_failure = subprocess.run(
            [str(PIPELINE), "--output", "/dev/full", "--buffers", "1"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(media_failure.returncode, 1)

        with tempfile.TemporaryDirectory() as directory, Path("/dev/full").open("wb") as full:
            output_failure = subprocess.run(
                [str(PIPELINE), "--output", str(Path(directory) / "ok.wav"), "--buffers", "1"],
                stdout=full,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
        self.assertEqual(output_failure.returncode, 1)
        self.assertIn("standard output", output_failure.stderr)


class LoadGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name) / "load"
        self.cpu = min(os.sched_getaffinity(0))

    def load(self, *args, timeout=18):
        return subprocess.run([str(LOAD_BINARY), *args], text=True, capture_output=True, timeout=timeout)

    def test_every_kind_does_bounded_work(self):
        for kind in LOAD_KINDS:
            with self.subTest(kind=kind):
                result = self.load(
                    "--kind",
                    kind,
                    "--cpus",
                    str(self.cpu),
                    "--max-duration-ms",
                    "200",
                    "--output",
                    str(self.prefix),
                    "--memory-mb",
                    "16",
                    "--io-mb",
                    "8",
                    "--io-mb-per-s",
                    "8",
                )
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                data = json.loads(result.stdout)
                self.assertEqual((data["status"], data["kind"], data["cpus"]), ("ok", kind, [self.cpu]))
                self.assertGreater(data["work"]["count"], 0)
                self.assertGreaterEqual(data["elapsed_ns"], 190_000_000)
                values = load_values(data)
                self.assertGreater(values["load_percent"], 0)
                # Only I/O load reports a write rate; it is what that kind is for.
                if kind == "io":
                    self.assertGreater(values["write_mib_s"], 0)
                else:
                    self.assertIsNone(values["write_mib_s"])
        self.assertEqual(list(Path(self.temp.name).glob("*.tmp")), [])

    def test_sigterm_stops_and_reports(self):
        process = subprocess.Popen(
            [str(LOAD_BINARY), "--kind", "cpu", "--cpus", str(self.cpu)], stdout=subprocess.PIPE, text=True
        )
        time.sleep(0.2)
        begin = time.monotonic()
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=5)
        self.assertLess(time.monotonic() - begin, 2)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(json.loads(stdout)["status"], "ok")

    def test_refusals(self):
        skipped = self.load("--kind", "rt", "--policy", "fifo", "--cpus", str(self.cpu))
        self.assertEqual(skipped.returncode, 77)
        self.assertIn("allow-rt", json.loads(skipped.stdout)["reason"])
        for args in (
            ("--kind", "cpu", "--policy", "fifo", "--cpus", str(self.cpu)),  # RT only for the rt kind
            ("--kind", "rt", "--cpus", str(self.cpu)),  # the rt kind needs an RT policy
            ("--kind", "cpu"),  # no CPUs
            ("--kind", "io", "--cpus", str(self.cpu)),  # no file prefix
            ("--kind", "cpu", "--cpus", "1023"),  # outside the allowed mask
            ("--kind", "cpu", "--cpus", "3-1"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.load(*args).returncode, 2)
        self.assertEqual(self.load("--kind", "noop").returncode, 0)

    @unittest.skipUnless(os.environ.get("SCHEDLAB_TEST_RT") == "1", "opt-in RT checks disabled")
    def test_opt_in_rt_load(self):
        allowed = sorted(os.sched_getaffinity(0))
        if len(allowed) < 2:
            self.skipTest("need two allowed CPUs")
        result = self.load(
            "--kind",
            "rt",
            "--policy",
            "fifo",
            "--priority",
            "5",
            "--allow-rt",
            "--cpus",
            str(allowed[-1]),
            "--max-duration-ms",
            "200",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreater(json.loads(result.stdout)["work"]["count"], 0)


class LoadScenarioTests(unittest.TestCase):
    def test_load_case_contract(self):
        for preset in ("load-audio", "load-offline", "load-placement", "load-priority", "load-external"):
            for allow_rt, siblings in ((False, []), (True, [1])):
                with self.subTest(preset=preset, allow_rt=allow_rt):
                    groups = cases(preset, 0, 2, [0, 1, 2, 3], siblings, allow_rt, 16)
                    specs = [spec for group in groups for spec in group]
                    names = [spec["case"] for spec in specs]
                    self.assertEqual(len(names), len(set(names)))
                    self.assertLessEqual(max(map(len, groups)), 3)
                    measured = {
                        spec["case"] for spec in specs if "generator" not in spec and "skip" not in spec
                    }
                    for spec in specs:
                        generator = spec.get("generator")
                        if generator:
                            self.assertIn(generator["baseline"], measured)
                            self.assertTrue(generator["cpus"])
                            self.assertTrue(allow_rt or generator["kind"] != "rt")
                    sibling_skips = [s for s in specs if "skip" in s and "sibling" in s["skip"]]
                    self.assertEqual(bool(sibling_skips), preset == "load-placement" and not siblings)

    def test_run_group_records_load_and_failures(self):
        cpu = min(os.sched_getaffinity(0))
        measured = dict(case="offline", mode="offline", cpu=cpu, jobs=80, work=1, duration_ms=200)
        generator = dict(kind="cpu", placement="same", cpus=[cpu], baseline="offline-idle", session="own")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = run_group([measured, dict(case="offline-load", generator=generator)], root, 1)
            work, load = records
            self.assertEqual(
                (work["status"], work["load_case"], load["role"], load["status"]),
                ("ok", "offline-load", "load", "ok"),
            )
            self.assertGreater(load_values(load["core"])["load_percent"], 0)
            broken = dict(generator, cpus=[1023])
            work, load = run_group([measured, dict(case="offline-load", generator=broken)], root, 2)
            self.assertEqual((work["status"], load["status"]), ("error", "error"))
            self.assertIn("load generator", work["reason"])
            skipped = run_group([dict(case="x", skip="not here")], root, 3)
            self.assertEqual(skipped[0]["status"], "skipped")

    def test_cpu_lists(self):
        self.assertEqual(parse_cpu_list("0-2,8"), [0, 1, 2, 8])
        self.assertEqual(parse_cpu_list(""), [])


def load_run(case, baseline, placement="same", policy="other", nice=0, session="own"):
    generator = dict(
        kind="cpu",
        placement=placement,
        cpus=[1],
        baseline=baseline,
        policy=policy,
        nice=nice,
        session=session,
    )
    return {
        "case": case,
        "label": case,
        "repetition": 1,
        "status": "ok",
        "role": "load",
        "parameters": {"case": case, "generator": generator},
        "core": {"cpu_ns": 900, "children_cpu_ns": 0, "elapsed_ns": 1000, "cpus": [1]},
    }


def contended_run(case, throughput, share, sibling, wait_ns=0, others_ns=0, load_case=None):
    run = synthetic_run(
        case,
        throughput_jobs_s=throughput,
        cpu_total_ns=share * 1e9,
        elapsed_ns=1e9,
        core={
            "contention": {
                "status": "ok",
                "window_ns": 1e9,
                "runqueue_wait_ns": wait_ns,
                "cpu_others_run_ns": others_ns,
                "smt_siblings_busy_ns": [sibling * 1e9],
            }
        },
    )
    run["group_cases"] = [case] if load_case is None else [case, load_case]
    if load_case:
        run["load_case"] = load_case
    return run


class LoadAnalysisTests(unittest.TestCase):
    def test_sibling_load_slows_without_taking_cpu_time(self):
        analysis = analyze_manifest(
            {
                "schema_version": 1,
                "preset": "load-placement",
                "repetitions": 3,
                "runs": [
                    contended_run("offline-idle", 1000, 1.0, 0.0),
                    contended_run("offline-cpu-sibling", 800, 1.0, 1.0, load_case="offline-cpu-sibling-load"),
                    load_run("offline-cpu-sibling-load", "offline-idle", placement="sibling"),
                ],
            }
        )
        texts = [finding["text"] for finding in analysis["findings"]]
        self.assertTrue(any("cut its speed to 0.80× per CPU-second" in text for text in texts), texts)
        self.assertTrue(any("SMT sibling busy 0% → 100%" in text for text in texts), texts)

    def test_priority_model_and_session(self):
        runs = [contended_run("offline-idle", 2000, 1.0, 0.0)]
        for session, share in (("same", 0.98), ("own", 0.97)):
            case = f"offline-nice19-{session}"
            runs += [
                contended_run(case, 1960, share, 0.0, others_ns=2e7, load_case=case + "-load"),
                load_run(case + "-load", "offline-idle", nice=19, session=session),
            ]
        manifest = {
            "schema_version": 1,
            "preset": "load-priority",
            "repetitions": 3,
            "runs": runs,
            "environment": {
                "runtime": {"/proc/sys/kernel/sched_autogroup_enabled": {"status": "ok", "value": "1"}}
            },
        }
        findings = analyze_manifest(manifest)["findings"]
        self.assertAlmostEqual(findings[0]["data"]["model_share"], 1024 / (1024 + 15))
        self.assertIn("made no difference", findings[1]["text"])

    def test_alone_case_sharing_its_core_is_flagged(self):
        manifest = {
            "schema_version": 1,
            "preset": "load-external",
            "repetitions": 3,
            "runs": [contended_run("offline", 1000, 0.6, 0.9, wait_ns=4e8, others_ns=4e8)],
        }
        analysis = analyze_manifest(manifest)
        self.assertTrue(
            any("met other work on their core" in c and "offline" in c for c in analysis["caveats"])
        )
        self.assertIn("other work ran 40% of the measured CPU", analysis["findings"][0]["text"])

    def test_short_windows_report_no_tick_sampled_shares(self):
        # 26 ms is under three 10 ms ticks: busy time that coarse cannot give a share.
        run = contended_run("offline", 1000, 1.0, 1.0, wait_ns=3e5)
        run["core"]["contention"].update(window_ns=26e6, cpu_others_run_ns=15.6e6, tick_ns=1e7)
        values = contention_values(run["core"])
        self.assertEqual((values["others_share"], values["sibling_busy_share"]), (None, None))
        self.assertAlmostEqual(values["wait_ms"], 0.3)
        manifest = {"schema_version": 1, "preset": "quick", "repetitions": 3, "runs": [run]}
        self.assertFalse(any("met other work" in caveat for caveat in analyze_manifest(manifest)["caveats"]))


if __name__ == "__main__":
    unittest.main()
