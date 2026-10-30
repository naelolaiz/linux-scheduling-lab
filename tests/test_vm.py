"""VM tooling that needs no VM: formats, catalog, published builds, QEMU and kernel-config
arguments, analysis."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
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

    def test_published_builds_cover_what_the_matrices_build(self):
        for name, sha256 in catalog.PREBUILT.items():
            with self.subTest(build=name):
                self.assertRegex(sha256, SHA256)
                if catalog.kernel_kind(name) == "distribution":
                    self.assertTrue(catalog.KERNELS[name].get("scx_source"), "only schedulers are built")
                package = catalog.prebuilt(name)
                self.assertTrue(package["asset"].startswith(f"{name}-") and sha256[:16] in package["asset"])
                self.assertEqual(package["urls"], [catalog.PREBUILT_URL + package["asset"]])
        built = {
            entry["kernel"]
            for matrix in catalog.MATRICES.values()
            for entry in matrix["entries"]
            if catalog.kernel_kind(entry["kernel"]) == "source" or entry.get("scx")
        }
        self.assertLessEqual(built, set(catalog.PREBUILT))
        self.assertIsNone(catalog.prebuilt("debian-7.2"))
        self.assertIn("-schedulers-", catalog.prebuilt_asset("fedora-44", "0" * 64))


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


class ReproducibleBuildTests(unittest.TestCase):
    def test_build_identity_comes_from_the_source(self):
        with tempfile.TemporaryDirectory() as temp:
            makefile = Path(temp) / "Makefile"
            makefile.write_text("VERSION = 7\n")
            os.utime(makefile, (0, 1793026874))
            environment = build.build_environment(Path(temp))
        self.assertEqual(environment["SOURCE_DATE_EPOCH"], "1793026874")
        self.assertEqual(environment["KBUILD_BUILD_TIMESTAMP"], "Mon Oct 26 15:01:14 UTC 2026")
        self.assertEqual(
            [environment[f"KBUILD_BUILD_{key}"] for key in ("USER", "HOST", "VERSION")],
            ["schedlab", "schedlab", "1"],
        )

    def test_pack_ignores_times_owners_modes_and_creation_order(self):
        digests = []
        for mtime, mode, scx_first in ((1, 0o664, True), (2000000000, 0o600, False)):
            with tempfile.TemporaryDirectory() as temp:
                directory = Path(temp) / "linux-7.2.13-scx"
                directory.mkdir()
                files = [("scx/scx_simple", b"\x7fELF"), ("kernel.json", b"{}\n")]
                for relative, content in files if scx_first else reversed(files):
                    (directory / relative).parent.mkdir(exist_ok=True)
                    (directory / relative).write_bytes(content)
                (directory / "scx/scx_simple").chmod(0o750)
                (directory / "kernel.json").chmod(mode)
                for path in [directory, *directory.rglob("*")]:
                    os.utime(path, (mtime, mtime))
                digests.append(build.pack(directory, Path(temp) / "asset.tar.xz"))
                with tarfile.open(Path(temp) / "asset.tar.xz") as archive:
                    members = {member.name: member for member in archive.getmembers()}
        self.assertEqual(digests[0], digests[1])
        self.assertEqual(
            list(members),
            [
                "linux-7.2.13-scx",
                "linux-7.2.13-scx/kernel.json",
                "linux-7.2.13-scx/scx",
                "linux-7.2.13-scx/scx/scx_simple",
            ],
        )
        self.assertEqual(members["linux-7.2.13-scx/scx/scx_simple"].mode, 0o755)
        self.assertEqual(members["linux-7.2.13-scx/kernel.json"].mode, 0o644)
        self.assertTrue(all(member.uid == 0 and member.mtime == 0 for member in members.values()))

    def test_toolchain_versions(self):
        installed = "\n".join(
            [
                "binutils-2.45.1-r1 x86_64 {binutils} (GPL-2.0-or-later) [installed]",
                "clang22-22.1.3-r2 x86_64 {clang22} (Apache-2.0 WITH LLVM-exception) [installed]",
                "gcc-15.2.0-r5 x86_64 {gcc} (GPL-2.0-or-later AND LGPL-2.1-or-later) [installed]",
                "llvm-22-r0 x86_64 {llvm-meta} (Apache-2.0) [installed]",
                "musl-dev-1.2.6-r3 x86_64 {musl} (MIT) [installed]",
                "zstd-libs-1.5.7-r2 x86_64 {zstd} (BSD-3-Clause) [installed]",
            ]
        )
        self.assertEqual(
            build.parse_toolchain(installed),
            {"binutils": "2.45.1-r1", "clang22": "22.1.3-r2", "gcc": "15.2.0-r5", "musl-dev": "1.2.6-r3"},
        )

    def test_differences_name_what_changed(self):
        ours = {"name": "x", "vmlinuz_sha256": "a", "toolchain": {"gcc": "15.2.0-r5", "pahole": "v1.32"}}
        theirs = {"name": "x", "vmlinuz_sha256": "b", "toolchain": {"gcc": "15.2.0-r6", "pahole": "v1.32"}}
        self.assertEqual(
            build.differences(ours, theirs),
            ["toolchain.gcc: 15.2.0-r5 here, 15.2.0-r6 published", "vmlinuz_sha256: a here, b published"],
        )


class PrebuiltTests(unittest.TestCase):
    """Published builds installed from a local "release" through file:// URLs."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = self.root / "store"
        self.release = self.root / "release"
        self.store.mkdir()
        self.release.mkdir()
        for patcher in (
            patch.object(machine, "STORE", self.store),
            patch.object(catalog, "PREBUILT_URL", self.release.as_uri() + "/"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def publish(self, name, files):
        """`files` packed as the release asset of `name`; returns the asset's SHA-256."""
        directory = self.root / "build" / name
        for relative, content in files.items():
            (directory / relative).parent.mkdir(parents=True, exist_ok=True)
            (directory / relative).write_bytes(content)
            if relative.startswith("scx/"):
                (directory / relative).chmod(0o755)
        packed = self.root / "asset.tar.xz"
        sha256 = build.pack(directory, packed)
        packed.rename(self.release / catalog.prebuilt_asset(name, sha256))
        return sha256

    def test_source_kernel(self):
        name = "linux-7.2.13-hz1000"
        files = {
            "kernel.json": b'{"name": "linux-7.2.13-hz1000"}',
            "vmlinuz": b"image",
            "config": b"CONFIG_HZ=1000\n",
        }
        sha256 = self.publish(name, files)
        with patch.dict(catalog.PREBUILT, {name: sha256}):
            machine.fetch_prebuilt(name)
        record = machine.kernel_record(name)
        self.assertEqual(record["name"], name)
        self.assertEqual(
            (record["prebuilt"]["asset"], record["prebuilt"]["sha256"]),
            (catalog.prebuilt_asset(name, sha256), sha256),
        )
        self.assertEqual((self.store / "kernels" / name / "vmlinuz").read_bytes(), b"image")
        self.assertEqual(list((self.store / "downloads").iterdir()), [])

    def test_a_digest_mismatch_installs_nothing(self):
        name = "linux-7.2.13-hz1000"
        sha256 = self.publish(name, {"kernel.json": b"{}", "vmlinuz": b"image", "config": b""})
        wrong = "f" * 64
        shutil.copy(
            self.release / catalog.prebuilt_asset(name, sha256),
            self.release / catalog.prebuilt_asset(name, wrong),
        )
        with (
            patch.dict(catalog.PREBUILT, {name: wrong}),
            self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"),
        ):
            machine.fetch_prebuilt(name)
        self.assertEqual(list(self.store.iterdir()), [self.store / "downloads"])

    def test_distribution_schedulers_must_match_the_kernel_image(self):
        kernel = self.store / "kernels" / "fedora-44"
        kernel.mkdir(parents=True)
        (kernel / "vmlinuz").write_bytes(b"fedora image")
        (kernel / "kernel.json").write_text(json.dumps({"name": "fedora-44", "kind": "distribution"}))
        built = {
            "name": "fedora-44",
            "kind": "schedulers",
            "kernel_vmlinuz_sha256": hashlib.sha256(b"fedora image").hexdigest(),
            "schedulers": {"scx_simple": "1" * 64},
        }
        sha256 = self.publish(
            "fedora-44", {"schedulers.json": json.dumps(built).encode(), "scx/scx_simple": b"\x7fELF"}
        )
        with patch.dict(catalog.PREBUILT, {"fedora-44": sha256}):
            machine.fetch_prebuilt("fedora-44")
            record = machine.kernel_record("fedora-44")
            self.assertEqual(record["schedulers"], {"scx_simple": "1" * 64})
            self.assertEqual(record["schedulers_build"]["kind"], "schedulers")
            self.assertEqual(record["prebuilt"]["sha256"], sha256)
            self.assertTrue(os.access(kernel / "scx/scx_simple", os.X_OK))
            (kernel / "vmlinuz").write_bytes(b"an updated fedora image")
            with self.assertRaisesRegex(RuntimeError, "another fedora-44 kernel image"):
                machine.fetch_prebuilt("fedora-44")


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

    def provide(self, stores, prebuilt=True, download_status=0):
        """The container commands ensure_kernels runs, given the store before and after downloads."""
        calls = []

        def container(_image, args, **_options):
            calls.append(args)
            return download_status if args[1] == "prebuilt" else 0

        with (
            patch.object(host, "build_image"),
            patch.object(host, "stored_kernels", side_effect=stores),
            patch.object(host, "container", side_effect=container),
        ):
            host.ensure_kernels(["linux-7.2.13-hz1000", "linux-7.2.13-hz300"], prebuilt=prebuilt)
        return calls

    def test_published_builds_are_downloaded_and_the_rest_built(self):
        downloaded = {"linux-7.2.13-hz1000": {}}
        self.assertEqual(
            self.provide([{}, downloaded]),
            [
                ["schedlab.vm.machine", "prebuilt", "linux-7.2.13-hz1000"],
                ["schedlab.vm.build", "linux-7.2.13-hz300"],
            ],
        )
        builds = [["schedlab.vm.build", "linux-7.2.13-hz1000"], ["schedlab.vm.build", "linux-7.2.13-hz300"]]
        self.assertEqual(self.provide([{}, {}], download_status=1)[1:], builds)
        self.assertEqual(self.provide([{}], prebuilt=False), builds)

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
