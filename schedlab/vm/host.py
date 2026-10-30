"""`schedlab vm`: run presets and opt-in checks inside QEMU/KVM guests through podman.

The host needs podman and /dev/kvm only. Images come from vm/Containerfile; kernels, sources and
build results live in the podman volume `schedlab-vm` (or the directory SCHEDLAB_VM_STORE names),
so nothing lands in the checkout or the home directory. `podman volume rm schedlab-vm` removes it.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from . import catalog

ROOT = Path(__file__).resolve().parents[2]
TOOLS_IMAGE = "localhost/schedlab-vm"
BUILDER_IMAGE = "localhost/schedlab-vm-builder"
VOLUME = "schedlab-vm"


def say(text):
    print(f"schedlab vm: {text}", flush=True)


def store_mount():
    path = os.environ.get("SCHEDLAB_VM_STORE")
    if path:
        Path(path).mkdir(parents=True, exist_ok=True)
        return f"{Path(path).resolve()}:/store"
    return f"{VOLUME}:/store"


def build_image(target, tag):
    """Build (or reuse from podman's cache) one stage of vm/Containerfile."""
    completed = subprocess.run(
        ["podman", "build", "-f", str(ROOT / "vm/Containerfile"), "--target", target, "-t", tag, str(ROOT)],
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        sys.stderr.write(completed.stdout[-4000:] + completed.stderr[-4000:])
        raise RuntimeError(f"podman build of the {target} image failed")


def kvm_options(accel):
    if accel != "kvm":
        return []
    device = Path("/dev/kvm")
    if not device.exists():
        raise RuntimeError(
            "/dev/kvm is missing: enable virtualization, or pass --accel tcg (slow; timings meaningless)"
        )
    options = ["--device", str(device)]
    # Access through the kvm group needs the caller's groups inside a rootless container.
    if stat.S_IMODE(device.stat().st_mode) & 0o006 != 0o006:
        options += ["--group-add", "keep-groups"]
    return options


def container(image, args, mounts=(), accel=None):
    command = ["podman", "run", "--rm", "-v", store_mount(), *kvm_options(accel)]
    for mount in mounts:
        command += ["-v", mount]
    return subprocess.run([*command, image, "python3", "-m", *args]).returncode


def checkout():
    """Commit and worktree state of this checkout, which the guest cannot see."""
    facts = {}
    for key, args in (
        ("git_commit", ["rev-parse", "HEAD"]),
        ("git_worktree_status", ["status", "--porcelain"]),
    ):
        try:
            completed = subprocess.run(
                ["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=5
            )
            facts[key] = completed.stdout.strip() if completed.returncode == 0 else "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            facts[key] = "unavailable"
    return facts


def stored_kernels():
    """Records of the kernels already in the store, by name."""
    command = ["podman", "run", "--rm", "-v", store_mount(), TOOLS_IMAGE]
    completed = subprocess.run(
        [*command, "python3", "-m", "schedlab.vm.machine", "list"], capture_output=True, text=True
    )
    if completed.returncode:
        raise RuntimeError(f"cannot list the store: {completed.stderr.strip()}")
    return {record["name"]: record for record in json.loads(completed.stdout)}


def ensure_kernels(names, scx=()):
    """Fetch missing distribution kernels, build missing source kernels, and build the sched_ext
    schedulers of the distribution kernels in `scx` (source kernels build their own)."""
    build_image("tools", TOOLS_IMAGE)
    stored = stored_kernels()
    missing = {name for name in names if name not in stored}
    fetch = sorted(name for name in missing if catalog.kernel_kind(name) == "distribution")
    if fetch and container(TOOLS_IMAGE, ["schedlab.vm.machine", "fetch", *fetch]):
        raise RuntimeError("kernel download failed")
    sources = {name for name in missing if catalog.kernel_kind(name) == "source"}
    builds = sorted(sources | {name for name in scx if not stored.get(name, {}).get("schedulers")})
    if builds:
        say(f"building {', '.join(builds)}: this takes minutes and is kept in the store")
        build_image("builder", BUILDER_IMAGE)
        for name in builds:
            if container(BUILDER_IMAGE, ["schedlab.vm.build", name]):
                raise RuntimeError(f"build of {name} failed")


def output_dir(out, label):
    if out is None:
        out = Path("results") / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-vm-" + label)
    out = Path(out).resolve()
    if out.exists():
        raise ValueError(f"{out} already exists; choose a new output directory")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def boot(spec, out):
    """One VM run in the tools container; returns the guest's success."""
    spec = dict(spec, checkout=checkout())
    code = container(
        TOOLS_IMAGE,
        ["schedlab.vm.machine", "boot", "--spec", json.dumps(spec), "--out", f"/out/{out.name}"],
        mounts=[f"{out.parent}:/out"],
        accel=spec.get("accel", "kvm"),
    )
    return code == 0


def vm_spec(args, entry=None):
    """A machine.boot spec from command-line options, overridden by a matrix entry."""
    entry = entry or {}
    spec = {
        "name": entry.get("name", args.kernel),
        "kernel": entry.get("kernel", args.kernel),
        "boot": " ".join(part for part in (entry.get("boot"), args.boot) if part),
        "smp": entry.get("smp") or {"cpus": args.smp, "threads": args.threads},
        "numa": entry.get("numa", args.numa),
        "memory_mb": args.memory,
        "pin": entry.get("pin", args.pin),
        "neighbour": entry.get("neighbour", args.neighbour),
        "scx": entry.get("scx", args.scx),
        "cpu": entry.get("cpu", args.cpu),
        "accel": args.accel,
        "timeout_s": args.timeout,
    }
    if spec["neighbour"] and not spec["pin"]:
        raise ValueError("--neighbour needs --pin: the load must share the vCPUs' host CPUs")
    if spec["scx"] and not scx_capable(spec["kernel"]):
        usable = ", ".join(name for name in catalog.KERNELS if scx_capable(name))
        raise ValueError(
            f"--scx needs a kernel with usable sched_ext ({usable}, or a source kernel named with -scx), "
            f"not {spec['kernel']}"
        )
    return spec


def scx_capable(kernel):
    if catalog.kernel_kind(kernel) == "distribution":
        return bool(catalog.KERNELS[kernel].get("scx_source"))
    return catalog.parse_source(kernel)["sched_ext"]


def scx_builds(specs):
    """Distribution kernels whose schedulers the specs need; source kernels build their own."""
    return sorted(
        {
            spec["kernel"]
            for spec in specs
            if spec["scx"] and catalog.kernel_kind(spec["kernel"]) == "distribution"
        }
    )


def run_job(args):
    return {
        "kind": "run",
        "preset": args.preset,
        "repetitions": args.repetitions,
        "allow_rt": args.allow_rt,
        "label": args.label or f"vm {args.preset}",
    }


def command_kernels(_args):
    print("Distribution kernels (fetched on first use, verified by SHA-256):")
    for name, entry in catalog.KERNELS.items():
        print(f"  {name:16} {entry['release']:26} {entry['description']}")
    print("\nSource kernels (built on first use): linux-VERSION[-PREEMPT][-hzN][-nohzfull][-scx]")
    print(f"  VERSION: {', '.join(catalog.SOURCES)}; PREEMPT: {', '.join(catalog.PREEMPT_MODELS)}; ", end="")
    print(f"N: {', '.join(map(str, catalog.HZ_VALUES))}")
    print("\nMatrices (python3 -m schedlab vm matrix NAME PRESET):")
    for name, matrix in catalog.MATRICES.items():
        print(f"  {name:12} {matrix['description']}")
    print("\nIn the store:")
    build_image("tools", TOOLS_IMAGE)
    return container(TOOLS_IMAGE, ["schedlab.vm.machine", "list"])


def command_fetch(args):
    ensure_kernels(args.kernels)
    return 0


def command_build(args):
    if catalog.kernel_kind(args.kernel) == "source":
        ensure_kernels([args.kernel])
    elif scx_capable(args.kernel):
        ensure_kernels([args.kernel], scx=[args.kernel])
    else:
        raise ValueError(f"{args.kernel} is a distribution kernel without usable sched_ext: `vm fetch` it")
    return 0


def command_run(args):
    spec = vm_spec(args)
    ensure_kernels([spec["kernel"]], scx=scx_builds([spec]))
    out = output_dir(args.out, spec["name"])
    spec["job"] = run_job(args)
    ok = boot(spec, out)
    print(out)
    return 0 if ok else 1


def command_matrix(args):
    from ..analysis import analyze, render

    matrix = catalog.MATRICES[args.matrix]
    specs = [vm_spec(args, entry) for entry in matrix["entries"]]
    ensure_kernels([spec["kernel"] for spec in specs], scx=scx_builds(specs))
    out = output_dir(args.out, f"{args.matrix}-{args.preset}")
    out.mkdir()
    failures = 0
    for spec in specs:
        spec["job"] = dict(run_job(args), label=f"{args.matrix}: {spec['name']} · {args.preset}")
        failures += not boot(spec, out / spec["name"])
    runs = [out / spec["name"] for spec in specs if (out / spec["name"] / "run.json").exists()]
    if runs:
        print(render(analyze(runs), "text"), end="")
    print(out)
    return 1 if failures else 0


def command_test(args):
    spec = vm_spec(args)
    ensure_kernels([spec["kernel"]], scx=scx_builds([spec]))
    out = output_dir(args.out, f"test-{spec['name']}")
    spec["job"] = {"kind": "test"}
    ok = boot(spec, out)
    print(out / "vm" / "test.log")
    return 0 if ok else 1


def add_parser(commands):
    """The `vm` subcommand of the schedlab CLI."""
    vm = commands.add_parser("vm", help="run presets and opt-in checks inside QEMU/KVM guests (podman)")
    actions = vm.add_subparsers(dest="vm_command", required=True)
    actions.add_parser("kernels", help="list bootable kernels, matrices and the store's contents")
    fetcher = actions.add_parser("fetch", help="download distribution kernels into the store")
    fetcher.add_argument("kernels", nargs="+", choices=catalog.KERNELS)
    builder = actions.add_parser(
        "build",
        help="build a source kernel (e.g. linux-7.2.13-rt-hz1000) or a distribution kernel's schedulers",
    )
    builder.add_argument("kernel")

    def machine_options(parser, kernel=True):
        if kernel:
            parser.add_argument("--kernel", default="fedora-44", help="see `vm kernels` (default fedora-44)")
        parser.add_argument("--boot", default="", help="extra kernel parameters, e.g. preempt=full")
        parser.add_argument("--smp", type=int, default=4, help="vCPUs (default 4)")
        parser.add_argument("--threads", type=int, choices=(1, 2), default=1, help="SMT threads per core")
        parser.add_argument("--numa", type=int, help="split vCPUs and memory into this many NUMA nodes")
        parser.add_argument("--memory", type=int, default=2048, help="guest memory in MiB")
        parser.add_argument("--pin", help="host CPUs for the vCPUs, e.g. 4-7, or 'auto'")
        parser.add_argument("--neighbour", choices=("cpu", "wakeups", "bursts", "memory"), help="host load")
        parser.add_argument(
            "--scx", choices=("scx_simple", "scx_qmap", "scx_central"), help="sched_ext scheduler"
        )
        parser.add_argument("--cpu", type=int, help="measured guest CPU")
        parser.add_argument("--accel", choices=("kvm", "tcg"), default="kvm")
        parser.add_argument("--timeout", type=int, default=900, help="seconds before the VM is killed")
        parser.add_argument("--out", type=Path)

    def preset_options(parser):
        from ..runner import PRESETS

        parser.add_argument("preset", choices=PRESETS)
        parser.add_argument("--repetitions", type=int, default=3, choices=range(1, 11), metavar="1..10")
        parser.add_argument("--allow-rt", action="store_true", help="RT inside the guest only")
        parser.add_argument("--label")

    runner = actions.add_parser("run", help="run a preset in one VM")
    preset_options(runner)
    machine_options(runner)
    matrix = actions.add_parser("matrix", help="run a preset in each VM of a named matrix")
    matrix.add_argument("matrix", choices=catalog.MATRICES)
    preset_options(matrix)
    machine_options(matrix, kernel=False)
    tester = actions.add_parser("test", help="the test suite as root in a VM, opt-in checks included")
    machine_options(tester)


def main(args):
    handlers = {
        "kernels": command_kernels,
        "fetch": command_fetch,
        "build": command_build,
        "run": command_run,
        "matrix": command_matrix,
        "test": command_test,
    }
    if args.vm_command == "matrix":
        args.kernel = None
    return handlers[args.vm_command](args)
