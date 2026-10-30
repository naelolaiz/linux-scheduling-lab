"""PID 1 of a lab VM: mount the kernel interfaces, run one job, send its results out, power off.

The initramfs holds Alpine, Python, the static native build and this package; the job comes from
/lab/job.json. Results leave as one framed tar.gz on the second serial port, so the guest needs no
disk, network or kernel modules. Whatever happens, the guest powers off, which ends QEMU.
"""

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import termios
import time
import traceback
import tty

from ..environment import parse_cpu_list
from . import transport

LAB = Path("/lab")
RESULTS = Path("/results")
RESULT_PORT = "/dev/ttyS1"
CGROUP = Path("/sys/fs/cgroup")
# A delegated subtree for the cgroup preset and the opt-in cgroup checks.
CGROUP_PARENT = CGROUP / "lab"
MOUNTS = (
    ("proc", "/proc", "proc"),
    ("sysfs", "/sys", "sysfs"),
    ("devtmpfs", "/dev", "devtmpfs"),
    ("tmpfs", "/tmp", "tmpfs"),
    ("tmpfs", "/run", "tmpfs"),
    ("cgroup2", "/sys/fs/cgroup", "cgroup2"),
    # Root inside the guest may read the active preemption mode from debugfs.
    ("debugfs", "/sys/kernel/debug", "debugfs"),
)


def say(text):
    print(f"schedlab-vm: {text}", flush=True)


def mount_all():
    for source, target, kind in MOUNTS:
        Path(target).mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(["mount", "-t", kind, source, target], capture_output=True, text=True)
        if completed.returncode:
            say(f"mount {kind} on {target} failed: {completed.stderr.strip()}")


def delegate_cgroup():
    """Enable the cpu controller below the root and create the lab's delegated subtree."""
    (CGROUP / "cgroup.subtree_control").write_text("+cpu")
    CGROUP_PARENT.mkdir(exist_ok=True)
    (CGROUP_PARENT / "cgroup.subtree_control").write_text("+cpu")


def lab_environment():
    return dict(os.environ, HOME="/root", SCHEDLAB_NATIVE_DIR=str(LAB / "build/static"))


def command(job):
    """The lab command for a job, and the extra environment it needs."""
    if job["kind"] == "run":
        args = [sys.executable, "-m", "schedlab", "run", job["preset"], "--out", str(RESULTS / "run")]
        args += ["--repetitions", str(job["repetitions"]), "--label", job["label"]]
        if job.get("cpu") is not None:
            args += ["--cpu", str(job["cpu"])]
        if job.get("allow_rt"):
            args.append("--allow-rt")
        if job["preset"] == "cgroup":
            args += ["--cgroup-parent", str(CGROUP_PARENT)]
        return args, {}
    if job["kind"] == "test":
        # Root in a disposable guest: the opt-in checks run without touching the host.
        extra = {
            "SCHEDLAB_TEST_RT": "1",
            "SCHEDLAB_TEST_DEADLINE": "1",
            "SCHEDLAB_CGROUP_PARENT": str(CGROUP_PARENT),
        }
        return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], extra
    if job["kind"] == "doctor":
        return [sys.executable, "-m", "schedlab", "doctor", "--out", str(RESULTS / "doctor.json")], {}
    raise ValueError(f"unknown job kind: {job['kind']}")


