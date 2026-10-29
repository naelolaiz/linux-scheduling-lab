"""Join one leaf before exec; descendants then inherit it (no parent mutation)."""

import errno
import json
import os
from pathlib import Path
import sys


def main():
    if len(sys.argv) < 3:
        print("usage: python3 -m schedlab.exec CGROUP EXECUTABLE [ARGS...]", file=sys.stderr)
        return 2
    try:
        (Path(sys.argv[1]) / "cgroup.procs").write_text(str(os.getpid()))
    except OSError as exc:
        skipped = exc.errno in (errno.EPERM, errno.EACCES, errno.EROFS, errno.ENOENT)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "skipped" if skipped else "error",
                    "reason": "join delegated cgroup: %s" % exc,
                }
            )
        )
        return 77 if skipped else 1
    os.execv(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
