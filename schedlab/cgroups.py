"""Create only owned leaf cgroups below an explicitly delegated v2 subtree."""

from contextlib import contextmanager
import errno
from pathlib import Path
import tempfile


class Unavailable(RuntimeError):
    pass


@contextmanager
def cpu_groups(parent, controls):
    root = Path(parent).resolve()
    owned = []
    try:
        if not (root / "cgroup.controllers").is_file():
            raise Unavailable("parent is not a visible cgroup v2 directory")
        if "cpu" not in (root / "cgroup.subtree_control").read_text().split():
            raise Unavailable("cpu must already be enabled in the delegated parent's cgroup.subtree_control")
        if (root / "cgroup.type").read_text().strip() != "domain":
            raise Unavailable("this lab requires a delegated domain cgroup")
        for control in controls:
            child = Path(tempfile.mkdtemp(prefix="schedlab-", dir=root))
            owned.append(child)
            for name, value in control.items():
                if name not in ("cpu.weight", "cpu.max"):
                    raise ValueError("unsupported cgroup control: %s" % name)
                (child / name).write_text(str(value))
        yield owned
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT, errno.EOPNOTSUPP):
            raise Unavailable("cgroup delegation unavailable: %s" % exc) from exc
        raise
    finally:
        for child in reversed(owned):
            # rmdir fails rather than deleting another process's group or any subtree.
            child.rmdir()
