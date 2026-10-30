"""Initramfs archives in the kernel's "newc" cpio format.

Written here rather than with cpio(1) because a rootless container cannot create the device nodes
an initramfs needs (/dev/console); an archive entry is just a header, so no mknod is involved.
The kernel unpacks concatenated archives in order, which lets a small per-run archive add files
to a large, reusable one.
"""

from pathlib import Path
import stat

MAGIC = b"070701"
TRAILER = "TRAILER!!!"


def _padding(length):
    return b"\0" * (-length % 4)


class Archive:
    """Writes entries to a binary stream; `close` adds the trailer."""

    def __init__(self, stream):
        self.stream = stream
        self.inode = 0

    def _entry(self, name, mode, data=b"", mtime=0, rdev=(0, 0)):
        self.inode += 1
        encoded = name.encode() + b"\0"
        fields = (
            self.inode,
            mode,
            0,  # uid: root
            0,  # gid: root
            2 if stat.S_ISDIR(mode) else 1,
            int(mtime),
            len(data),
            0,  # device major of the containing filesystem
            0,  # device minor
            rdev[0],
            rdev[1],
            len(encoded),
            0,  # checksum, unused by "newc"
        )
        header = MAGIC + b"".join(f"{value:08x}".encode() for value in fields)
        self.stream.write(header + encoded + _padding(len(header) + len(encoded)))
        self.stream.write(data + _padding(len(data)))

    def directory(self, name, mode=0o755, mtime=0):
        self._entry(name, stat.S_IFDIR | mode, mtime=mtime)

    def file(self, name, data, mode=0o644, mtime=0):
        self._entry(name, stat.S_IFREG | mode, data, mtime)

    def symlink(self, name, target, mtime=0):
        self._entry(name, stat.S_IFLNK | 0o777, target.encode(), mtime)

    def character_device(self, name, major, minor, mode=0o600):
        self._entry(name, stat.S_IFCHR | mode, rdev=(major, minor))

    def tree(self, root, prefix=""):
        """Every directory, regular file and symlink below `root`, as `prefix/relative/path`."""
        root = Path(root)
        for path in sorted(root.rglob("*")):
            name = str(Path(prefix) / path.relative_to(root)).lstrip("/")
            info = path.lstat()
            permissions = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                self.symlink(name, str(path.readlink()), info.st_mtime)
            elif stat.S_ISDIR(info.st_mode):
                self.directory(name, permissions, info.st_mtime)
            elif stat.S_ISREG(info.st_mode):
                self.file(name, path.read_bytes(), permissions, info.st_mtime)

    def close(self):
        self._entry(TRAILER, 0)


def entries(data):
    """(name, mode, content) of every entry in one archive, for inspection and tests."""
    found = []
    offset = 0
    while True:
        if data[offset : offset + 6] != MAGIC:
            raise ValueError(f"no newc header at offset {offset}")
        fields = [int(data[offset + 6 + 8 * index : offset + 14 + 8 * index], 16) for index in range(13)]
        mode, size, name_size = fields[1], fields[6], fields[11]
        name_start = offset + 110
        name = data[name_start : name_start + name_size - 1].decode()
        content_start = name_start + name_size + (-(110 + name_size) % 4)
        content = data[content_start : content_start + size]
        offset = content_start + size + (-size % 4)
        if name == TRAILER:
            return found
        found.append((name, mode, content))
