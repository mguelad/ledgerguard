"""Build (or check) the provider manifest. Does not upload or authorize a Stripe App."""

import argparse
import json
import tomllib
from pathlib import Path

from packages.acceptance.stripe_app import manifest, validate_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--output", type=Path, default=Path("dist/stripe-app.json"))
    parser.add_argument("--check", type=Path, help="Validate an existing manifest instead of building")
    args = parser.parse_args()
    version = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["version"]
    try:
        expected = manifest(args.app_id, args.origin, version)
        if args.check:
            validate_manifest(json.loads(args.check.read_text()), args.app_id, args.origin, version)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(expected, indent=2) + "\n")
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Stripe manifest rejected: {type(exc).__name__}") from None


if __name__ == "__main__":
    main()
