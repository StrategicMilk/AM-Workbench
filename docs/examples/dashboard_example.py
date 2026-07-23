"""Example dashboard payload with dynamic, timezone-aware timestamps.

This file is documentation support: it can be imported directly and copied into
guide snippets without freezing old trace dates into examples.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def build_dashboard_example(now: datetime | None = None) -> dict[str, object]:
    """Return a small dashboard payload relative to ``now``."""
    current = now or datetime.now(timezone.utc)
    previous = current - timedelta(minutes=17)

    return {
        "generated_at": current.isoformat(),
        "run": {
            "id": "run-example",
            "status": "passed",
            "started_at": previous.isoformat(),
            "completed_at": current.isoformat(),
        },
        "signals": [
            {"name": "verification", "state": "passed", "observed_at": current.isoformat()},
            {"name": "evidence", "state": "fresh", "observed_at": previous.isoformat()},
        ],
    }


if __name__ == "__main__":
    import json

    print(json.dumps(build_dashboard_example(), indent=2))
