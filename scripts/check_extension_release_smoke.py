"""Smoke-test extension marketplace admission and rollback evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for import_path in (ROOT, ROOT / "python"):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from vetinari.workbench.mcp_marketplace.release import ExtensionReleaseService


def _manifest(**overrides):
    payload = {
        "extension_id": "amw.local.ide",
        "schema_version": "ide-extension.v1",
        "signature": "signed:local-fixture",
        "permissions": ["tool:submit_goal", "resource:workspace"],
        "retention_days": 7,
        "privacy": {"redaction": "secret-bearing-fields", "deletion": "delete within retention"},
        "receipt_path": "receipts/ide-extension.jsonl",
    }
    payload.update(overrides)
    return payload


def run_checks() -> list[dict[str, object]]:
    service = ExtensionReleaseService()
    good = service.admit({"manifest": _manifest(), "default_on": True})
    unsigned = service.admit({"manifest": _manifest(signature="unsigned"), "default_on": True})
    undeclared = service.admit({"manifest": _manifest(permissions=["fs_read:../secret"]), "default_on": True})
    not_default = service.admit({"manifest": _manifest(), "default_on": False})
    return [
        {
            "id": "default-on-signed-manifest-admitted",
            "passed": good.admitted and good.receipts[0]["write_path"] == "receipts/ide-extension.jsonl",
        },
        {
            "id": "unsigned-package-rolls-back",
            "passed": (not unsigned.admitted)
            and unsigned.code == "EXT_SIGNATURE"
            and unsigned.receipts[-1]["action"] == "rollback",
        },
        {
            "id": "undeclared-permission-rolls-back",
            "passed": (not undeclared.admitted) and undeclared.code == "EXT_PERMISSION",
        },
        {
            "id": "default-on-product-registration-required",
            "passed": (not not_default.admitted) and not_default.code == "EXT_DEFAULT_ON",
        },
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = run_checks()
    passed = all(row["passed"] for row in rows)
    if args.json:
        print(json.dumps({"passed": passed, "checks": rows}, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(f"{row['id']}: {'PASS' if row['passed'] else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
