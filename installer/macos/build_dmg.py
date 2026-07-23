"""macOS DMG packaging builder for AM Workbench."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from installer.package_preflight import PreflightResult, preflight_staging_dir

ARTIFACT_NAME = "AMWorkbench.dmg"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _tool_path(name: str) -> str | None:
    return shutil.which(name)


async def _run_native_process(command: tuple[str, ...]) -> int:
    process = await asyncio.create_subprocess_exec(*command)
    return await process.wait()


def _spawn_native_process(command: tuple[str, ...]) -> int:
    return asyncio.run(_run_native_process(command))


def _run_native_commands(
    commands: tuple[tuple[str, ...], ...],
    *,
    artifact_path: Path,
    tool: str,
) -> PreflightResult:
    for command in commands:
        try:
            return_code = _spawn_native_process(command)
        except OSError as exc:
            return PreflightResult(
                False,
                (f"{tool} could not be launched with explicit argv {command!r}: {exc}",),
                artifact_path=str(artifact_path),
                tool=tool,
                argv=commands,
            )
        if return_code != 0:
            return PreflightResult(
                False,
                (f"{tool} failed with exit code {return_code} while building {artifact_path}",),
                artifact_path=str(artifact_path),
                tool=tool,
                argv=commands,
            )

    if not artifact_path.is_file():
        return PreflightResult(
            False,
            (f"{tool} completed but did not create expected artifact at {artifact_path}",),
            artifact_path=str(artifact_path),
            tool=tool,
            argv=commands,
        )
    if artifact_path.stat().st_size == 0:
        return PreflightResult(
            False,
            (f"{tool} created an empty artifact at {artifact_path}",),
            artifact_path=str(artifact_path),
            tool=tool,
            argv=commands,
        )
    return PreflightResult(True, artifact_path=str(artifact_path), tool=tool, argv=commands)


def preflight(staging_dir: Path | str) -> PreflightResult:
    return preflight_staging_dir(staging_dir, repo_root_path=_repo_root())


def build(staging_dir: Path | str, output_dir: Path | str) -> PreflightResult:
    result = preflight(staging_dir)
    if not result.passed:
        return result

    staging_path = Path(staging_dir).resolve()
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    artifact_path = output_path / ARTIFACT_NAME

    hdiutil = _tool_path("hdiutil")
    if hdiutil:
        commands = (
            (
                hdiutil,
                "create",
                "-volname",
                "AM Workbench",
                "-srcfolder",
                str(staging_path),
                "-ov",
                "-format",
                "UDZO",
                str(artifact_path),
            ),
        )
        return _run_native_commands(commands, artifact_path=artifact_path, tool="hdiutil")

    return PreflightResult(
        False,
        (
            "cannot build .dmg artifact because native DMG tooling is missing: "
            "install or run on macOS with hdiutil on PATH",
        ),
        artifact_path=str(artifact_path),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the macOS DMG with hdiutil.")
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = preflight(args.staging_dir) if args.preflight_only else build(args.staging_dir, args.output_dir)
    if args.json:
        sys.stdout.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
    elif result.blockers:
        for blocker in result.blockers:
            sys.stderr.write(blocker + "\n")
    return 0 if result.passed else 1


__all__ = ["build", "main", "preflight"]


if __name__ == "__main__":
    raise SystemExit(main())
