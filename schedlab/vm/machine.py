"""Inside the tools container: fetch kernels into the store, assemble initramfs images, boot QEMU,
pin vCPUs, run host-side interference and unpack the guest's results.

The store is mounted at /store (the podman volume or SCHEDLAB_VM_STORE); apart from it, only the
output directory is written. Usage: python3 -m schedlab.vm.machine fetch|boot|list ...
"""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request

from ..environment import parse_cpu_list, system_load
from ..report import write_report
from . import catalog, transport
from .initramfs import Archive

STORE = Path("/store")
GUEST = Path("/guest")
HOST_LOAD = Path("/lab/build/static/sched-load")
# console=ttyS0 carries kernel and guest messages; ttyS1 carries the results frame.
BASE_CMDLINE = "console=ttyS0,115200 loglevel=4 panic=-1 rdinit=/init random.trust_cpu=on"


def say(text):
    print(f"schedlab vm: {text}", flush=True)


def kernel_dir(name):
    return STORE / "kernels" / name


def download(package, destination):
    """Fetch one package from its first working URL; keep it only if its SHA-256 is one listed."""
    errors = []
    partial = destination.with_suffix(".partial")
    for url in package["urls"]:
        try:
            digest = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=60) as response, open(partial, "wb") as output:
                while chunk := response.read(1 << 20):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() in package["sha256"]:
                partial.rename(destination)
                return url, digest.hexdigest()
            errors.append(f"{url}: SHA-256 mismatch")
        except OSError as exc:
            errors.append(f"{url}: {exc}")
    partial.unlink(missing_ok=True)
    raise RuntimeError("download failed: " + "; ".join(errors))


