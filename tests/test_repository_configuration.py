"""Validate Compose mount semantics before starting the integration stack."""

from pathlib import Path

import yaml


def test_application_tmpfs_is_one_absolute_mount_with_non_root_ownership():
    compose = yaml.safe_load((Path(__file__).parents[1] / "compose.yaml").read_text())
    for name, service in compose["services"].items():
        if name == "db":
            continue
        mounts = service["tmpfs"]
        assert len(mounts) == 1, f"{name}: YAML must not split comma-separated mount options"
        path, options = mounts[0].split(":", maxsplit=1)
        assert path == "/tmp"  # noqa: S108 - isolated in-memory container mount
        assert set(options.split(",")) == {"uid=10001", "gid=10001", "mode=1777"}
        assert service["read_only"] is True
