"""Set or clear a value in .env without ever printing the value.

Usage:
    python start/set_env.py KEY --random   # generate a random hex value
    python start/set_env.py KEY --empty    # set an empty value

Comments in .env are preserved; active `KEY=...` lines are replaced. Works
regardless of the current directory (paths are resolved from this file).
"""
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: set_env.py KEY --random|--empty", file=sys.stderr)
        return 2

    key, mode = sys.argv[1], sys.argv[2]
    if mode == "--random":
        value = secrets.token_hex(32)
    elif mode == "--empty":
        value = ""
    else:
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [line for line in lines if not line.lstrip().startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
