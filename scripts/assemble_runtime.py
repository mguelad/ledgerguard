"""Assemble the server-only CPython prefix for the distroless runtime during Docker build.

No package database is removed. Added Debian libraries retain their original
package metadata so image scanners can identify the actual shipped versions.
"""

import argparse
import shutil
import subprocess
from pathlib import Path

NATIVE_PACKAGES = ("libbz2-1.0", "libffi8", "liblzma5", "libgcc-s1")
OMIT = (
    "site-packages",
    "ensurepip",
    "idlelib",
    "tkinter",
    "turtledemo",
    "test",
    "__pycache__",
    "_curses.*",
    "_curses_panel.*",
    "readline.*",
    "_sqlite3.*",
    "_dbm.*",
    "_gdbm.*",
    "_tkinter.*",
    "_uuid.*",
    "_test*",
    "_ctypes_test.*",
    "_xxtest*",
    "xxlimited*",
    "xxsubtype*",
)


def assemble(prefix: Path, output: Path) -> None:
    output.mkdir()  # Refuse to overwrite any existing directory.
    destination = output / "usr/local"
    (destination / "bin").mkdir(parents=True)
    shutil.copy2(prefix / "bin/python3.13", destination / "bin/python3.13")
    for alias in ("python", "python3"):
        (destination / "bin" / alias).symlink_to("python3.13")
    (destination / "lib").mkdir()
    for path in (prefix / "lib").glob("libpython3.13.so*"):
        shutil.copy2(path, destination / "lib" / path.name)
    shutil.copytree(prefix / "lib/python3.13", destination / "lib/python3.13", ignore=shutil.ignore_patterns(*OMIT))
    metadata = output / "var/lib/dpkg/status.d"
    metadata.mkdir(parents=True)
    for package in NATIVE_PACKAGES:
        # Both command and package arguments are fixed above, never caller input.
        status = subprocess.run(  # noqa: S603 - fixed command and reviewed constant package name
            ["/usr/bin/dpkg-query", "--status", package], check=True, capture_output=True, text=True
        ).stdout
        (metadata / package).write_text(status)
        files = subprocess.run(  # noqa: S603 - fixed command and reviewed constant package name
            ["/usr/bin/dpkg-query", "--listfiles", package], check=True, capture_output=True, text=True
        ).stdout
        for name in files.splitlines():
            source = Path(name)
            if source.is_file() and (".so" in source.name or source.name == "copyright"):
                target = output / source.relative_to("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path("/usr/local"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assemble(args.prefix, args.output)


if __name__ == "__main__":
    main()
