"""Kernels the lab can boot, and named matrices of VM configurations.

Distribution kernels are pinned packages (.deb or .rpm). Each package lists a primary URL and a
fallback that keeps it after the distribution moves on (snapshot.debian.org, Launchpad, Fedora's
Koji, Rocky's vault) and is verified against its SHA-256; only the kernel image and its config are
kept. Source kernels are kernel.org releases built with lab-chosen options; their names spell the
options (see `parse_source`). The builds the matrices need are also published (see `PREBUILT`).

Usage: python3 -m schedlab.vm.catalog prints the published builds and their assets as JSON (for CI).
"""

import json
import re

DEBIAN = "http://deb.debian.org/debian/"
DEBIAN_SECURITY = "http://security.debian.org/debian-security/"
# Snapshots serve the newest state at or before this time, so these URLs never change.
SNAPSHOT = "https://snapshot.debian.org/archive/{archive}/20261029T000000Z/"
UBUNTU = "http://archive.ubuntu.com/ubuntu/"
LAUNCHPAD = "https://launchpad.net/ubuntu/+archive/primary/+files/"
FEDORA = "https://dl.fedoraproject.org/pub/fedora/linux/updates/{release}/Everything/x86_64/Packages/k/"
KOJI = "https://kojipkgs.fedoraproject.org/packages/kernel/{version}/{build}/x86_64/"
# A Rocky minor release's packages move from pub/ to vault/ when the next minor release ships.
ROCKY = "https://dl.rockylinux.org/{tree}/rocky/{minor}/{repo}/{packages}/Packages/k/"


def _debian(path, sha256, archive="debian"):
    primary = DEBIAN if archive == "debian" else DEBIAN_SECURITY
    return {"urls": [primary + path, SNAPSHOT.format(archive=archive) + path], "sha256": [sha256]}


def _ubuntu(path, sha256):
    return {"urls": [UBUNTU + path, LAUNCHPAD + path.rsplit("/", 1)[1]], "sha256": [sha256]}


def _fedora(release, version, build, signed, unsigned):
    """Mirrors carry the signed package, Koji keeps the unsigned build: same files, two digests."""
    name = f"kernel-core-{version}-{build}.x86_64.rpm"
    urls = [FEDORA.format(release=release) + name, KOJI.format(version=version, build=build) + name]
    return {"urls": urls, "sha256": [signed, unsigned]}


def _rocky(minor, repo, name, sha256, packages="x86_64/os"):
    urls = [
        ROCKY.format(tree=tree, minor=minor, repo=repo, packages=packages) + name for tree in ("pub", "vault")
    ]
    return {"urls": urls, "sha256": [sha256]}


# Rocky backports sched_ext beyond its 6.12 base, so its schedulers come from its own patched
# source (the source package's tarball), not from any kernel.org release.
_ROCKY_10_SOURCE = {
    "package": _rocky(
        "10.2",
        "BaseOS",
        "kernel-6.12.0-211.69.1.el10_2.src.rpm",
        "313f173503c751cc23499b464c4d98e4ea90cde707514ed2ffaef70ff3b50e04",
        packages="source/tree",
    ),
    "tarball": "linux-6.12.0-211.69.1.el10_2.tar.xz",
}


_DEBIAN_6_12 = "pool/updates/main/l/linux-signed-amd64/linux-image-6.12.113+deb13-{}_6.12.113-1_amd64.deb"
_DEBIAN_7_2_BINARY = "pool/main/l/linux-signed-amd64/linux-binary-7.2.11+deb13-{}_7.2.11-1~bpo13+1_amd64.deb"
_DEBIAN_7_2_BASE = "pool/main/l/linux/linux-base-7.2.11+deb13-{}_7.2.11-1~bpo13+1_amd64.deb"

