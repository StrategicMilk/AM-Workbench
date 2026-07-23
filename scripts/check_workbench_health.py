#!/usr/bin/env python3
"""Probe the local Workbench health endpoints."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
from dataclasses import asdict, dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class HealthCheck:
    check: str
    status: str
    detail: str


def _server_url() -> str:
    return os.environ.get("VETINARI_WORKBENCH_URL", "http://127.0.0.1:8080").rstrip("/")


def _fetch(url: str, timeout: float) -> tuple[int, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OSError("URL scheme must be http or https")
    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_cls(parsed.hostname, parsed.port, timeout=timeout)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", errors="replace")
    finally:
        connection.close()


def probe(url: str, timeout: float) -> tuple[int, list[HealthCheck]]:
    checks: list[HealthCheck] = []
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return 2, [HealthCheck("url", "error", "URL scheme must be http or https")]
    try:
        ready_status, ready_body = _fetch(f"{url}/ready", timeout)
    except OSError as exc:
        return 2, [HealthCheck("ready", "error", str(exc))]

    checks.append(
        HealthCheck(
            "ready",
            "pass" if 200 <= ready_status < 300 else "fail",
            f"HTTP {ready_status}: {ready_body[:160]}",
        )
    )

    try:
        health_status, health_body = _fetch(f"{url}/health", timeout)
    except OSError as exc:
        return 2, [*checks, HealthCheck("health", "error", str(exc))]

    checks.append(
        HealthCheck(
            "health",
            "pass" if 200 <= health_status < 300 and '"status"' in health_body else "fail",
            f"HTTP {health_status}: {health_body[:160]}",
        )
    )
    return (0 if all(check.status == "pass" for check in checks) else 1), checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check local Vetinari Workbench health.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable check results.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Probe timeout in seconds.")
    args = parser.parse_args(argv)

    exit_code, checks = probe(_server_url(), args.timeout)
    if args.json:
        print(json.dumps([asdict(check) for check in checks], indent=2))
    else:
        for check in checks:
            print(f"{check.check}: {check.status} - {check.detail}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
