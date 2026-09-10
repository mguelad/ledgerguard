"""Inspect AWS without writes or secret retrieval. Requires an explicit initialized target."""

import argparse
import json
from pathlib import Path

import boto3

from packages.acceptance.aws import inspect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--environment", required=True, choices=["dev", "staging", "production"])
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, default=Path("var/acceptance/aws-preflight.json"))
    args = parser.parse_args()
    try:
        report = inspect(
            boto3.Session(), json.loads(args.config.read_text()), args.account_id, args.environment, args.image
        )
    except (ValueError, OSError):
        raise SystemExit("Invalid acceptance configuration; no readiness result was produced") from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    for check in report["checks"]:
        print(f"{check['status']}: {check['name']}")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