# image/config: (package, member path inside it); a config package of "image" means the image's
# package. "scx_source" is the source whose tools/sched_ext builds the example BPF schedulers
# against this kernel's BTF: a kernel.org release, or the distribution's own source package for a
# backported sched_ext. The Debian 7.2 kernels enable sched_ext but have none:
# their BTF was generated with pahole 1.30, which leaves the implicit arguments of 7.x kfuncs in
# the prototypes, so no scheduler loads on them. Fedora 44 (7.2) and Rocky 10 (6.12) are usable.
_FEDORA_44 = "7.2.13-200.fc44.x86_64"
_ROCKY_9 = "5.14.0-687.61.1.el9_8.x86_64"
_ROCKY_10 = "6.12.0-211.69.1.el10_2.x86_64"
KERNELS = {
    "ubuntu-5.4": {
        "description": "Ubuntu 20.04 5.4: CFS before EEVDF, voluntary preemption, no PREEMPT_DYNAMIC",
        "release": "5.4.0-216-generic",
        "image": (
            _ubuntu(
                "pool/main/l/linux-signed/linux-image-5.4.0-216-generic_5.4.0-216.236_amd64.deb",
                "9ab123036173136d766f49cbcd7decb7ab3da629985abd2c7b12a733b1601514",
            ),
            "boot/vmlinuz-5.4.0-216-generic",
        ),
        "config": (
            _ubuntu(
                "pool/main/l/linux/linux-modules-5.4.0-216-generic_5.4.0-216.236_amd64.deb",
                "6f8da7e233a20a6aafe9bb2c03d7394026a6253c5d5056a4be4d2264ab2e5f45",
            ),
            "boot/config-5.4.0-216-generic",
        ),
    },
    "debian-6.1": {
        "description": "Debian 12 6.1: the last CFS long-term kernel; PREEMPT_DYNAMIC (none/voluntary/full)",
        "release": "6.1.0-54-amd64",
        "image": (
            _debian(
                "pool/updates/main/l/linux-signed-amd64/linux-image-6.1.0-54-amd64_6.1.190-1_amd64.deb",
                "d788f148714b4cec6a9ff5e66282f56d9a7a0c2c12ac3e5093de12abaf47f56e",
                archive="debian-security",
            ),
            "boot/vmlinuz-6.1.0-54-amd64",
        ),
        "config": ("image", "boot/config-6.1.0-54-amd64"),
    },
    "debian-6.12": {
        "description": "Debian 13 6.12: EEVDF; PREEMPT_DYNAMIC (none/voluntary/full)",
        "release": "6.12.113+deb13-amd64",
        "image": (
            _debian(
                _DEBIAN_6_12.format("amd64"),
                "a4ad0bf9f084b199dd2b12cb8a2ed918abb442da467f40b772166a267c784a5b",
                archive="debian-security",
            ),
            "boot/vmlinuz-6.12.113+deb13-amd64",
        ),
        "config": ("image", "boot/config-6.12.113+deb13-amd64"),
    },
    "debian-6.12-rt": {
        "description": "Debian 13 6.12 with PREEMPT_RT, the first mainline RT release",
        "release": "6.12.113+deb13-rt-amd64",
        "image": (
            _debian(
                _DEBIAN_6_12.format("rt-amd64"),
                "9339ef214a66dc4264deb34ff979ad987d6be05d36a0488d14cdeb5696da6825",
                archive="debian-security",
            ),
            "boot/vmlinuz-6.12.113+deb13-rt-amd64",
        ),
        "config": ("image", "boot/config-6.12.113+deb13-rt-amd64"),
    },
    "debian-7.2": {
        "description": "Debian 13 backports 7.2: lazy preemption by default, PREEMPT_DYNAMIC",
        "release": "7.2.11+deb13-amd64",
        "image": (
            _debian(
                _DEBIAN_7_2_BINARY.format("amd64"),
                "4177643521605976abdd8735ddcf05320a5e9f08c6c625790a3751fc1c3fe171",
            ),
            "usr/lib/modules/7.2.11+deb13-amd64/vmlinuz",
        ),
        "config": (
            _debian(
                _DEBIAN_7_2_BASE.format("amd64"),
                "4b163432fd63dc295e8dcc366c38122e2c7e7d5a5cdcd31426f6f957c6defbc2",
            ),
            "usr/lib/modules/7.2.11+deb13-amd64/config",
        ),
    },
    "debian-7.2-rt": {
        "description": "Debian 13 backports 7.2 with PREEMPT_RT",
        "release": "7.2.11+deb13-rt-amd64",
        "image": (
            _debian(
                _DEBIAN_7_2_BINARY.format("rt-amd64"),
                "6f165dc04e16babde22bc1495de1fc9546b77d31879ae493a37a484e54834cfd",
            ),
            "usr/lib/modules/7.2.11+deb13-rt-amd64/vmlinuz",
        ),
        "config": (
            _debian(
                _DEBIAN_7_2_BASE.format("rt-amd64"),
                "8ed024aa429588c5bea05c2049cfdb0eac9def8c2d35651b5a7d2e0cbacfef62",
            ),
            "usr/lib/modules/7.2.11+deb13-rt-amd64/config",
        ),
    },
    "fedora-44": {
        "description": "Fedora 44 7.2: lazy preemption, PREEMPT_DYNAMIC, HZ 1000, steal accounting, scx",
        "release": _FEDORA_44,
        "scx_source": "7.2.13",
        "image": (
            _fedora(
                "44",
                "7.2.13",
                "200.fc44",
                "92a31e25873080a1cf4e78fee033197320d036baae9a816a640946913ff3234d",
                "a27faf7d45a599f43d752cd7a292b8b8c44eb1fdbff0b164ecd77e03606e5a14",
            ),
            f"lib/modules/{_FEDORA_44}/vmlinuz",
        ),
        "config": ("image", f"lib/modules/{_FEDORA_44}/config"),
    },
    "rocky-9": {
        "description": "Rocky 9.8 5.14: an enterprise kernel with years of backports, HZ 1000",
        "release": _ROCKY_9,
        "image": (
            _rocky(
                "9.8",
                "BaseOS",
                f"kernel-core-{_ROCKY_9}.rpm",
                "9640c0552701c24c3074592a32f01ebb1be266876f78a37c05eaa2edde42014f",
            ),
            f"lib/modules/{_ROCKY_9}/vmlinuz",
        ),
        "config": ("image", f"lib/modules/{_ROCKY_9}/config"),
    },
    "rocky-9-rt": {
        "description": "Rocky 9.8 5.14 kernel-rt: PREEMPT_RT with lazy preemption backported",
        "release": f"{_ROCKY_9}+rt",
        "image": (
            _rocky(
                "9.8",
                "RT",
                f"kernel-rt-core-{_ROCKY_9}.rpm",
                "d19dc8a9c594c56aa6dd63d61afefb0a98ba6dde0df75854885df66069a793bf",
            ),
            f"lib/modules/{_ROCKY_9}+rt/vmlinuz",
        ),
        "config": ("image", f"lib/modules/{_ROCKY_9}+rt/config"),
    },
    "rocky-10": {
        "description": "Rocky 10.2 6.12: enterprise backports, PREEMPT_DYNAMIC, HZ 1000, sched_ext",
        "release": _ROCKY_10,
        "scx_source": _ROCKY_10_SOURCE,
        "image": (
            _rocky(
                "10.2",
                "BaseOS",
                f"kernel-core-{_ROCKY_10}.rpm",
                "d87daadc1738b7caaf9c889ce98ebd6ebfcfc70af1b405cb996a82ce9fc61440",
            ),
            f"lib/modules/{_ROCKY_10}/vmlinuz",
        ),
        "config": ("image", f"lib/modules/{_ROCKY_10}/config"),
    },
    "rocky-10-rt": {
        "description": "Rocky 10.2 6.12 kernel-rt: PREEMPT_RT, HZ 1000, sched_ext",
        "release": f"{_ROCKY_10}+rt",
        "scx_source": _ROCKY_10_SOURCE,
        "image": (
            _rocky(
                "10.2",
                "RT",
                f"kernel-rt-core-{_ROCKY_10}.rpm",
                "cbf024b11d6106afc932900b113d322fa632dc7c084600e27f0fdceba52d0635",
            ),
            f"lib/modules/{_ROCKY_10}+rt/vmlinuz",
        ),
        "config": ("image", f"lib/modules/{_ROCKY_10}+rt/config"),
    },
}

