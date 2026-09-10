import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import assemble_runtime


def test_server_runtime_preserves_needed_files_and_package_inventory(tmp_path, monkeypatch):
    prefix, output = tmp_path / "prefix", tmp_path / "runtime"
    for name in [
        "bin/python3.13",
        "lib/libpython3.13.so.1.0",
        "lib/python3.13/os.py",
        "lib/python3.13/lib-dynload/_ssl.cpython.so",
        "lib/python3.13/lib-dynload/_sqlite3.cpython.so",
        "lib/python3.13/site-packages/pip/__init__.py",
    ]:
        path = prefix / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")
    library = tmp_path / "native/libffi.so.8"
    library.parent.mkdir()
    library.write_text("synthetic library")
    copyright = tmp_path / "native/copyright"
    copyright.write_text("synthetic license")

    def run(args, **kwargs):
        assert args[0] == "/usr/bin/dpkg-query" and args[2] in assemble_runtime.NATIVE_PACKAGES
        return SimpleNamespace(
            stdout=f"Package: {args[2]}\nVersion: 1.0\n" if args[1] == "--status" else f"{library}\n{copyright}\n"
        )

    monkeypatch.setattr(assemble_runtime.subprocess, "run", run)
    assemble_runtime.assemble(prefix, output)
    assert (output / "usr/local/bin/python").is_symlink()
    assert (output / "usr/local/bin/python").read_text() == "synthetic"
    assert (output / "usr/local/lib/python3.13/lib-dynload/_ssl.cpython.so").exists()
    assert not (output / "usr/local/lib/python3.13/lib-dynload/_sqlite3.cpython.so").exists()
    assert not (output / "usr/local/lib/python3.13/site-packages").exists()
    assert (output / library.relative_to("/")).read_text() == "synthetic library"
    assert (output / copyright.relative_to("/")).read_text() == "synthetic license"
    for package in assemble_runtime.NATIVE_PACKAGES:
        assert "Version: 1.0" in (output / "var/lib/dpkg/status.d" / package).read_text()


def test_runtime_assembly_refuses_existing_output(tmp_path):
    with pytest.raises(FileExistsError):
        assemble_runtime.assemble(Path("/unused"), tmp_path)


def test_runtime_assembly_refuses_missing_package_metadata(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/python3.13").write_text("fixture")
    (prefix / "lib/python3.13").mkdir(parents=True)

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["dpkg-query"])

    monkeypatch.setattr(assemble_runtime.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        assemble_runtime.assemble(prefix, tmp_path / "runtime")
