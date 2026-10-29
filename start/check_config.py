"""Validate .env against config.Settings without printing secret values.

Usage (from anywhere):
    python start/check_config.py

Exit 0 when config imports successfully. Otherwise prints the offending field
names and pydantic messages (never the values) and exits 1.
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    # config.py loads .env relative to the current directory; make it the
    # project root so the check works no matter where this script is called.
    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, str(PROJECT_ROOT))

    try:
        import config  # noqa: F401
    except Exception as exc:  # pydantic ValidationError or a load error
        errors = getattr(exc, "errors", None)
        if callable(errors):
            print("Invalid or missing .env values:")
            for err in errors(include_url=False):
                loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
                print(f"  - {loc}: {err.get('msg') or err.get('type')}")
        else:
            print(f"Cannot load config ({type(exc).__name__}): {exc}")
        return 1

    print("Configuration OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
