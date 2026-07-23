"""Stop the AM Workbench launcher gracefully."""

from __future__ import annotations

import json
import sys

from vetinari.runtime.app_lifecycle import get_app_lifecycle


def main() -> int:
    report = get_app_lifecycle().shutdown()
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