# kernel.org tarballs, checked against these SHA-256 sums (from kernel.org's sha256sums.asc).
# 7.2.11 matches the Debian 7.2 kernels: the same release with the lab's config instead of theirs.
SOURCES = {
    "7.2.13": "08e7b0c4f2dd1971f972da274b5e589a98989390ac56acd7a1a5af922363ea6c",
    "7.2.11": "fd0fc479f983a8c05313f13e9c36330fe6651cf725396748c40d5cd93fa1e6a4",
    "6.18.59": "53a55ac9863a5547e292b8e381d4e4c2240dd1127d5428a7c44dbc550be23963",
    "6.12.114": "7a8589912bd7a8adb07fed074283652cf482a35facef8954554654f0154b4af3",
}
PREEMPT_MODELS = ("none", "voluntary", "full", "lazy", "rt")
HZ_VALUES = (100, 250, 300, 1000)
SOURCE_NAME = re.compile(
    r"linux-(?P<version>\d+\.\d+(?:\.\d+)?)"
    r"(?:-(?P<preempt>none|voluntary|full|lazy|rt))?"
    r"(?:-hz(?P<hz>\d+))?"
    r"(?P<nohz_full>-nohzfull)?"
    r"(?P<sched_ext>-scx)?"
)


def source_url(version):
    return f"https://cdn.kernel.org/pub/linux/kernel/v{version.split('.')[0]}.x/linux-{version}.tar.xz"


