"""Inside the builder container: kernel.org releases into bootable lab kernels, with the sched_ext
example schedulers (the kernel's tools/sched_ext) when the name asks for sched_ext.

Usage: python3 -m schedlab.vm.build NAME, where NAME is a source kernel such as
linux-7.2.13-rt-hz1000 (see catalog.parse_source), or a distribution kernel with a usable
sched_ext to build schedulers for. The tree is unpacked and built in the container's temporary
space; the store keeps the kernel image, its config, a record and any schedulers. The schedulers'
kfunc declarations must match the kernel's BTF, which for 7.0 and later needs pahole 1.31 or newer
when the kernel is built (the builder has it).
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from . import catalog
from .machine import download

STORE = Path("/store")
# Everything a lab guest needs is built in, so the guest loads no modules. IKCONFIG_PROC also
# exposes the config at /proc/config.gz; PARAVIRT_TIME_ACCOUNTING keeps steal time out of task time.
BASE_OPTIONS = {
    "IKCONFIG": "y",
    "IKCONFIG_PROC": "y",
    "SCHEDSTATS": "y",
    "SCHED_DEBUG": "y",
    "PSI": "y",
    "PARAVIRT_TIME_ACCOUNTING": "y",
    "NUMA": "y",
    "CGROUPS": "y",
    "CGROUP_SCHED": "y",
    "FAIR_GROUP_SCHED": "y",
    "CFS_BANDWIDTH": "y",
    "CPUSETS": "y",
    "SCHED_AUTOGROUP": "y",
    "RT_GROUP_SCHED": "n",
    "SERIAL_8250": "y",
    "SERIAL_8250_CONSOLE": "y",
    "DEVTMPFS": "y",
    "BLK_DEV_INITRD": "y",
    "RD_GZIP": "y",
}
PREEMPT_SYMBOLS = {
    "none": "PREEMPT_NONE",
    "voluntary": "PREEMPT_VOLUNTARY",
    "full": "PREEMPT",
    "lazy": "PREEMPT_LAZY",
    "rt": "PREEMPT_RT",
}
SCHED_EXT_OPTIONS = {
    "BPF": "y",
    "BPF_SYSCALL": "y",
    "BPF_JIT": "y",
    "DEBUG_INFO": "y",
    "DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT": "y",
    "DEBUG_INFO_REDUCED": "n",
    "DEBUG_INFO_BTF": "y",
    "SCHED_CLASS_EXT": "y",
}
SCHEDULERS = ("scx_simple", "scx_qmap", "scx_central")


def say(text):
    print(f"schedlab vm build: {text}", flush=True)


def requested_options(options):
    """Config symbols for a source kernel's options (BASE_OPTIONS come on top of these)."""
    values = {}
    if options["preempt"]:
        # A fixed model: the experiment is the compile-time choice, not a boot parameter.
        values.update(dict.fromkeys(PREEMPT_SYMBOLS.values(), "n"))
        values.update({PREEMPT_SYMBOLS[options["preempt"]]: "y", "PREEMPT_DYNAMIC": "n"})
        if options["preempt"] == "rt":
            values["EXPERT"] = "y"
    if options["hz"]:
        values.update({f"HZ_{hz}": "n" for hz in catalog.HZ_VALUES})
        values[f"HZ_{options['hz']}"] = "y"
    if options["nohz_full"]:
        values.update({"HZ_PERIODIC": "n", "NO_HZ_IDLE": "n", "NO_HZ_FULL": "y", "RCU_NOCB_CPU": "y"})
    if options["sched_ext"]:
        values.update(SCHED_EXT_OPTIONS)
    return values


def parse_config(text):
    values = {}
    for line in text.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            values[key.removeprefix("CONFIG_")] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            values[line.split()[1].removeprefix("CONFIG_")] = "n"
    return values


