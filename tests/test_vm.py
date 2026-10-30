"""VM tooling that needs no VM: formats, catalog, QEMU and kernel-config arguments, analysis."""

import io
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest
from unittest.mock import patch

from schedlab.analysis import analyze_manifest, preemption_mode, render, vm_summary
from schedlab.vm import build, catalog, guest, host, machine, transport
from schedlab.vm.initramfs import Archive, entries

SHA256 = re.compile(r"[0-9a-f]{64}")


class InitramfsTests(unittest.TestCase):
    def test_entries_round_trip_with_alignment(self):
        stream = io.BytesIO()
        archive = Archive(stream)
        archive.directory("lab")
        archive.file("lab/job.json", b'{"kind": "run"}', mode=0o600)
        archive.symlink("lab/link", "job.json")
        archive.character_device("dev/console", 5, 1)
        archive.close()
        data = stream.getvalue()
        self.assertEqual(len(data) % 4, 0)
        found = {name: (mode, content) for name, mode, content in entries(data)}
        self.assertEqual(found["lab/job.json"], (stat.S_IFREG | 0o600, b'{"kind": "run"}'))
        self.assertEqual(found["lab/link"][1], b"job.json")
        self.assertTrue(stat.S_ISDIR(found["lab"][0]))
        self.assertTrue(stat.S_ISCHR(found["dev/console"][0]))

    def test_tree_and_single_directory_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "lab").mkdir()
            (root / "lab/run.py").write_text("print()\n")
            (root / "init").symlink_to("lab/run.py")
            stream = io.BytesIO()
            archive = machine.InitramfsArchive(stream)
            archive.tree(root)
            archive.directory("lab")
            archive.close()
        names = [name for name, _, _ in entries(stream.getvalue())]
        self.assertEqual(sorted(names), ["init", "lab", "lab/run.py"])


