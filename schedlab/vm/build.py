"""Inside the builder container: kernel.org releases into bootable lab kernels, with the sched_ext
example schedulers (the kernel's tools/sched_ext) when the name asks for sched_ext.

Usage: python3 -m schedlab.vm.build [--verify [--assets DIR]] NAME, where NAME is a source kernel
such as linux-7.2.13-rt-hz1000 (see catalog.parse_source), or a distribution kernel with a usable
sched_ext to build schedulers for. The tree is unpacked and built in the container's temporary
space; the store keeps the kernel image, its config, a record and any schedulers. The schedulers'
kfunc declarations must match the kernel's BTF, which for 7.0 and later needs pahole 1.31 or newer
when the kernel is built (the builder has it).

Builds are reproducible: the same source, options and toolchain give the same bytes on any machine.
The kernel gets the source's own date and a fixed user, host and build number instead of the build
machine's; every build runs in the same directory, so paths in debug and BPF line information
match; and the record holds no times. --verify rebuilds a published build (catalog.PREBUILT) outside
the store, packs it as its release asset and compares the two.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from . import catalog
from .machine import download, sha256_file

STORE = Path("/store")
WORK = Path("/tmp/schedlab-build")
BUILD_USER = "schedlab"
BUILD_HOST = "schedlab"
# Builder packages whose versions shape a build's bytes (clang and LLVM carry their major version in
# the package name); pahole is built from source in vm/Containerfile and recorded separately.
TOOLCHAIN = ("binutils", "busybox", "gcc", "musl-dev", "python3", "xz-libs")
VERSIONED_TOOLCHAIN = re.compile(r"(clang|llvm)\d+")
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


def parse_toolchain(installed):
    """Toolchain package versions from `apk list --installed` output."""
    versions = {}
    for line in installed.splitlines():
        name, version, release = line.split(" ", 1)[0].rsplit("-", 2)
        if name in TOOLCHAIN or VERSIONED_TOOLCHAIN.fullmatch(name):
            versions[name] = f"{version}-{release}"
    return versions


def toolchain():
    installed = subprocess.run(["apk", "list", "--installed"], capture_output=True, text=True, check=True)
    pahole = subprocess.run(["pahole", "--version"], capture_output=True, text=True, check=True)
    return dict(sorted({**parse_toolchain(installed.stdout), "pahole": pahole.stdout.strip()}.items()))


def build_environment(tree):
    """make's environment: the source's own date (its top-level Makefile's, as released) and a
    fixed user, host and build number in place of the build machine's."""
    epoch = int((tree / "Makefile").stat().st_mtime)
    # The format of `date`, which the kernel would otherwise call.
    stamp = datetime.fromtimestamp(epoch, timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
    return {
        **os.environ,
        "SOURCE_DATE_EPOCH": str(epoch),
        "KBUILD_BUILD_TIMESTAMP": stamp,
        "KBUILD_BUILD_USER": BUILD_USER,
        "KBUILD_BUILD_HOST": BUILD_HOST,
        "KBUILD_BUILD_VERSION": "1",
    }


@contextmanager
def workdir():
    """The build directory, the same for every build and emptied before and after."""
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    try:
        yield WORK
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


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


def source_tree(source, work):
    """A source tree unpacked under `work` (which must start empty)."""
    tarball = source_tarball(source)
    say(f"unpacking {tarball.name}")
    subprocess.run(["tar", "-xJf", str(tarball), "-C", str(work)], check=True)
    trees = [path for path in Path(work).iterdir() if path.is_dir()]
    if len(trees) != 1:
        raise RuntimeError(f"{tarball.name} does not unpack to a single directory")
    return trees[0]


def make(tree, *targets, environment):
    jobs = len(os.sched_getaffinity(0))
    subprocess.run(["make", "-C", str(tree), "-s", f"-j{jobs}", *targets], check=True, env=environment)


def build_schedulers(tree, vmlinux, destination, environment):
    """tools/sched_ext against the BTF in `vmlinux`; the example schedulers go to `destination`.
    Returns each scheduler's SHA-256 by name."""
    say(f"building sched_ext example schedulers: {', '.join(SCHEDULERS)}")
    subprocess.run(
        [
            "make",
            "-C",
            str(tree / "tools/sched_ext"),
            f"-j{len(os.sched_getaffinity(0))}",
            f"VMLINUX_BTF={vmlinux}",
            *SCHEDULERS,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        env=environment,
    )
    destination.mkdir(parents=True, exist_ok=True)
    built = {}
    for name in SCHEDULERS:
        binary = tree / "tools/sched_ext/build/bin" / name
        if binary.exists():
            shutil.copy2(binary, destination / name)
            built[name] = sha256_file(binary)
    if not built:
        raise RuntimeError("tools/sched_ext produced none of " + ", ".join(SCHEDULERS))
    return built


def build_source(name, target=None):
    """A source kernel, built into `target` (by default its place in the store)."""
    options = catalog.parse_source(name)
    target = target or STORE / "kernels" / name
    if (target / "kernel.json").exists():
        say(f"{name} is already built")
        return
    started = time.monotonic()
    with workdir() as work:
        tree = source_tree(options["version"], work)
        environment = build_environment(tree)
        make(tree, "defconfig", environment=environment)
        config = tree / ".config"
        fragment = parse_config((tree / "kernel/configs/kvm_guest.config").read_text())
        values = requested_options(options)
        set_options(config, {**fragment, **BASE_OPTIONS, **values})
        make(tree, "olddefconfig", environment=environment)
        # The named options are the experiment, so they must hold; base options are best effort.
        missing = unmet(config, values)
        if missing:
            raise RuntimeError(f"{name}: this release cannot provide {missing}")
        say(f"compiling {name} (several minutes)")
        make(tree, "bzImage", environment=environment)
        release = subprocess.run(
            ["make", "-C", str(tree), "-s", "kernelrelease"],
            capture_output=True,
            text=True,
            check=True,
            env=environment,
        ).stdout.strip()
        staging = target.with_name(f".{target.name}")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        shutil.copy2(tree / "arch/x86/boot/bzImage", staging / "vmlinuz")
        shutil.copy2(config, staging / "config")
        schedulers = {}
        if options["sched_ext"]:
            schedulers = build_schedulers(tree, tree / "vmlinux", staging / "scx", environment)
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
            "build_timestamp": environment["KBUILD_BUILD_TIMESTAMP"],
            "toolchain": toolchain(),
            "vmlinuz_sha256": sha256_file(staging / "vmlinuz"),
            "config_sha256": sha256_file(staging / "config"),
            "schedulers": schedulers,
        }
        (staging / "kernel.json").write_text(json.dumps(record, indent=2) + "\n")
        staging.rename(target)
    say(f"{name}: kernel {release} ready after {(time.monotonic() - started) / 60:.0f} min")


def _describe(options):
    parts = [f"preempt={options['preempt']}" if options["preempt"] else "the defconfig preemption model"]
    if options["hz"]:
        parts.append(f"HZ={options['hz']}")
    if options["nohz_full"]:
        parts.append("NO_HZ_FULL")
    if options["sched_ext"]:
        parts.append("sched_ext")
    return ", ".join(parts)


def build_distribution_schedulers(name, target=None):
    """sched_ext schedulers for a fetched distribution kernel, built from the tools/sched_ext of its
    scx_source against the BTF in the distribution's own image. `target` (by default the kernel's
    place in the store) receives scx/ and their record, schedulers.json."""
    source = catalog.KERNELS[name].get("scx_source")
    if not source:
        raise ValueError(f"{name} has no usable sched_ext; see the notes in schedlab/vm/catalog.py")
    kernel = STORE / "kernels" / name
    if not (kernel / "kernel.json").exists():
        raise RuntimeError(f"{name} is not in the store; fetch it first")
    target = target or kernel
    if (target / "schedulers.json").exists():
        say(f"{name}: schedulers already built")
        return
    with workdir() as work:
        tree = source_tree(source, work)
        environment = build_environment(tree)
        vmlinux = work / "vmlinux"
        with open(vmlinux, "wb") as output:
            subprocess.run(
                [str(tree / "scripts/extract-vmlinux"), str(kernel / "vmlinuz")], stdout=output, check=True
            )
        staging = work / "scx"
        built = build_schedulers(tree, vmlinux, staging, environment)
        target.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(target / "scx", ignore_errors=True)
        shutil.copytree(staging, target / "scx")
    record = {
        "name": name,
        "kind": "schedulers",
        "release": catalog.KERNELS[name]["release"],
        "kernel_vmlinuz_sha256": sha256_file(kernel / "vmlinuz"),
        "source": source if isinstance(source, str) else source["tarball"],
        "source_sha256": sha256_file(source_tarball(source)),
        "toolchain": toolchain(),
        "schedulers": built,
    }
    (target / "schedulers.json").write_text(json.dumps(record, indent=2) + "\n")
    say(f"{name}: schedulers {', '.join(built)} ready")


def pack(directory, destination):
    """`directory` as a reproducible .tar.xz: sorted entries owned by root, with fixed times and
    modes, so equal contents give an equal file."""
    with tarfile.open(destination, "w:xz", format=tarfile.USTAR_FORMAT, preset=6) as archive:
        for path in sorted([directory, *directory.rglob("*")]):
            info = archive.gettarinfo(str(path), arcname=path.relative_to(directory.parent).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o755 if info.isdir() or info.mode & 0o100 else 0o644
            if info.isfile():
                with open(path, "rb") as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
    return sha256_file(destination)


def published_record(name, package):
    """The record inside a published asset, or None when it cannot be downloaded."""
    with tempfile.TemporaryDirectory(prefix="schedlab-published-") as temp:
        asset = Path(temp) / package["asset"]
        try:
            download(package, asset)
        except RuntimeError:
            return None
        member = "kernel.json" if catalog.kernel_kind(name) == "source" else "schedulers.json"
        with tarfile.open(asset) as archive:
            return json.load(archive.extractfile(f"{name}/{member}"))


def differences(ours, theirs, prefix=""):
    """Keys, dotted for nested ones, whose values differ between two records."""
    lines = []
    for key in sorted(set(ours) | set(theirs)):
        mine, published = ours.get(key), theirs.get(key)
        if isinstance(mine, dict) and isinstance(published, dict):
            lines += differences(mine, published, f"{prefix}{key}.")
        elif mine != published:
            lines.append(f"{prefix}{key}: {mine} here, {published} published")
    return lines


def verify(name, assets=None):
    """Rebuild `name` from source outside the store, pack it as its release asset and compare it
    with the published SHA-256. With `assets`, the asset and the source it was built from (the
    GPL's corresponding source) are written there, matching or not. Returns whether it matched."""
    source_kind = catalog.kernel_kind(name) == "source"
    builder = build_source if source_kind else build_distribution_schedulers
    source = catalog.parse_source(name)["version"] if source_kind else catalog.KERNELS[name]["scx_source"]
    record = "kernel.json" if source_kind else "schedulers.json"
    with tempfile.TemporaryDirectory(prefix="schedlab-verify-") as temp:
        directory = Path(temp) / name
        builder(name, directory)
        packed = Path(temp) / "asset.tar.xz"
        sha256 = pack(directory, packed)
        asset = catalog.prebuilt_asset(name, sha256)
        if assets:
            assets.mkdir(parents=True, exist_ok=True)
            shutil.copy2(packed, assets / asset)
            shutil.copy2(source_tarball(source), assets)
        ours = json.loads((directory / record).read_text())
    package = catalog.prebuilt(name)
    if package is None:
        say(f"{name} has no published build; this build is {asset}, SHA-256 {sha256}")
        return False
    if sha256 in package["sha256"]:
        say(f"{name}: identical to the published build {package['asset']}")
        return True
    say(f"{name}: this build ({sha256}) differs from the published one ({package['sha256'][0]})")
    theirs = published_record(name, package)
    if theirs is None:
        say("the published asset could not be downloaded to compare their records")
        return False
    for line in differences(ours, theirs):
        print(f"  {line}")
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m schedlab.vm.build")
    parser.add_argument("kernel")
    parser.add_argument("--verify", action="store_true", help="rebuild a published build and compare")
    parser.add_argument("--assets", type=Path, help="with --verify: write the release assets here")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            return 0 if verify(args.kernel, args.assets) else 1
        if catalog.kernel_kind(args.kernel) == "source":
            build_source(args.kernel)
        else:
            build_distribution_schedulers(args.kernel)
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
