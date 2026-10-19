"""Demo helper: pulls the newest Health Connect exports straight off a
USB-connected phone, so nothing has to be emailed to the laptop.

Uses `adb exec-out run-as <package>`, which can read the app's private files
directory on debuggable builds (anything installed from Android Studio).

Usage:
    python -m android.pull --out exports/
    python -m android.pull --out exports/ --subject mrn-00123 --map   # pull, then map each file
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_PACKAGE = "com.example.healthconnectbridge"
_KINDS = ("heart_rate", "spo2", "hrv")


def pull_latest(package: str, dest: Path) -> list[Path]:
    if shutil.which("adb") is None:
        raise SystemExit("error: adb not found on PATH - install Android platform-tools")

    listing = subprocess.run(
        ["adb", "exec-out", "run-as", package, "ls", "-t", "files"],
        capture_output=True, text=True,
    )
    if listing.returncode != 0:
        raise SystemExit(
            f"error: couldn't list {package}'s files ({listing.stderr.strip() or 'no device?'}). "
            "Check the phone is connected with USB debugging on and the app is a debug build."
        )
    names = listing.stdout.split()  # ls -t: newest first

    dest.mkdir(parents=True, exist_ok=True)
    pulled = []
    for kind in _KINDS:
        newest = next((n for n in names if n.startswith(kind + "_") and n.endswith(".json")), None)
        if newest is None:
            print(f"no {kind} export on the phone yet - tap Read Samples for it first", file=sys.stderr)
            continue
        data = subprocess.run(
            ["adb", "exec-out", "run-as", package, "cat", f"files/{newest}"],
            capture_output=True, check=True,
        ).stdout
        target = dest / newest
        target.write_bytes(data)
        pulled.append(target)
        print(f"pulled {newest}", file=sys.stderr)
    return pulled


def main() -> None:
    parser = argparse.ArgumentParser(description="Pull the newest Health Connect exports from a connected phone.")
    parser.add_argument("--out", type=Path, default=Path("exports"), help="Folder to save exports into.")
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help=f"Bridge app package (default: {DEFAULT_PACKAGE}).")
    parser.add_argument("--map", action="store_true", help="Also map each pulled file with android.cli.")
    parser.add_argument("--subject", help="Patient ID / synthetic MRN (required with --map).")
    args = parser.parse_args()

    if args.map and not args.subject:
        parser.error("--subject is required with --map")

    pulled = pull_latest(args.package, args.out)
    if not args.map:
        sys.exit(0 if pulled else 1)

    from android.cli import run

    status = 0
    for path in pulled:
        out = path.with_name(path.stem + ".canonical.json")
        status |= run([str(path), "--subject", args.subject, "--out", str(out)])
        print(f"mapped {path.name} -> {out.name}", file=sys.stderr)
    sys.exit(status)


if __name__ == "__main__":
    main()