def extract(package_file, member):
    """One file from a .deb's or .rpm's payload."""
    if package_file.suffix == ".rpm":
        completed = subprocess.run(["bsdtar", "-xOf", str(package_file), f"./{member}"], capture_output=True)
        if completed.returncode == 0 and completed.stdout:
            return completed.stdout
        raise RuntimeError(f"{member} not found in {package_file.name}: {completed.stderr.decode().strip()}")
    # Reading stops at the file, so dpkg-deb's complaint about the closed pipe is discarded.
    process = subprocess.Popen(
        ["dpkg-deb", "--fsys-tarfile", str(package_file)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for entry in archive:
                if entry.name.removeprefix("./") == member and entry.isfile():
                    return archive.extractfile(entry).read()
    finally:
        process.stdout.close()
        process.wait()
    raise RuntimeError(f"{member} not found in {package_file.name}")


def fetch(name):
    """Kernel image and config of a distribution kernel, kept in the store; idempotent."""
    target = kernel_dir(name)
    if (target / "kernel.json").exists():
        return json.loads((target / "kernel.json").read_text())
    entry = catalog.KERNELS[name]
    downloads = STORE / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=STORE, prefix=f".{name}-"))
    packages = []
    try:
        files = {}
        for part in ("image", "config"):
            package, member = entry[part]
            if package == "image":
                package = entry["image"][0]
            file_name = package["urls"][0].rsplit("/", 1)[1]
            package_file = downloads / (package["sha256"][0] + Path(file_name).suffix)
            if not package_file.exists():
                say(f"downloading {file_name}")
                url, sha256 = download(package, package_file)
                packages.append({"url": url, "sha256": sha256})
            files[part] = extract(package_file, member)
        (staging / "vmlinuz").write_bytes(files["image"])
        (staging / "config").write_bytes(files["config"])
        record = {
            "name": name,
            "kind": "distribution",
            "description": entry["description"],
            "release": entry["release"],
            "packages": packages,
            "vmlinuz_sha256": hashlib.sha256(files["image"]).hexdigest(),
            "config_sha256": hashlib.sha256(files["config"]).hexdigest(),
            "fetched_utc": datetime.now(timezone.utc).isoformat(),
        }
        (staging / "kernel.json").write_text(json.dumps(record, indent=2) + "\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        # Only the image and config are kept; the packages are large.
        for package_file in downloads.iterdir():
            package_file.unlink()
    say(f"{name}: kernel {entry['release']} ready")
    return record


def installed():
    """Every kernel in the store with its record."""
    return [json.loads(path.read_text()) for path in sorted((STORE / "kernels").glob("*/kernel.json"))]


class InitramfsArchive(Archive):
    """An Archive that writes each directory once, so later trees can add to earlier ones."""

    def __init__(self, stream):
        super().__init__(stream)
        self.directories = set()

    def directory(self, name, mode=0o755, mtime=0):
        if name not in self.directories:
            self.directories.add(name)
            super().directory(name, mode, mtime)


def build_initramfs(path, kernel, job, scx_dir=None):
    """The guest tree, /dev/console, the kernel's config at /boot and the job, as one archive."""
    with open(path, "wb") as stream:
        archive = InitramfsArchive(stream)
        archive.tree(GUEST)
        archive.directory("dev")
        archive.character_device("dev/console", 5, 1)
        archive.directory("boot")
        archive.file(f"boot/config-{kernel['release']}", (kernel_dir(kernel["name"]) / "config").read_bytes())
        archive.directory("lab")
        archive.file("lab/job.json", json.dumps(job).encode())
        if scx_dir:
            archive.directory("lab/scx")
            archive.tree(scx_dir, "lab/scx")
        archive.close()


def topology(spec):
    """CPU count and its split into sockets, cores and threads, as QEMU's -smp wants it."""
    smp = spec.get("smp") or {}
    cpus = smp.get("cpus", 4)
    threads = smp.get("threads", 1)
    cores = smp.get("cores", cpus // threads)
    if cores * threads == 0 or cpus % (cores * threads):
        raise ValueError(f"{cpus} vCPUs do not split into {cores} cores x {threads} threads")
    return {"cpus": cpus, "sockets": cpus // (cores * threads), "cores": cores, "threads": threads}


def numa_args(nodes, cpus, memory_mb):
    """QEMU options for `nodes` equal NUMA nodes with local distance 10 and remote distance 20."""
    if cpus % nodes or memory_mb % nodes:
        raise ValueError(f"{cpus} vCPUs and {memory_mb} MiB do not split into {nodes} NUMA nodes")
    per_node = cpus // nodes
    args = []
    for node in range(nodes):
        args += ["-object", f"memory-backend-ram,id=mem{node},size={memory_mb // nodes}M"]
        first = node * per_node
        args += ["-numa", f"node,nodeid={node},cpus={first}-{first + per_node - 1},memdev=mem{node}"]
    for source in range(nodes):
        for destination in range(source + 1, nodes):
            args += ["-numa", f"dist,src={source},dst={destination},val=20"]
    return args


def cmdline(spec):
    return " ".join(part for part in (BASE_CMDLINE, spec.get("boot", "")) if part)


def qemu_args(spec, kernel, initramfs, workdir, accel):
    shape = topology(spec)
    memory_mb = spec.get("memory_mb", 2048)
    args = [
        "qemu-system-x86_64",
        "-nodefaults",
        "-no-user-config",
        "-machine",
        f"q35,accel={accel}",
        "-cpu",
        "host" if accel == "kvm" else "max",
        "-smp",
        "{cpus},sockets={sockets},cores={cores},threads={threads}".format(**shape),
        "-m",
        f"{memory_mb}M",
    ]
    if spec.get("numa"):
        args += numa_args(spec["numa"], shape["cpus"], memory_mb)
    args += [
        "-kernel",
        str(kernel_dir(kernel["name"]) / "vmlinuz"),
        "-initrd",
        str(initramfs),
        "-append",
        cmdline(spec),
        "-display",
        "none",
        "-no-reboot",
        "-chardev",
        f"stdio,id=console,signal=off,logfile={workdir / 'console.log'}",
        "-serial",
        "chardev:console",
        "-chardev",
        f"file,id=results,path={workdir / 'results.bin'}",
        "-serial",
        "chardev:results",
        "-qmp",
        f"unix:{workdir / 'qmp.sock'},server=on,wait=off",
    ]
    return args


def host_cpus_for(shape, requested):
    """Host CPUs for each vCPU: an explicit list, or "auto" - whole cores from the top of the
    allowed set, using both hardware threads of a core when the guest has SMT."""
    allowed = sorted(os.sched_getaffinity(0))
    if requested != "auto":
        cpus = parse_cpu_list(requested) if isinstance(requested, str) else list(requested)
        if len(cpus) != shape["cpus"] or not set(cpus) <= set(allowed):
            raise ValueError(f"--pin needs {shape['cpus']} allowed host CPUs, got {requested}")
        return cpus
    cores = {}
    for cpu in allowed:
        siblings_file = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list")
        siblings = tuple(parse_cpu_list(siblings_file.read_text())) if siblings_file.exists() else (cpu,)
        cores.setdefault(siblings, [cpu for cpu in siblings if cpu in allowed])
    chosen = []
    for threads in sorted(cores.values(), key=lambda threads: -threads[0]):
        if len(threads) >= shape["threads"]:
            chosen += threads[: shape["threads"]]
        if len(chosen) >= shape["cpus"]:
            return sorted(chosen[: shape["cpus"]])
    raise ValueError(f"not enough host cores to pin {shape['cpus']} vCPUs ({shape['threads']} per core)")


def qmp_vcpu_threads(path, timeout=15):
    """Host thread ID of each vCPU, from QEMU's QMP socket."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            connection = socket.socket(socket.AF_UNIX)
            connection.connect(str(path))
            break
        except OSError:
            connection.close()
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    with connection, connection.makefile("rw") as stream:

        def call(command):
            stream.write(json.dumps({"execute": command}) + "\n")
            stream.flush()
            while True:
                reply = json.loads(stream.readline())
                if "return" in reply:
                    return reply["return"]
                if "error" in reply:
                    raise RuntimeError(f"QMP {command}: {reply['error']}")

        stream.readline()  # greeting
        call("qmp_capabilities")
        return {cpu["cpu-index"]: cpu["thread-id"] for cpu in call("query-cpus-fast")}


class Neighbour:
    """Host-side sched-load on the vCPUs' host CPUs for as long as the VM runs."""

    def __init__(self, kind, cpus, workdir):
        self.kind, self.cpus, self.workdir = kind, cpus, workdir
        self.stopping = threading.Event()
        self.records = []
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.process = None

    def _loop(self):
        while not self.stopping.is_set():
            args = [str(HOST_LOAD), "--kind", self.kind, "--cpus", ",".join(map(str, self.cpus))]
            args += ["--max-duration-ms", "15000", "--output", str(self.workdir / "neighbour")]
            self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = self.process.communicate()
            try:
                self.records.append(json.loads(stdout))
            except ValueError:
                self.records.append({"status": "error", "stderr": stderr.strip()})
                return

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stopping.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.thread.join(timeout=10)


def unpack(frame_path, staging):
    payload = transport.decode(frame_path.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        archive.extractall(staging, filter="data")


def boot(spec, out):
    """Run one job in one VM and write its results to `out`, which must not exist yet."""
    kernel_path = kernel_dir(spec["kernel"]) / "kernel.json"
    if not kernel_path.exists():
        raise RuntimeError(f"kernel {spec['kernel']} is not in the store; fetch or build it first")
    kernel = json.loads(kernel_path.read_text())
    accel = spec.get("accel", "kvm")
    if accel == "kvm" and not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise RuntimeError("/dev/kvm is not usable here; pass --device /dev/kvm (or use accel tcg)")
    scx_dir = kernel_dir(spec["kernel"]) / "scx" if spec.get("scx") else None
    if scx_dir and not (scx_dir / spec["scx"]).exists():
        raise RuntimeError(f"{spec['scx']} is not built for {spec['kernel']}")
    shape = topology(spec)
    job = dict(spec["job"], cpu=spec.get("cpu"))
    if spec.get("scx"):
        job["scx"] = {"name": spec["scx"]}
    out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="schedlab-vm-") as work:
        workdir = Path(work)
        initramfs = workdir / "initramfs.cpio"
        build_initramfs(initramfs, kernel, job, scx_dir)
        args = qemu_args(spec, kernel, initramfs, workdir, accel)
        record = {
            "name": spec.get("name", spec["kernel"]),
            "kernel": kernel,
            "boot": spec.get("boot", ""),
            "cmdline": cmdline(spec),
            "topology": shape,
            "numa_nodes": spec.get("numa") or 1,
            "memory_mb": spec.get("memory_mb", 2048),
            "accel": accel,
            "sched_ext": spec.get("scx"),
            "checkout": spec.get("checkout", {}),
            "qemu": {"version": _qemu_version(), "args": args},
            "host": {
                "kernel": platform.release(),
                "cpu_model": _cpu_model(),
                "allowed_cpus": sorted(os.sched_getaffinity(0)),
                "load_before": system_load(),
            },
            "started_utc": datetime.now(timezone.utc).isoformat(),
        }
        say(f"booting {kernel['name']} ({kernel['release']}) with {cmdline(spec)}")
        started = time.monotonic()
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL)
        neighbour = None
        try:
            if spec.get("pin"):
                host_cpus = host_cpus_for(shape, spec["pin"])
                threads = qmp_vcpu_threads(workdir / "qmp.sock")
                for vcpu, thread in threads.items():
                    os.sched_setaffinity(thread, {host_cpus[vcpu]})
                record["pinning"] = {str(vcpu): host_cpus[vcpu] for vcpu in sorted(threads)}
                if spec.get("neighbour"):
                    neighbour = Neighbour(spec["neighbour"], host_cpus, workdir).start()
            process.wait(timeout=spec.get("timeout_s", 900))
        except subprocess.TimeoutExpired:
            record["timeout"] = True
            process.kill()
            process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if neighbour:
                neighbour.stop()
                record["neighbour"] = {"kind": spec["neighbour"], "runs": neighbour.records}
        record.update(wall_seconds=round(time.monotonic() - started, 3), qemu_exit=process.returncode)
        record["host"]["load_after"] = system_load()
        vm_dir = out / "vm"
        vm_dir.mkdir()
        shutil.copy(workdir / "console.log", vm_dir / "console.log")
        staging = workdir / "results"
        try:
            unpack(workdir / "results.bin", staging)
            record["guest"] = json.loads((staging / "guest.json").read_text())
        except (OSError, ValueError, tarfile.TarError) as exc:
            record["guest"] = {"status": "error", "reason": f"no results from the guest: {exc}"}
        if (staging / "run").is_dir():
            for item in (staging / "run").iterdir():
                shutil.move(str(item), out / item.name)
            shutil.rmtree(staging / "run")
        if staging.is_dir():
            for item in staging.iterdir():
                shutil.move(str(item), vm_dir / item.name)
    (vm_dir / "vm.json").write_text(json.dumps(record, indent=2) + "\n")
    manifest_path = out / "run.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        manifest["vm"] = {key: value for key, value in record.items() if key != "guest"}
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        write_report(manifest, out / "report.html")
    status = record["guest"].get("status")
    say(f"{record['name']}: guest {status} after {record['wall_seconds']:.1f} s" + _reason(record["guest"]))
    return record


def _reason(guest):
    return f" ({guest['reason']})" if guest.get("reason") else ""


def _qemu_version():
    completed = subprocess.run(["qemu-system-x86_64", "--version"], capture_output=True, text=True)
    return completed.stdout.splitlines()[0] if completed.stdout else "unknown"


def _cpu_model():
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "unknown"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m schedlab.vm.machine")
    commands = parser.add_subparsers(dest="command", required=True)
    fetcher = commands.add_parser("fetch")
    fetcher.add_argument("kernels", nargs="+", choices=catalog.KERNELS)
    commands.add_parser("list")
    booter = commands.add_parser("boot")
    booter.add_argument("--spec", required=True, help="VM configuration and job, as JSON")
    booter.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            for name in args.kernels:
                fetch(name)
            return 0
        if args.command == "list":
            print(json.dumps(installed(), indent=2))
            return 0
        record = boot(json.loads(args.spec), args.out)
        return 0 if record["guest"].get("status") == "ok" else 1
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