def parse_source(name):
    """Options of a source kernel name such as linux-7.2.13-rt-hz1000-nohzfull-scx, or None.

    Omitted options keep the x86-64 defconfig choice; the built config is recorded either way.
    """
    match = SOURCE_NAME.fullmatch(name)
    if not match:
        return None
    hz = int(match["hz"]) if match["hz"] else None
    if hz is not None and hz not in HZ_VALUES:
        raise ValueError(f"HZ must be one of {', '.join(map(str, HZ_VALUES))}")
    return {
        "version": match["version"],
        "preempt": match["preempt"],
        "hz": hz,
        "nohz_full": bool(match["nohz_full"]),
        "sched_ext": bool(match["sched_ext"]),
    }


def kernel_kind(name):
    if name in KERNELS:
        return "distribution"
    if parse_source(name):
        return "source"
    raise ValueError(f"unknown kernel '{name}': use one of {', '.join(KERNELS)} or linux-VERSION[-options]")


# The lab's own builds, published as assets of one GitHub release by .github/workflows/kernels.yml
# and pinned here by each asset's SHA-256: a source kernel whole, a distribution kernel's sched_ext
# schedulers only (the kernel itself comes from the distribution). The builds are reproducible, so
# `schedlab vm verify NAME` rebuilds one and must arrive at the same file; it also prints the digest
# of a new or changed build. Asset names carry the start of their digest, so a new build never
# replaces one that an older checkout pins.
PREBUILT_URL = "https://github.com/naelolaiz/linux-scheduling-lab/releases/download/kernels/"
PREBUILT = {
    "linux-7.2.13-hz100": "1d045a9a31b2f174f0b2e4cc1cd0bf01899771be9930acada191807fea1fb287",
    "linux-7.2.13-hz250": "3563219f6d56fb1f574cce395d559bfad883955e092803e6cfc7596a55775dc3",
    "linux-7.2.13-hz1000": "179a4b74cdcee47a3bdca76fb168e2ab4e73a73ea77564209064d70e33a53772",
    "linux-7.2.13-scx": "731b2bda8c45b6d23b48f79470a4533cda2806aa5e252fed8af8696c83a958ba",
    "fedora-44": "543133aa72c0750b6663cc6baae90686e411fe1f24140f354476d9f1db1a09e8",
    "rocky-10": "3186f5e44e78ae472a7ab0cc30920041b4e3f886a4b050a928a0b8f7224b996f",
    "rocky-10-rt": "a11f6df4ecea40c5f642caee6832eefaaa544b1c9d6eae8cad19580e3066b262",
}


def prebuilt_asset(name, sha256):
    contents = "" if kernel_kind(name) == "source" else "-schedulers"
    return f"{name}{contents}-{sha256[:16]}.tar.xz"


def prebuilt(name):
    """The published build of `name` as a package for machine.download, or None."""
    sha256 = PREBUILT.get(name)
    if sha256 is None:
        return None
    asset = prebuilt_asset(name, sha256)
    return {"asset": asset, "urls": [PREBUILT_URL + asset], "sha256": [sha256]}