def set_options(path, values):
    """Rewrite a .config with `values` applied; `make olddefconfig` then resolves dependencies."""
    lines = [
        line
        for line in path.read_text().splitlines()
        if not any(line.startswith((f"CONFIG_{key}=", f"# CONFIG_{key} is not set")) for key in values)
    ]
    for key, value in values.items():
        lines.append(f"# CONFIG_{key} is not set" if value == "n" else f"CONFIG_{key}={value}")
    path.write_text("\n".join(lines) + "\n")


def unmet(path, values):
    """Enabled symbols the resolved config did not keep: absent from this release or blocked by a
    dependency. A disabled symbol that does not exist is already disabled."""
    actual = parse_config(path.read_text())
    return {key: actual.get(key, "absent") for key, value in values.items() if actual.get(key, "n") != value}


def source_tarball(source):
    """A source tarball cached in the store: a kernel.org release given by its version, or the
    tarball inside a distribution's source package ({"package": ..., "tarball": name})."""
    sources = STORE / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    if isinstance(source, str):
        sha256 = catalog.SOURCES.get(source)
        if sha256 is None:
            raise RuntimeError(f"linux-{source} is not pinned; add its SHA-256 to catalog.SOURCES")
        tarball = sources / f"linux-{source}.tar.xz"
        if not tarball.exists():
            say(f"downloading {catalog.source_url(source)}")
            download({"urls": [catalog.source_url(source)], "sha256": [sha256]}, tarball)
        return tarball
    tarball = sources / source["tarball"]
    if not tarball.exists():
        package = source["package"]
        source_package = sources / (package["sha256"][0] + ".src.rpm")
        say(f"downloading {package['urls'][0].rsplit('/', 1)[1]}")
        download(package, source_package)
        try:
            # Only the tarball is kept; the rest of the source package is spec files and configs.
            subprocess.run(
                ["bsdtar", "-xf", str(source_package), "-C", str(sources), source["tarball"]], check=True
            )
        finally:
            source_package.unlink()
    return tarball


def source_tree(source, workdir):
    """A source tree unpacked under `workdir` (which must start empty)."""
    tarball = source_tarball(source)
    say(f"unpacking {tarball.name}")
    subprocess.run(["tar", "-xJf", str(tarball), "-C", str(workdir)], check=True)
    trees = [path for path in Path(workdir).iterdir() if path.is_dir()]
    if len(trees) != 1:
        raise RuntimeError(f"{tarball.name} does not unpack to a single directory")
    return trees[0]


def make(tree, *targets, jobs=None):
    subprocess.run(["make", "-C", str(tree), "-s", f"-j{jobs or os.cpu_count()}", *targets], check=True)


