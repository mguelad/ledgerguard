"""Build a deterministic plugin archive for one fixed HTTPS deployment origin."""

import argparse
import hashlib
import json
import re
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import urlparse


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    origin = args.origin.rstrip("/")
    url = urlparse(origin)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
        or url.port not in {None, 443}
        or not re.fullmatch(r"https://[a-z0-9.-]+(?::443)?", origin)
        or url.hostname.endswith(".invalid")
    ):
        raise SystemExit("An exact HTTPS origin without credentials, path or query is required")
    source = Path(__file__).parents[1] / "plugins/ledgerguard-woocommerce"
    args.output.mkdir(parents=True, exist_ok=True)
    version = tomllib.loads((source.parents[1] / "pyproject.toml").read_text())["project"]["version"]
    if f"Version: {version}" not in (source / "ledgerguard-woocommerce.php").read_text():
        raise SystemExit("Application and plugin versions differ")
    archive = args.output / f"ledgerguard-woocommerce-{version}.zip"
    files = {}
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if not path.is_file() or not (
                str(relative) in {"ledgerguard-woocommerce.php", "uninstall.php", "readme.txt", "LICENSE"}
                or (relative.parts[0] == "includes" and path.suffix == ".php")
            ):
                continue
            data = path.read_bytes()
            if path.name == "class-ledgerguard-client.php":
                data = data.replace(b"https://api.ledgerguard.invalid", origin.encode())
                if b".invalid" in data:
                    raise SystemExit("Build destination could not be compiled")
            name = "ledgerguard-woocommerce/" + str(path.relative_to(source))
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
            files[name] = hashlib.sha256(data).hexdigest()
    archive.with_suffix(".sha256").write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n"
    )
    archive.with_suffix(".manifest.json").write_text(
        json.dumps({"version": version, "origin": origin, "files": files}, indent=2) + "\n"
    )
    archive.with_suffix(".cdx.json").write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "metadata": {
                    "component": {
                        "type": "application",
                        "bom-ref": "ledgerguard-woocommerce",
                        "name": "ledgerguard-woocommerce",
                        "version": version,
                        "licenses": [{"license": {"id": "GPL-2.0-or-later"}}],
                        "hashes": [{"alg": "SHA-256", "content": hashlib.sha256(archive.read_bytes()).hexdigest()}],
                    }
                },
                "components": [],
                "dependencies": [{"ref": "ledgerguard-woocommerce", "dependsOn": []}],
            },
            indent=2,
        )
        + "\n"
    )
    print(archive)


if __name__ == "__main__":
    main()