# One VM configuration: kernel, extra boot parameters, CPU topology and placement, host-side load
# and an optional sched_ext scheduler. A matrix runs the same preset in each.
MATRICES = {
    "kernels": {
        "description": "Kernel generations, default boot: CFS 5.4 and 6.1, EEVDF 6.12 and 7.2",
        "entries": [
            {"name": "ubuntu-5.4", "kernel": "ubuntu-5.4"},
            {"name": "debian-6.1", "kernel": "debian-6.1"},
            {"name": "debian-6.12", "kernel": "debian-6.12"},
            {"name": "debian-7.2", "kernel": "debian-7.2"},
        ],
    },
    "distributions": {
        "description": "Rocky 5.14 and 6.12 against Debian 6.12; 7.2 from Debian (HZ 250) and Fedora (1000)",
        "entries": [
            {"name": "rocky-9", "kernel": "rocky-9"},
            {"name": "debian-6.12", "kernel": "debian-6.12"},
            {"name": "rocky-10", "kernel": "rocky-10"},
            {"name": "debian-7.2", "kernel": "debian-7.2"},
            {"name": "fedora-44", "kernel": "fedora-44"},
        ],
    },
    "preemption": {
        # Rocky 10 offers all four models at boot; 7.x kernels on x86 offer only full and lazy.
        "description": "One Rocky 10 kernel booted with each preemption model, then its PREEMPT_RT build",
        "entries": [
            *(
                {"name": model, "kernel": "rocky-10", "boot": f"preempt={model}"}
                for model in PREEMPT_MODELS[:4]
            ),
            {"name": "rt", "kernel": "rocky-10-rt"},
        ],
    },
    "rt": {
        "description": "Ordinary and PREEMPT_RT builds: Rocky 5.14 and 6.12, Debian 6.12 and 7.2",
        "entries": [
            {"name": "rocky-9", "kernel": "rocky-9"},
            {"name": "rocky-9-rt", "kernel": "rocky-9-rt"},
            {"name": "rocky-10", "kernel": "rocky-10"},
            {"name": "rocky-10-rt", "kernel": "rocky-10-rt"},
            {"name": "debian-6.12", "kernel": "debian-6.12"},
            {"name": "debian-6.12-rt", "kernel": "debian-6.12-rt"},
            {"name": "debian-7.2", "kernel": "debian-7.2"},
            {"name": "debian-7.2-rt", "kernel": "debian-7.2-rt"},
        ],
    },
    "isolation": {
        "description": "The measured CPU 2 shared, with adaptive ticks, and fully isolated",
        "entries": [
            {"name": "shared", "kernel": "debian-7.2", "cpu": 2},
            {"name": "nohz-full", "kernel": "debian-7.2", "cpu": 2, "boot": "nohz_full=2-3 rcu_nocbs=2-3"},
            {
                "name": "isolated",
                "kernel": "debian-7.2",
                "cpu": 2,
                "boot": "isolcpus=domain,managed_irq,2-3 nohz_full=2-3 rcu_nocbs=2-3 irqaffinity=0-1",
            },
        ],
    },
    "topology": {
        "description": "Four vCPUs as four cores, as two SMT cores, and as two NUMA nodes",
        "entries": [
            {"name": "cores", "kernel": "debian-7.2", "smp": {"cpus": 4, "cores": 4, "threads": 1}},
            {"name": "smt", "kernel": "debian-7.2", "smp": {"cpus": 4, "cores": 2, "threads": 2}},
            {"name": "numa", "kernel": "debian-7.2", "smp": {"cpus": 4, "cores": 4, "threads": 1}, "numa": 2},
        ],
    },
    "neighbour": {
        # Fedora's kernel keeps stolen time out of task CPU time (PARAVIRT_TIME_ACCOUNTING).
        "description": "Pinned vCPUs on a quiet host, then with host load stealing their CPUs",
        "entries": [
            {"name": "quiet", "kernel": "fedora-44", "pin": "auto"},
            {"name": "host-cpu", "kernel": "fedora-44", "pin": "auto", "neighbour": "cpu"},
            {"name": "host-wakeups", "kernel": "fedora-44", "pin": "auto", "neighbour": "wakeups"},
        ],
    },
    "sched_ext": {
        "description": "EEVDF against two sched_ext example BPF schedulers",
        "entries": [
            {"name": "eevdf", "kernel": "fedora-44"},
            {"name": "scx_simple", "kernel": "fedora-44", "scx": "scx_simple"},
            {"name": "scx_qmap", "kernel": "fedora-44", "scx": "scx_qmap"},
        ],
    },
    "timer-hz": {
        "description": "One source release built with HZ 100, 250 and 1000",
        "entries": [{"name": f"hz{hz}", "kernel": f"linux-7.2.13-hz{hz}"} for hz in (100, 250, 1000)],
    },
}


def main():
    print(json.dumps([{"name": name, "asset": prebuilt(name)["asset"]} for name in PREBUILT], indent=2))


if __name__ == "__main__":
    main()