class TransportTests(unittest.TestCase):
    def test_round_trip_after_stray_bytes(self):
        payload = bytes(range(256)) * 10
        self.assertEqual(transport.decode(b"noise\r\n" + transport.encode(payload)), payload)

    def test_missing_short_and_corrupt_frames(self):
        frame = transport.encode(b"results")
        for data, message in (
            (b"no frame here", "did not finish"),
            (frame[:-2], "truncated"),
            (frame[:-1] + b"X", "corrupt"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                transport.decode(data)


class CatalogTests(unittest.TestCase):
    def test_every_kernel_is_pinned_with_a_fallback(self):
        for name, entry in catalog.KERNELS.items():
            with self.subTest(kernel=name):
                for part, file_name in (("image", "vmlinuz"), ("config", "config")):
                    package, member = entry[part]
                    if package == "image":
                        package = entry["image"][0]
                    self.assert_pinned(package)
                    self.assertFalse(member.startswith("/"))
                    self.assertTrue(member.rsplit("/", 1)[1].startswith(file_name), member)
                    self.assertIn(entry["release"], member)
                source = entry.get("scx_source")
                if isinstance(source, str):
                    self.assertIn(source, catalog.SOURCES)
                elif source:
                    self.assert_pinned(source["package"])
                    self.assertTrue(source["tarball"].endswith(".tar.xz"))
        for version, sha256 in catalog.SOURCES.items():
            self.assertRegex(sha256, SHA256, version)

    def assert_pinned(self, package):
        self.assertTrue(package["sha256"])
        for sha256 in package["sha256"]:
            self.assertRegex(sha256, SHA256)
        self.assertGreaterEqual(len(package["urls"]), 2)
        self.assertTrue(all(url.startswith(("http://", "https://")) for url in package["urls"]))

    def test_source_names(self):
        self.assertEqual(
            catalog.parse_source("linux-7.2.13-rt-hz1000-nohzfull-scx"),
            {"version": "7.2.13", "preempt": "rt", "hz": 1000, "nohz_full": True, "sched_ext": True},
        )
        self.assertEqual(catalog.parse_source("linux-6.18.59")["preempt"], None)
        self.assertIsNone(catalog.parse_source("linux-7.2.13-fast"))
        with self.assertRaises(ValueError):
            catalog.parse_source("linux-7.2.13-hz500")
        with self.assertRaises(ValueError):
            catalog.kernel_kind("windows-11")

    def test_matrices_name_bootable_kernels(self):
        for name, matrix in catalog.MATRICES.items():
            names = [entry["name"] for entry in matrix["entries"]]
            self.assertEqual(len(names), len(set(names)), name)
            for entry in matrix["entries"]:
                catalog.kernel_kind(entry["kernel"])
                if entry.get("neighbour"):
                    self.assertTrue(entry.get("pin"), f"{name}/{entry['name']}")


KERNEL = {"name": "debian-7.2", "release": "7.2.11+deb13-amd64"}


class MachineTests(unittest.TestCase):
    def test_topology(self):
        self.assertEqual(
            machine.topology({"smp": {"cpus": 4, "threads": 2}}),
            {"cpus": 4, "sockets": 1, "cores": 2, "threads": 2},
        )
        self.assertEqual(machine.topology({})["cpus"], 4)
        with self.assertRaises(ValueError):
            machine.topology({"smp": {"cpus": 3, "threads": 2}})

    def test_qemu_arguments(self):
        spec = {"kernel": "debian-7.2", "smp": {"cpus": 4, "threads": 2}, "numa": 2, "boot": "preempt=full"}
        args = machine.qemu_args(spec, KERNEL, Path("/tmp/initramfs"), Path("/tmp/work"), "kvm")
        self.assertIn("4,sockets=1,cores=2,threads=2", args)
        self.assertEqual(args[args.index("-cpu") + 1], "host")
        append = args[args.index("-append") + 1]
        self.assertTrue(append.startswith("console=ttyS0") and append.endswith("preempt=full"))
        self.assertIn("node,nodeid=1,cpus=2-3,memdev=mem1", args)
        self.assertIn("dist,src=0,dst=1,val=20", args)
        self.assertEqual(args.count("-serial"), 2)
        emulated = machine.qemu_args({"kernel": "debian-7.2"}, KERNEL, Path("/i"), Path("/w"), "tcg")
        self.assertEqual(emulated[emulated.index("-cpu") + 1], "max")

    def test_numa_split_must_be_even(self):
        with self.assertRaises(ValueError):
            machine.numa_args(3, 4, 2048)

    def test_explicit_pinning_is_validated(self):
        shape = {"cpus": 2, "threads": 1}
        allowed = sorted(os.sched_getaffinity(0))
        if len(allowed) < 2:
            self.skipTest("needs two allowed CPUs")
        self.assertEqual(machine.host_cpus_for(shape, allowed[:2]), allowed[:2])
        with self.assertRaises(ValueError):
            machine.host_cpus_for(shape, allowed[:1])


class BuildConfigTests(unittest.TestCase):
    def test_requested_options(self):
        options = catalog.parse_source("linux-7.2.13-rt-hz1000-nohzfull-scx")
        values = build.requested_options(options)
        self.assertEqual((values["PREEMPT_RT"], values["PREEMPT_DYNAMIC"], values["EXPERT"]), ("y", "n", "y"))
        self.assertEqual((values["HZ_1000"], values["HZ_250"]), ("y", "n"))
        self.assertEqual((values["NO_HZ_FULL"], values["SCHED_CLASS_EXT"]), ("y", "y"))
        self.assertEqual(build.requested_options(catalog.parse_source("linux-7.2.13")), {})

    def test_config_rewrite_and_unmet(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / ".config"
            config.write_text("CONFIG_HZ_250=y\n# CONFIG_HZ_1000 is not set\nCONFIG_NUMA=y\n")
            build.set_options(config, {"HZ_250": "n", "HZ_1000": "y", "SCHED_CLASS_EXT": "y"})
            values = build.parse_config(config.read_text())
            self.assertEqual((values["HZ_250"], values["HZ_1000"], values["NUMA"]), ("n", "y", "y"))
            config.write_text("CONFIG_HZ_1000=y\n")
            self.assertEqual(
                build.unmet(config, {"HZ_1000": "y", "SCHED_CLASS_EXT": "y", "PREEMPT_NONE": "n"}),
                {"SCHED_CLASS_EXT": "absent"},
            )


class GuestTests(unittest.TestCase):
    def test_commands(self):
        args, extra = guest.command(
            {"kind": "run", "preset": "cgroup", "repetitions": 2, "label": "x", "cpu": 2, "allow_rt": True}
        )
        self.assertEqual(args[1:5], ["-m", "schedlab", "run", "cgroup"])
        self.assertIn("--allow-rt", args)
        self.assertEqual(args[args.index("--cpu") + 1], "2")
        self.assertEqual(args[args.index("--cgroup-parent") + 1], str(guest.CGROUP_PARENT))
        self.assertEqual(extra, {})
        args, extra = guest.command({"kind": "test"})
        self.assertEqual(args[1:4], ["-m", "unittest", "discover"])
        self.assertEqual((extra["SCHEDLAB_TEST_RT"], extra["SCHEDLAB_TEST_DEADLINE"]), ("1", "1"))
        with self.assertRaises(ValueError):
            guest.command({"kind": "shell"})


class HostTests(unittest.TestCase):
    def options(self, **overrides):
        values = dict(
            kernel="debian-7.2",
            boot="quiet",
            smp=4,
            threads=1,
            numa=None,
            memory=2048,
            pin=None,
            neighbour=None,
            scx=None,
            cpu=None,
            accel="kvm",
            timeout=900,
        )
        values.update(overrides)
        return type("Args", (), values)()

    def test_matrix_entries_override_options(self):
        entry = catalog.MATRICES["isolation"]["entries"][2]
        spec = host.vm_spec(self.options(kernel=None), entry)
        self.assertEqual((spec["name"], spec["kernel"], spec["cpu"]), ("isolated", "debian-7.2", 2))
        self.assertTrue(spec["boot"].startswith("isolcpus=") and spec["boot"].endswith("quiet"))
        self.assertEqual(spec["smp"], {"cpus": 4, "threads": 1})

    def test_neighbour_needs_pinning(self):
        with self.assertRaisesRegex(ValueError, "--pin"):
            host.vm_spec(self.options(neighbour="cpu"))

    def test_sched_ext_needs_a_usable_kernel(self):
        self.assertEqual(
            host.vm_spec(self.options(kernel="fedora-44", scx="scx_simple"))["scx"], "scx_simple"
        )
        host.vm_spec(self.options(kernel="linux-7.2.13-scx", scx="scx_qmap"))
        for kernel in ("debian-7.2", "linux-7.2.13"):
            with self.subTest(kernel=kernel), self.assertRaisesRegex(ValueError, "usable sched_ext"):
                host.vm_spec(self.options(kernel=kernel, scx="scx_simple"))
        specs = [
            host.vm_spec(self.options(kernel=k, scx="scx_simple")) for k in ("fedora-44", "linux-7.2.13-scx")
        ]
        self.assertEqual(host.scx_builds(specs), ["fedora-44"])

    def test_store_directory_override(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"SCHEDLAB_VM_STORE": temp}):
            self.assertEqual(host.store_mount(), f"{Path(temp).resolve()}:/store")
        with patch.dict(os.environ, {"SCHEDLAB_VM_STORE": ""}):
            self.assertEqual(host.store_mount(), "schedlab-vm:/store")


def vm_manifest(steal_share, neighbour=None, pinning=None):
    window = 1e9
    run = {
        "case": "offline",
        "label": "offline-r1",
        "repetition": 1,
        "status": "ok",
        "group_cases": ["offline"],
        "core": {
            "mode": "offline",
            "effective": {"policy": "other", "nice": 0},
            "contention": {
                "status": "ok",
                "window_ns": window,
                "runqueue_wait_ns": 0,
                "cpu_others_run_ns": 0,
                "cpu_steal_ns": steal_share * window,
                "smt_siblings_busy_ns": [],
            },
        },
        "metrics": {"throughput_jobs_s": 1000, "cpu_total_ns": window, "elapsed_ns": window},
    }
    environment = {
        "system": {"kernel": "7.2.11+deb13-amd64", "architecture": "x86_64"},
        "kernel_config": {"options": {"CONFIG_PREEMPT_DYNAMIC": "y", "CONFIG_HZ": "250"}},
        "runtime": {
            "/sys/kernel/debug/sched/preempt": {"status": "ok", "value": "none voluntary full (lazy)"}
        },
    }
    vm = {
        "kernel": {"name": "debian-7.2"},
        "accel": "kvm",
        "topology": {"cpus": 4, "cores": 2, "threads": 2},
        "numa_nodes": 1,
        "boot": "preempt=lazy",
        "pinning": pinning,
        "neighbour": neighbour,
        "host": {},
    }
    return {
        "schema_version": 1,
        "preset": "quick",
        "repetitions": 3,
        "runs": [run],
        "environment": environment,
        "vm": vm,
    }


class VmAnalysisTests(unittest.TestCase):
    def test_preemption_mode(self):
        manifest = vm_manifest(0)
        self.assertEqual(preemption_mode(manifest["environment"]), "lazy")
        self.assertIsNone(preemption_mode({"kernel_config": {"options": {"CONFIG_PREEMPT_DYNAMIC": "y"}}}))
        fixed = {"kernel_config": {"options": {"CONFIG_PREEMPT_VOLUNTARY": "y", "CONFIG_PREEMPT": "n"}}}
        self.assertEqual(preemption_mode(fixed), "voluntary")
        self.assertEqual(preemption_mode({"kernel_config": {"options": {"CONFIG_PREEMPT_RT": "y"}}}), "rt")

    def test_ignored_preempt_parameter_is_a_caveat(self):
        manifest = vm_manifest(0)
        self.assertFalse(any("does not offer" in caveat for caveat in analyze_manifest(manifest)["caveats"]))
        manifest["vm"]["boot"] = "quiet preempt=none"
        caveats = analyze_manifest(manifest)["caveats"]
        self.assertTrue(any("preempt=none, but the kernel ran 'lazy'" in caveat for caveat in caveats))

    def test_summary_and_unpinned_steal_caveats(self):
        analysis = analyze_manifest(vm_manifest(0.3))
        self.assertEqual(
            analysis["system"]["vm"],
            "debian-7.2 in QEMU/KVM, 4 vCPUs as 2 cores × 2 threads, boot preempt=lazy",
        )
        caveats = " ".join(analysis["caveats"])
        self.assertIn("QEMU/KVM guest", caveats)
        self.assertIn("not pinned", caveats)
        self.assertIn("offline 30%", caveats)
        text = render({"runs": [analysis]}, "text")
        self.assertIn("(preempt lazy) · HZ 250", text)
        self.assertIn("vm: debian-7.2 in QEMU/KVM", text)
        self.assertIn("steal 30%", text)

    def test_neighbour_steal_is_a_finding(self):
        pinning = {"0": 8, "1": 10, "2": 12, "3": 14}
        analysis = analyze_manifest(vm_manifest(0.5, neighbour={"kind": "cpu"}, pinning=pinning))
        self.assertTrue(any("stole part of the measured vCPU" in f["text"] for f in analysis["findings"]))
        self.assertFalse(any("steal)" in caveat for caveat in analysis["caveats"]))
        self.assertIn("pinned to host CPUs 8,10,12,14, host cpu load", analysis["system"]["vm"])
        self.assertIsNone(vm_summary(None))


if __name__ == "__main__":
    unittest.main()
