import csv
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

from schedlab.cgroups import Unavailable, cpu_groups
from schedlab.environment import capture, kernel_config, read
from schedlab.metrics import distribution, quantile, summarize
from schedlab.report import compare, write_report
from schedlab.runner import BINARY, ROOT, cases, run, run_group


class MetricsTests(unittest.TestCase):
    def test_interpolation(self):
        self.assertEqual(quantile([10, 0, 20], .5), 10)
        self.assertAlmostEqual(quantile([0, 100], .95), 95)
        self.assertEqual(quantile([42], .99), 42)
        self.assertEqual(distribution([2, 8])["max"], 8)
        self.assertEqual(distribution([])["n"], 0)
        self.assertIsNone(distribution([])["p99"])

    def test_invalid_quantile(self):
        for values, q in (([], .5), ([1], -1), ([1], 2)):
            with self.assertRaises(ValueError):
                quantile(values, q)

    def test_exact_metrics_and_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.csv"
            path.write_text("index,release_ns,start_ns,finish_ns,cpu_ns\n"
                            "0,100,102,110,5\n1,110,114,123,6\n")
            core = {"samples": 2, "period_ns": 10, "effective": {"policy": "other"}}
            metric = summarize(path, core)
            self.assertEqual(metric["deadline_misses"], 1)  # Equality is on time.
            self.assertEqual(metric["miss_fraction"], .5)
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
            core = {"samples": 0, "period_ns": 0, "window_ns": 100,
                    "effective": {"policy": "idle"}}
            metric = summarize(path, core)
            self.assertEqual(metric["throughput_jobs_s"], 0)
            self.assertIsNone(metric["wall_processing_ns"]["max"])

    def test_deadline_uses_relative_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.csv"
            path.write_text("index,release_ns,start_ns,finish_ns,cpu_ns\n0,0,0,6000,4000\n")
            core = {"samples": 1, "period_ns": 10000, "deadline": {"deadline_us": 5},
                    "effective": {"policy": "deadline"}}
            self.assertEqual(summarize(path, core)["deadline_misses"], 1)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name) / "native"
        self.cpu = min(os.sched_getaffinity(0))

    def invoke(self, *args):
        return subprocess.run([str(BINARY), "--output", str(self.prefix), *args],
                              text=True, capture_output=True, timeout=18)

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
        self.assertEqual(len(image)-len(header), 160*90)
        self.assertGreater(data["checksum"], 0)

    def test_fault_probe(self):
        demand = self.success("--mode", "audio", "--jobs", "4", "--period-us", "1000", "--memory", "demand")
        prefault = self.success("--mode", "audio", "--jobs", "4", "--period-us", "1000", "--memory", "prefault")
        self.assertGreater(demand["minor_faults"], prefault["minor_faults"])
        self.assertEqual(demand["checksum"], prefault["checksum"])

    def test_invalid_options(self):
        invalid = (("--jobs", "0"), ("--work", "nope"), ("--policy", "imaginary"),
                   ("--nice", "20"), ("--priority", "99"), ("--mode", "lock"),
                   ("--mode", "audio", "--duration-ms", "1"), ("--start-ns", "-1"),
                   ("--mode", "audio", "--jobs", "20000", "--period-us", "100000"),
                   ("--policy", "deadline", "--mode", "audio", "--cpu", str(self.cpu)),
                   ("--policy", "deadline", "--mode", "audio", "--runtime-us", "9000"),
                   ("--policy", "deadline", "--mode", "audio", "--deadline-us", "20000"))
        for args in invalid:
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

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
        self.assertLess(time.monotonic()-begin, 3)

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
            data = self.success("--policy", policy, "--cpu", str(self.cpu), "--mode", "audio",
                                "--jobs", "5", "--allow-rt")
            self.assertEqual(data["effective"]["priority"], 10)
        waits = {}
        for protocol in ("none", "inherit"):
            data = self.success("--mode", "lock", "--policy", "fifo", "--cpu", str(self.cpu),
                                "--allow-rt", "--protocol", protocol)
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
        for preset in ("quick", "offline", "fairness", "audio", "video", "mixed", "affinity",
                       "rt", "sharing", "deadline", "locks", "memory"):
            groups = cases(preset, 1, 2)
            labels = [spec["case"] for group in groups for spec in group]
            self.assertEqual(len(labels), len(set(labels)), preset)
            self.assertLessEqual(max(map(len, groups)), 3)

    def test_run_report_and_compare(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "quick"
            manifest = run("quick", output, repetitions=1)
            self.assertEqual([r["status"] for r in manifest["runs"]], ["ok"]*3)
            self.assertEqual(manifest["environment"]["system"]["architecture"], os.uname().machine)
            self.assertTrue((output / "run.json").is_file())
            text = (output / "report.html").read_text()
            self.assertIn("not hardware XRUNs", text)
            self.assertNotIn("<script src=", text)
            manifest["title"] = "<script>alert(1)</script>"
            write_report(manifest, root / "escaped.html")
            self.assertIn("&lt;script&gt;", (root / "escaped.html").read_text())
            compare([output, output / "run.json"], root / "comparison")
            combined = json.loads((root / "comparison/run.json").read_text())
            self.assertEqual(len(combined["runs"]), 6)
            self.assertTrue(combined["runs"][0]["artifact_prefix"].startswith("file:"))
            with self.assertRaises(FileExistsError):
                run("quick", output, repetitions=1)

    def test_cgroup_no_delegation_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = run("cgroup", Path(directory) / "result", repetitions=1)
            self.assertEqual(manifest["runs"][0]["status"], "skipped")
            with self.assertRaises(Unavailable):
                with cpu_groups(directory, [{"cpu.weight": "100"}]):
                    self.fail("ordinary directory accepted as cgroup")

    def test_harness_timeout_reaps_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "hang"
            pidfile = root / "child"
            script.write_text("#!%s\nimport os,time\nfrom pathlib import Path\n"
                              "pid=os.fork()\n"
                              "if pid == 0:\n Path(%r).write_text(str(os.getpid()))\n"
                              "time.sleep(30)\n" % (sys.executable, str(pidfile)))
            script.chmod(0o700)
            records = run_group([{"case": "hang"}], root, 1, binary=script, timeout=.3)
            self.assertEqual(records[0]["status"], "error")
            self.assertIn("timeout", records[0]["reason"])
            pid = int(pidfile.read_text())
            state = Path("/proc/%d/stat" % pid)
            if state.exists():
                self.assertEqual(state.read_text().split()[2], "Z")  # Exited; awaiting init reaping.

    def test_cli_invalid(self):
        result = subprocess.run([sys.executable, "-m", "schedlab", "run", "quick", "--repetitions", "0"],
                                cwd=ROOT, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)

    def test_replay_does_not_inherit_privilege_or_commands(self):
        from schedlab.__main__ import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            source.write_text(json.dumps({"schema_version": 1, "preset": "deadline",
                                          "repetitions": 1, "selected_cpu": min(os.sched_getaffinity(0)),
                                          "allow_rt": True, "runs": [{"command": ["do-not-execute"]}]}))
            self.assertEqual(main(["replay", str(source), "--out", str(root / "output")]), 0)
            manifest = json.loads((root / "output/run.json").read_text())
            self.assertFalse(manifest["allow_rt"])
            self.assertEqual(manifest["runs"][0]["status"], "skipped")
            source.write_text(json.dumps({"schema_version": 1, "preset": "quick", "repetitions": 100000}))
            self.assertEqual(main(["replay", str(source), "--out", str(root / "bad")]), 1)

    def test_interrupt_manifest_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "interrupted"
            with patch("schedlab.runner.run_group", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run("quick", output, repetitions=1)
            self.assertEqual(json.loads((output / "run.json").read_text())["status"], "interrupted")
            self.assertIn("interrupted", (output / "report.html").read_text())

    @unittest.skipUnless(os.environ.get("SCHEDLAB_CGROUP_PARENT"), "opt-in cgroup delegation check disabled")
    def test_opt_in_cgroup(self):
        parent = Path(os.environ["SCHEDLAB_CGROUP_PARENT"])
        before = set(parent.iterdir())
        with tempfile.TemporaryDirectory() as directory:
            manifest = run("cgroup", Path(directory) / "run", repetitions=1, parent=parent)
            self.assertEqual([r["status"] for r in manifest["runs"]], ["ok"]*4)
        self.assertEqual(set(parent.iterdir()), before)


class EnvironmentTests(unittest.TestCase):
    def test_missing_is_explicit(self):
        self.assertEqual(read("/definitely-not-a-schedlab-file")["status"], "unavailable")
        with patch("schedlab.environment.gzip.open", side_effect=PermissionError("denied")), \
             patch("schedlab.environment.Path.read_text", side_effect=PermissionError("denied")):
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


class PipelineTests(unittest.TestCase):
    def test_bounded_pipeline_and_media(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.wav"
            for delay in (0, 50):
                result = subprocess.run(
                    [str(ROOT / "build/thread-pipeline"), "--output", str(path),
                     "--buffers", "20", "--consumer-delay-ms", str(delay)],
                    capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["consumed"]+data["dropped"], 20)
                self.assertFalse(data["device_xruns_measured"])
                with wave.open(str(path), "rb") as wav:
                    self.assertEqual(wav.getnframes(), 20*480)
                    self.assertEqual(wav.getframerate(), 48000)
                    self.assertNotEqual(set(wav.readframes(480)), {0})

    def test_pipeline_bounds(self):
        for value in ("0", "101", "not-a-number"):
            result = subprocess.run(
                [str(ROOT / "build/thread-pipeline"), "--output", "/dev/null", "--buffers", value],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
