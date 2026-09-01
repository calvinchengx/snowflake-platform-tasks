#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sys


def main() -> int:
    missing = [n for n in ("docker", "uv", "make") if shutil.which(n) is None]
    if missing:
        print("missing:", ", ".join(missing), file=sys.stderr)
        return 1
    print("doctor: docker, uv, make are on PATH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
