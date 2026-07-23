"""Windows MSI packaging builder for AM Workbench."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from xml.etree import ElementTree  # nosec B405 - tree is constructed locally; no external XML parsed

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from installer.package_preflight import PreflightResult, preflight_staging_dir

PRODUCT_NAME = "AM Workbench"
ARTIFACT_NAME = "AMWorkbench.msi"
WIX_V4_NAMESPACE = "http://wixtoolset.org/schemas/v4/wxs"
WIX_V3_NAMESPACE = "http://schemas.microsoft.com/wix/2006/wi"
UPGRADE_CODE = "8c0c7c8a-f705-4638-bef6-131dfb40b8b5"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def preflight(staging_dir: Path | str) -> PreflightResult:
    return preflight_staging_dir(staging_dir, repo_root_path=_repo_root())


def _iter_payload_files(staging_dir: Path) -> list[Path]:
    return sorted(path for path in staging_dir.rglob("*") if path.is_file())


def _stable_id(prefix: str, path: Path) -> str:
    safe = "".join(character if character.isalnum() else "_" for character in path.as_posix())
    if not safe or safe[0].isdigit():
        safe = f"_{safe}"
    return f"{prefix}_{safe[:64]}"


def _write_wix_manifest(staging_dir: Path, manifest_path: Path, *, version: int) -> PreflightResult:
    payload_files = _iter_payload_files(staging_dir)
    if not payload_files:
        return PreflightResult(False, ("MSI staging_dir must contain at least one payload file",))

    namespace = WIX_V4_NAMESPACE if version == 4 else WIX_V3_NAMESPACE
    ElementTree.register_namespace("", namespace)
    wix = ElementTree.Element(f"{{{namespace}}}Wix")

    if version == 4:
        package = ElementTree.SubElement(
            wix,
            f"{{{namespace}}}Package",
            {
                "Name": PRODUCT_NAME,
                "Manufacturer": "Vetinari contributors",
                "Version": "0.0.0",
                "UpgradeCode": UPGRADE_CODE,
                "Scope": "perMachine",
            },
        )
        package_parent = package
    else:
        product = ElementTree.SubElement(
            wix,
            f"{{{namespace}}}Product",
            {
                "Id": "*",
                "Name": PRODUCT_NAME,
                "Language": "1033",
                "Version": "0.0.0",
                "Manufacturer": "Vetinari contributors",
                "UpgradeCode": UPGRADE_CODE,
            },
        )
        package = ElementTree.SubElement(
            product,
            f"{{{namespace}}}Package",
            {
                "InstallerVersion": "500",
                "Compressed": "yes",
                "InstallScope": "perMachine",
            },
        )
        package_parent = product

    ElementTree.SubElement(
        package_parent,
        f"{{{namespace}}}MajorUpgrade",
        {"DowngradeErrorMessage": "A newer version is installed."},
    )
    ElementTree.SubElement(package_parent, f"{{{namespace}}}MediaTemplate", {"EmbedCab": "yes"})
    feature = ElementTree.SubElement(
        package_parent,
        f"{{{namespace}}}Feature",
        {"Id": "MainFeature", "Title": PRODUCT_NAME, "Level": "1"},
    )

    if version == 4:
        standard_directory = ElementTree.SubElement(
            wix, f"{{{namespace}}}StandardDirectory", {"Id": "ProgramFilesFolder"}
        )
        install_dir = ElementTree.SubElement(
            standard_directory, f"{{{namespace}}}Directory", {"Id": "INSTALLFOLDER", "Name": PRODUCT_NAME}
        )
    else:
        directory = ElementTree.SubElement(
            product, f"{{{namespace}}}Directory", {"Id": "TARGETDIR", "Name": "SourceDir"}
        )
        program_files = ElementTree.SubElement(directory, f"{{{namespace}}}Directory", {"Id": "ProgramFilesFolder"})
        install_dir = ElementTree.SubElement(
            program_files, f"{{{namespace}}}Directory", {"Id": "INSTALLFOLDER", "Name": PRODUCT_NAME}
        )

    directory_by_relative: dict[Path, ElementTree.Element] = {Path("."): install_dir}
    for relative_dir in sorted({
        file.relative_to(staging_dir).parent for file in payload_files if file.parent != staging_dir
    }):
        parent = directory_by_relative[relative_dir.parent if relative_dir.parent != Path("") else Path(".")]
        directory_by_relative[relative_dir] = ElementTree.SubElement(
            parent,
            f"{{{namespace}}}Directory",
            {"Id": _stable_id("dir", relative_dir), "Name": relative_dir.name},
        )

    for payload_file in payload_files:
        relative = payload_file.relative_to(staging_dir)
        component = ElementTree.SubElement(
            directory_by_relative[relative.parent if relative.parent != Path("") else Path(".")],
            f"{{{namespace}}}Component",
            {"Id": _stable_id("cmp", relative), "Guid": "*"},
        )
        file_id = _stable_id("file", relative)
        ElementTree.SubElement(
            component,
            f"{{{namespace}}}File",
            {"Id": file_id, "Source": str(payload_file), "KeyPath": "yes"},
        )
        ElementTree.SubElement(feature, f"{{{namespace}}}ComponentRef", {"Id": component.attrib["Id"]})

    ElementTree.ElementTree(wix).write(manifest_path, encoding="utf-8", xml_declaration=True)
    return PreflightResult(True)


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
    artifact_label = artifact_path.suffix or artifact_path.name
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
            (f"{tool} completed but did not create expected {artifact_label} artifact at {artifact_path}",),
            artifact_path=str(artifact_path),
            tool=tool,
            argv=commands,
        )
    if artifact_path.stat().st_size == 0:
        return PreflightResult(
            False,
            (f"{tool} created an empty {artifact_label} artifact at {artifact_path}",),
            artifact_path=str(artifact_path),
            tool=tool,
            argv=commands,
        )
    return PreflightResult(True, artifact_path=str(artifact_path), tool=tool, argv=commands)


def build(staging_dir: Path | str, output_dir: Path | str) -> PreflightResult:
    result = preflight(staging_dir)
    if not result.passed:
        return result

    staging_path = Path(staging_dir).resolve()
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    artifact_path = output_path / ARTIFACT_NAME

    wix = _tool_path("wix")
    if wix:
        manifest_path = output_path / "am-workbench.wxs"
        manifest_result = _write_wix_manifest(staging_path, manifest_path, version=4)
        if not manifest_result.passed:
            return manifest_result
        commands = ((wix, "build", str(manifest_path), "-o", str(artifact_path)),)
        return _run_native_commands(commands, artifact_path=artifact_path, tool="wix")

    candle = _tool_path("candle")
    light = _tool_path("light")
    if candle and light:
        manifest_path = output_path / "am-workbench.wxs"
        object_path = output_path / "am-workbench.wixobj"
        manifest_result = _write_wix_manifest(staging_path, manifest_path, version=3)
        if not manifest_result.passed:
            return manifest_result
        commands = (
            (candle, str(manifest_path), "-out", str(object_path)),
            (light, str(object_path), "-out", str(artifact_path)),
        )
        return _run_native_commands(commands, artifact_path=artifact_path, tool="candle/light")

    return PreflightResult(
        False,
        (
            "cannot build .msi artifact because native MSI tooling is missing: "
            "install WiX Toolset (`wix`) or WiX v3 `candle` and `light` on PATH",
        ),
        artifact_path=str(artifact_path),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Windows MSI installer with WiX tooling.")
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


__all__ = ["PreflightResult", "build", "preflight"]


if __name__ == "__main__":
    raise SystemExit(main())
