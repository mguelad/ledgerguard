"""Create a private local environment without replacing an existing configuration."""

import base64
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
value = (
    (root / ".env.example")
    .read_text()
    .replace("replace-with-a-random-secret", secrets.token_urlsafe(64))
    .replace("replace-with-base64-32-byte-key", base64.b64encode(secrets.token_bytes(32)).decode())
)
try:
    fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit("An environment already exists; it was preserved") from None
with os.fdopen(fd, "w") as stream:
    stream.write(value)
print("Local environment configured. Start with docker compose up --build -d.")
