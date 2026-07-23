"""Run the AM Workbench launcher doctor."""

from __future__ import annotations

import argparse
import json
import sys

from vetinari.desktop.doctor import LauncherDoctor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run launcher doctor checks.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args(argv)
    report = LauncherDoctor().run()
    if args.json:
        print(json.dumps(report.to_dict(), sort_keys=True))
    else:
        print(f"launcher doctor: {'passed' if report.overall_passed else 'blocked'}")
    return 0 if report.overall_passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
