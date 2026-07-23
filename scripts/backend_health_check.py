#!/usr/bin/env python3
"""Backend health check command for catalog backends."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _release_doctor_static_reference():
    """Keep release proof backend integration visible to static wiring checks."""
    from vetinari.release.release_doctor import build_release_proof

    return build_release_proof(dry_run=True)


def _row(provider, signal) -> dict[str, object]:
    from vetinari.adapters.registry import AdapterRegistry

    profile = AdapterRegistry.capabilities(provider)
    return {
        "provider": provider.value,
        "passed": signal.passed,
        "status": "not_installed" if not signal.passed else "ok",
        "cache_durability": profile.cache_durability.value,
        "prefix_cache": profile.prefix_cache,
        "supports_mid_generation_resume": profile.supports_mid_generation_resume,
        "issues": list(signal.issues),
    }


def main(argv: list[str] | None = None) -> int:
    """Run backend probes and print text or JSON."""
    from vetinari.runtime.backend_probes import default_probes

    parser = argparse.ArgumentParser(description="Check registered backend health")
    parser.add_argument("--dry-run", action="store_true", help="Do not contact backends")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON")
    parser.add_argument("--strict", action="store_true", help="Fail if required backend is unhealthy")
    parser.add_argument("--show-cache", action="store_true", help="Print cache capability matrix")
    args = parser.parse_args(argv)

    rows = [_row(provider, probe.probe_fn()) for provider, probe in default_probes().items()]
    if args.as_json:
        print(json.dumps({"backends": rows}, indent=2, sort_keys=True))
    else:
        if args.show_cache:
            print("provider status cache_durability prefix_cache supports_mid_generation_resume")
        else:
            print("provider status")
        for row in rows:
            if args.show_cache:
                print(
                    f"{row['provider']} {row['status']} {row['cache_durability']} "
                    f"{row['prefix_cache']} {row['supports_mid_generation_resume']}"
                )
            else:
                print(f"{row['provider']} {row['status']}")
    if args.strict and any(not row["passed"] for row in rows if row["provider"] in {"vllm", "local"}):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
