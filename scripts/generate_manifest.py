#!/usr/bin/env python3
"""Generate or verify MANIFEST.sha256 for all tracked release files."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "MANIFEST.sha256"


def tracked_files() -> list[Path]:
    raw = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"])
    paths: list[Path] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        rel = os.fsdecode(item)
        if rel == "MANIFEST.sha256":
            continue
        if "\n" in rel or "\r" in rel:
            raise RuntimeError(f"unsupported tracked filename in manifest: {rel!r}")
        paths.append(Path(rel))
    return sorted(paths, key=lambda path: path.as_posix())


def render_manifest() -> str:
    lines: list[str] = []
    for rel in tracked_files():
        digest = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        lines.append(f"{digest}  ./{rel.as_posix()}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    rendered = render_manifest()

    if args.output:
        args.output.write_text(rendered, encoding="utf-8")

    if args.check:
        current = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
        if current != rendered:
            print(
                "MANIFEST.sha256 is stale; run "
                "'python scripts/generate_manifest.py --output MANIFEST.sha256'.",
                file=sys.stderr,
            )
            print("SUNAQ_GENERATED_MANIFEST_BEGIN")
            sys.stdout.write(rendered)
            print("SUNAQ_GENERATED_MANIFEST_END")
            return 1

    if not args.output and not args.check:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