def build_schedulers(tree, vmlinux, destination):
    """tools/sched_ext against the BTF in `vmlinux`; the example schedulers go to `destination`."""
    say(f"building sched_ext example schedulers: {', '.join(SCHEDULERS)}")
    subprocess.run(
        [
            "make",
            "-C",
            str(tree / "tools/sched_ext"),
            f"-j{os.cpu_count()}",
            f"VMLINUX_BTF={vmlinux}",
            *SCHEDULERS,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    destination.mkdir(parents=True, exist_ok=True)
    built = []
    for name in SCHEDULERS:
        binary = tree / "tools/sched_ext/build/bin" / name
        if binary.exists():
            shutil.copy2(binary, destination / name)
            built.append(name)
    if not built:
        raise RuntimeError("tools/sched_ext produced none of " + ", ".join(SCHEDULERS))
    return built


def build_source(name):
    options = catalog.parse_source(name)
    target = STORE / "kernels" / name
    if (target / "kernel.json").exists():
        say(f"{name} is already built")
        return
    with tempfile.TemporaryDirectory(prefix="schedlab-build-") as work:
        tree = source_tree(options["version"], Path(work))
        started = datetime.now(timezone.utc)
        make(tree, "defconfig")
        config = tree / ".config"
        fragment = parse_config((tree / "kernel/configs/kvm_guest.config").read_text())
        values = requested_options(options)
        set_options(config, {**fragment, **BASE_OPTIONS, **values})
        make(tree, "olddefconfig")
        # The named options are the experiment, so they must hold; base options are best effort.
        missing = unmet(config, values)
        if missing:
            raise RuntimeError(f"{name}: this release cannot provide {missing}")
        say(f"compiling {name} (several minutes)")
        make(tree, "bzImage")
        release = subprocess.run(
            ["make", "-C", str(tree), "-s", "kernelrelease"], capture_output=True, text=True, check=True
        ).stdout.strip()
        staging = STORE / "kernels" / f".{name}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        shutil.copy2(tree / "arch/x86/boot/bzImage", staging / "vmlinuz")
        shutil.copy2(config, staging / "config")
        schedulers = build_schedulers(tree, tree / "vmlinux", staging / "scx") if options["sched_ext"] else []
        compiler = subprocess.run(["gcc", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
        pahole = subprocess.run(["pahole", "--version"], capture_output=True, text=True).stdout.strip()
        record = {
            "name": name,
            "kind": "source",
            "description": f"kernel.org {options['version']} built with {_describe(options)}",
            "release": release,
            "version": options["version"],
            "options": options,
            "requested_config": values,
            "base_config_not_applied": unmet(config, BASE_OPTIONS),
            "source_sha256": catalog.SOURCES[options["version"]],
            "compiler": compiler,
            "pahole": pahole,
            "schedulers": schedulers,
            "vmlinuz_sha256": hashlib.sha256((staging / "vmlinuz").read_bytes()).hexdigest(),
            "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "built_utc": datetime.now(timezone.utc).isoformat(),
            "build_seconds": round((datetime.now(timezone.utc) - started).total_seconds()),
        }
        (staging / "kernel.json").write_text(json.dumps(record, indent=2) + "\n")
        staging.rename(target)
    say(f"{name}: kernel {release} ready")


def _describe(options):
    parts = [f"preempt={options['preempt']}" if options["preempt"] else "the defconfig preemption model"]
    if options["hz"]:
        parts.append(f"HZ={options['hz']}")
    if options["nohz_full"]:
        parts.append("NO_HZ_FULL")
    if options["sched_ext"]:
        parts.append("sched_ext")
    return ", ".join(parts)


def build_distribution_schedulers(name):
    """sched_ext schedulers for a fetched distribution kernel: tools/sched_ext of the kernel.org
    release named by its scx_source, built against the BTF in the distribution's own image."""
    source = catalog.KERNELS[name].get("scx_source")
    if not source:
        raise ValueError(f"{name} has no usable sched_ext; see the notes in schedlab/vm/catalog.py")
    kernel = STORE / "kernels" / name
    record_path = kernel / "kernel.json"
    if not record_path.exists():
        raise RuntimeError(f"{name} is not in the store; fetch it first")
    if (kernel / "scx").is_dir():
        say(f"{name}: schedulers already built")
        return
    with tempfile.TemporaryDirectory(prefix="schedlab-scx-") as work:
        tree = source_tree(source, Path(work))
        vmlinux = Path(work) / "vmlinux"
        with open(vmlinux, "wb") as output:
            subprocess.run(
                [str(tree / "scripts/extract-vmlinux"), str(kernel / "vmlinuz")], stdout=output, check=True
            )
        staging = Path(work) / "scx"
        built = build_schedulers(tree, vmlinux, staging)
        shutil.copytree(staging, kernel / "scx")
    record = json.loads(record_path.read_text())
    record.update(schedulers=built, schedulers_source=source)
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    say(f"{name}: schedulers {', '.join(built)} ready")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 -m schedlab.vm.build KERNEL", file=sys.stderr)
        return 2
    try:
        if catalog.kernel_kind(argv[0]) == "source":
            build_source(argv[0])
        else:
            build_distribution_schedulers(argv[0])
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
