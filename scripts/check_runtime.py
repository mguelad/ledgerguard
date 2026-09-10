"""Smoke-test the intentionally minimal server runtime inside the final image."""

import bz2
import ctypes
import importlib.util
import io
import lzma
import os
import ssl
import sys
from pathlib import Path

import boto3
import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PIL import Image
from reportlab.pdfgen.canvas import Canvas


def main() -> None:
    assert sys.version_info[:2] == (3, 13)
    assert os.getuid() == 10001
    assert not Path("/bin/sh").exists() and not Path("/usr/bin/apt").exists()
    assert importlib.util.find_spec("_sqlite3") is None
    assert importlib.util.find_spec("pip") is None
    assert ssl.create_default_context().cert_store_stats()["x509_ca"] > 0
    assert bz2.decompress(bz2.compress(b"test")) == lzma.decompress(lzma.compress(b"test")) == b"test"
    assert ctypes.sizeof(ctypes.c_void_p) == 8
    assert psycopg.pq.version() >= 170000
    assert boto3.Session(region_name="eu-west-1").region_name == "eu-west-1"
    key = Ed25519PrivateKey.generate()
    key.public_key().verify(key.sign(b"test"), b"test")
    png, pdf = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (2, 2)).save(png, format="PNG")
    canvas = Canvas(pdf)
    canvas.drawString(20, 20, "LedgerGuard runtime verification")
    canvas.save()
    assert png.getvalue().startswith(b"\x89PNG") and pdf.getvalue().startswith(b"%PDF")
    for package in ("libbz2-1.0", "libffi8", "liblzma5", "libgcc-s1"):
        assert (Path("/var/lib/dpkg/status.d") / package).is_file()
    print("Server runtime verified: PostgreSQL, TLS trust, crypto, compression, AWS SDK, PNG and PDF")


if __name__ == "__main__":
    main()