def run_logged(args, env, log_path):
    """Run `args`, copying its output to the console and to `log_path`; return its exit status."""
    with open(log_path, "w") as log:
        process = subprocess.Popen(
            args, cwd=LAB, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        for line in process.stdout:
            sys.stdout.write(line)
            log.write(line)
        sys.stdout.flush()
        return process.wait()


class Scheduler:
    """An optional sched_ext BPF scheduler, enabled before the job and removed after it."""

    def __init__(self, spec):
        self.spec = spec
        self.process = None
        self.record = {"name": spec["name"], "status": "not started"}

    def state(self):
        try:
            return Path("/sys/kernel/sched_ext/state").read_text().strip()
        except OSError as exc:
            return f"unavailable: {exc}"

    def start(self):
        binary = LAB / "scx" / self.spec["name"]
        log = os.open(RESULTS / "scx.log", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            self.process = subprocess.Popen(
                [str(binary), *self.spec.get("args", [])], stdout=log, stderr=subprocess.STDOUT
            )
        finally:
            os.close(log)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.process.poll() is None:
            if self.state() == "enabled":
                ops = Path("/sys/kernel/sched_ext/root/ops")
                self.record.update(status="enabled", ops=ops.read_text().strip() if ops.exists() else None)
                say(f"sched_ext enabled: {self.record['ops']}")
                return True
            time.sleep(0.05)
        self.record.update(status="failed", state=self.state(), exit_code=self.process.poll())
        lines = (RESULTS / "scx.log").read_text(errors="replace").strip().splitlines()
        self.record["log_tail"] = lines[-3:]
        return False

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.process:
            self.record["state_after"] = self.state()


def run_job(job, summary):
    env = lab_environment()
    args, extra = command(job)
    env.update(extra)
    # isolcpus= removes CPUs from init's default mask; the job may use every online CPU, so a
    # matrix can place the measured task on an isolated one on purpose.
    summary["init_affinity"] = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, parse_cpu_list(Path("/sys/devices/system/cpu/online").read_text()))
    scheduler = Scheduler(job["scx"]) if job.get("scx") else None
    if job["kind"] == "test" or job.get("preset") == "cgroup":
        delegate_cgroup()
    try:
        if scheduler and not scheduler.start():
            detail = "; ".join(scheduler.record.get("log_tail", [])) or scheduler.record["state"]
            summary.update(status="error", reason=f"the sched_ext scheduler did not enable: {detail}")
            return
        say(f"running: {' '.join(args[1:])}")
        started = time.monotonic()
        status = run_logged(args, env, RESULTS / f"{job['kind']}.log")
        summary.update(exit_code=status, job_seconds=round(time.monotonic() - started, 3))
        if status:
            summary.update(status="error", reason=f"{job['kind']} exited with status {status}")
        else:
            summary["status"] = "ok"
            summary.pop("reason", None)
    finally:
        if scheduler:
            scheduler.stop()
            summary["sched_ext"] = scheduler.record


def send_results():
    """Write all results as one frame to the results port, in raw mode so no byte is translated."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=6) as archive:
        archive.add(RESULTS, arcname=".")
    frame = transport.encode(buffer.getvalue())
    port = os.open(RESULT_PORT, os.O_WRONLY | os.O_NOCTTY)
    try:
        tty.setraw(port)
        for offset in range(0, len(frame), 65536):
            os.write(port, frame[offset : offset + 65536])
        termios.tcdrain(port)
    finally:
        os.close(port)
    say(f"sent {len(frame)} bytes of results")


def power_off():
    os.sync()
    try:
        subprocess.run(["poweroff", "-f"], check=False)
    finally:
        # Still running: ask the kernel directly.
        Path("/proc/sysrq-trigger").write_text("o")


def main():
    # The kernel starts PID 1 without PATH; plain output keeps the console log readable.
    os.environ.update(PATH="/usr/sbin:/usr/bin:/sbin:/bin", TERM="dumb", NO_COLOR="1")
    summary = {"status": "error", "reason": "the guest stopped before running the job"}
    try:
        mount_all()
        RESULTS.mkdir(exist_ok=True)
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        job = json.loads((LAB / "job.json").read_text())
        summary.update(job=job, kernel=os.uname().release, boot_seconds=uptime)
        summary["cmdline"] = Path("/proc/cmdline").read_text().strip()
        say(f"kernel {summary['kernel']} booted in {uptime:.2f} s")
        run_job(job, summary)
    except Exception as exc:
        summary.update(status="error", reason=f"guest failure: {exc}", traceback=traceback.format_exc())
        say(summary["reason"])
    finally:
        try:
            (RESULTS / "guest.json").write_text(json.dumps(summary, indent=2) + "\n")
            send_results()
        except Exception as exc:
            say(f"could not send results: {exc}")
        power_off()
