import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from .environment import capture
from .report import compare, write_report
from .runner import PRESETS, run


def bounded_repetitions(value):
    number = int(value)
    if not 1 <= number <= 10:
        raise argparse.ArgumentTypeError("repetitions must be in [1, 10]")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Measured Linux scheduling labs; no automatic privilege escalation."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="read-only kernel, CPU, cgroup and permission inventory")
    doctor.add_argument("--out", type=Path, help="write JSON here instead of stdout")
    experiment = commands.add_parser("run", help="run bounded workloads and generate an offline report")
    experiment.add_argument("preset", choices=PRESETS)
    experiment.add_argument("--out", type=Path)
    experiment.add_argument("--repetitions", type=bounded_repetitions, default=3)
    experiment.add_argument("--cpu", type=int)
    experiment.add_argument(
        "--allow-rt", action="store_true", help="explicitly opt in; native safety checks still apply"
    )
    experiment.add_argument("--cgroup-parent", type=Path)
    experiment.add_argument("--label", help="experiment label, e.g. kernel boot and hardware description")
    report = commands.add_parser("report", help="regenerate HTML from an existing run manifest")
    report.add_argument("manifest", type=Path)
    report.add_argument("--out", type=Path, required=True)
    comparison = commands.add_parser("compare", help="compare saved runs across boots or machines")
    comparison.add_argument("runs", type=Path, nargs="+")
    comparison.add_argument("--out", type=Path, required=True)
    replay = commands.add_parser(
        "replay", help="repeat a saved preset; never execute commands from a manifest"
    )
    replay.add_argument("manifest", type=Path)
    replay.add_argument("--out", type=Path, required=True)
    replay.add_argument("--cpu", type=int, help="explicitly remap placement on a different machine")
    replay.add_argument(
        "--allow-rt", action="store_true", help="renew RT consent; never inherited from saved JSON"
    )
    replay.add_argument("--cgroup-parent", type=Path, help="renew cgroup delegation; never inherited")
    replay.add_argument("--label")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            text = json.dumps(capture(), indent=2) + "\n"
            if args.out:
                args.out.write_text(text)
            else:
                print(text, end="")
            return 0
        if args.command == "report":
            manifest = json.loads(args.manifest.read_text())
            if manifest.get("schema_version") != 1:
                raise ValueError("unsupported manifest schema")
            write_report(manifest, args.out)
            print(args.out)
            return 0
        if args.command == "compare":
            print(compare(args.runs, args.out) / "report.html")
            return 0
        if args.command == "replay":
            source = json.loads(args.manifest.read_text())
            if source.get("schema_version") != 1 or source.get("preset") not in PRESETS:
                raise ValueError("not a supported experiment manifest")
            repetitions = source.get("repetitions")
            if type(repetitions) is not int or not 1 <= repetitions <= 10:
                raise ValueError("manifest repetitions must be an integer in [1, 10]")
            args.preset = source["preset"]
            args.repetitions = repetitions
            if args.cpu is None:
                args.cpu = source["selected_cpu"]
                if type(args.cpu) is not int:
                    raise ValueError("manifest selected_cpu must be an integer")
            print("Replaying preset with this checkout's implementation; compare version and binary hashes.")
        output = args.out or Path("results") / (
            datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + args.preset
        )
        manifest = run(
            args.preset, output, args.repetitions, args.cpu, args.allow_rt, args.cgroup_parent, args.label
        )
        states = {
            state: sum(r["status"] == state for r in manifest["runs"]) for state in ("ok", "skipped", "error")
        }
        print(f"{output / 'report.html'}: {states}")
        for result in manifest["runs"]:
            if result["status"] != "ok":
                print(f"{result['label']}: {result['status']}: {result['reason']}")
        return 1 if states["error"] else 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; started process groups cleaned up", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
