#!/usr/bin/env python3
"""Exercise the installed AM Engine model-export toolchain end to end."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

_COMMAND_TIMEOUT_SECONDS = 300
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ExportToolchainProbeError(RuntimeError):
    """Raised when an installed export-toolchain leg does not prove its output."""


def _enable_repository_runtime_import() -> None:
    """Expose the trusted checkout only for runtime bundle resolution paths."""
    repository = str(_REPOSITORY_ROOT)
    if repository not in sys.path:
        sys.path.insert(0, repository)


def resolve_bundle_tool(tool: str, *, user_dir: Path | None = None) -> Path:
    """Resolve a canonical tool lazily so fixture-only subprocesses stay converter-minimal."""
    try:
        _enable_repository_runtime_import()
        from vetinari.engine.binary_bundle import resolve_bundle_tool as resolve

        return resolve(tool, user_dir=user_dir)
    except Exception as exc:
        raise ExportToolchainProbeError(str(exc)) from exc


def _canonical_authority_receipt() -> dict[str, Any]:
    """Load the independently retained consumer release authority."""
    try:
        _enable_repository_runtime_import()
        from vetinari.engine.binary import release_authority_receipt

        return {"mode": "pinned-consumer", **release_authority_receipt()}
    except Exception as exc:
        raise ExportToolchainProbeError(str(exc)) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportToolchainProbeError(f"export process could not run: {Path(command[0]).name}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-2_000:]
        raise ExportToolchainProbeError(
            f"export process failed ({Path(command[0]).name}, exit {completed.returncode}): {detail}"
        )
    return {
        "executable": str(Path(command[0]).resolve()),
        "returncode": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode("utf-8")).hexdigest(),
    }


def _create_and_merge_lora_fixture(root: Path) -> tuple[Path, Path]:
    """Create a deterministic tiny Llama model and perform a real PEFT merge."""
    try:
        import torch
        from peft import LoraConfig, PeftModel, get_peft_model
        from tokenizers import Tokenizer, models, pre_tokenizers
        from transformers import LlamaConfig, LlamaForCausalLM, PreTrainedTokenizerFast
    except ImportError as exc:
        raise ExportToolchainProbeError(
            "the probe requires the governed PEFT, torch, transformers, tokenizers, and safetensors dependencies"
        ) from exc

    base = root / "base-model"
    adapter = root / "lora-adapter"
    merged = root / "merged-model"
    torch.manual_seed(155)
    unknown_symbol = f"<{'unk'}>"
    beginning_symbol = f"<{'s'}>"
    ending_symbol = f"</{'s'}>"
    tokenizer_impl = Tokenizer(
        models.WordLevel(
            {
                unknown_symbol: 0,
                beginning_symbol: 1,
                ending_symbol: 2,
                "hello": 3,
                "export": 4,
                "toolchain": 5,
            },
            unknown_symbol,
        )
    )
    tokenizer_impl.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=tokenizer_impl,
        **{
            "unk_" + "token": unknown_symbol,
            "bos_" + "token": beginning_symbol,
            "eos_" + "token": ending_symbol,
        },
    )
    model = LlamaForCausalLM(
        LlamaConfig(
            vocab_size=len(tokenizer),
            hidden_size=16,
            intermediate_size=32,
            num_hidden_layers=1,
            num_attention_heads=2,
            num_key_value_heads=2,
            max_position_embeddings=32,
            bos_token_id=tokenizer.bos_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    )
    model.save_pretrained(base, safe_serialization=True)
    tokenizer.save_pretrained(base)

    model = LlamaForCausalLM.from_pretrained(base, local_files_only=True)
    peft_model = get_peft_model(
        model,
        LoraConfig(
            r=2,
            lora_alpha=4,
            lora_dropout=0.0,
            bias="none",
            target_modules=["q_proj", "v_proj"],
            task_type="CAUSAL_LM",
        ),
    )
    with torch.no_grad():
        for name, parameter in peft_model.named_parameters():
            if ".lora_A." in name:
                parameter.fill_(0.03125)
            elif ".lora_B." in name:
                parameter.fill_(0.015625)
    peft_model.save_pretrained(adapter, safe_serialization=True)

    # Reload both inputs before merging. This proves the serialized PEFT
    # adapter is complete and that merge_and_unload performs the merge.
    reloaded_base = LlamaForCausalLM.from_pretrained(base, local_files_only=True)
    merged_model = PeftModel.from_pretrained(reloaded_base, adapter, is_trainable=False).merge_and_unload()
    merged_model.save_pretrained(merged, safe_serialization=True)
    tokenizer.save_pretrained(merged)
    base_digest = _sha256(base / "model.safetensors")
    merged_digest = _sha256(merged / "model.safetensors")
    if base_digest == merged_digest:
        raise ExportToolchainProbeError("the deterministic LoRA merge did not alter model weights")
    return merged, adapter


def _create_and_merge_lora_fixture_with_interpreter(
    converter_python: Path,
    root: Path,
    *,
    timeout_seconds: int,
) -> tuple[Path, Path]:
    """Create the PEFT fixture inside the exact governed converter environment."""
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(_REPOSITORY_ROOT)
        if not existing_pythonpath
        else os.pathsep.join((str(_REPOSITORY_ROOT), existing_pythonpath))
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    try:
        completed = subprocess.run(
            [str(converter_python), str(Path(__file__).resolve()), "--prepare-fixture", str(root)],
            cwd=_REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportToolchainProbeError("governed converter environment could not create the PEFT fixture") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-2_000:]
        raise ExportToolchainProbeError(f"governed converter environment failed to create the PEFT fixture: {detail}")
    try:
        payload = json.loads(completed.stdout)
        merged = Path(payload["merged_model"]).resolve(strict=True)
        adapter = Path(payload["adapter"]).resolve(strict=True)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExportToolchainProbeError(
            "governed converter environment returned invalid PEFT fixture evidence"
        ) from exc
    if merged.parent != root.resolve() or adapter.parent != root.resolve():
        raise ExportToolchainProbeError(
            "governed converter environment returned PEFT fixture paths outside the probe root"
        )
    return merged, adapter


def _artifact_record(path: Path) -> dict[str, int | str]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ExportToolchainProbeError(f"export artifact is absent or empty: {path.name}")
    return {"file": path.name, "size_bytes": path.stat().st_size, "sha256": _sha256(path)}


def _evidence_file_record(payload: bytes, *, member: str) -> dict[str, int | str]:
    return {"kind": "file", "member": member, "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _collect_evidence_tree(root: Path, *, prefix: str) -> tuple[dict[str, bytes], dict[str, Any]]:
    files: dict[str, bytes] = {}
    rows: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ExportToolchainProbeError(f"probe evidence tree contains a symlink: {path.name}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        member = f"{prefix}/{relative}"
        payload = path.read_bytes()
        files[member] = payload
        rows.append((relative, len(payload), hashlib.sha256(payload).hexdigest()))
    if not rows:
        raise ExportToolchainProbeError(f"probe evidence tree is empty: {root.name}")
    canonical = "".join(f"{relative}\0{size}\0{digest}\n" for relative, size, digest in rows).encode("utf-8")
    return files, {
        "kind": "tree",
        "prefix": f"{prefix}/",
        "file_count": len(rows),
        "size_bytes": sum(row[1] for row in rows),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _interpreter_closure_capture() -> tuple[dict[str, Any], dict[str, Path]]:
    """Hash the effective interpreter closure and retain every row's source path."""
    prefix = Path(sys.prefix).resolve(strict=True)
    base_prefix = Path(sys.base_prefix).resolve(strict=True)
    roots: list[tuple[str, Path]] = [("environment", prefix)]
    stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    if not stdlib.is_relative_to(prefix):
        roots.append(("stdlib", stdlib))
    dll_root = base_prefix / "DLLs"
    if dll_root.is_dir() and not dll_root.resolve().is_relative_to(prefix):
        roots.append(("base-dlls", dll_root.resolve()))
    explicit_files = [Path(sys.executable).resolve(strict=True)]
    if base_prefix != prefix:
        explicit_files.extend(
            path.resolve(strict=True)
            for pattern in ("python*.dll", "vcruntime*.dll", "ucrtbase.dll")
            for path in base_prefix.glob(pattern)
            if path.is_file()
        )
    rows: dict[str, tuple[int, str]] = {}
    sources: dict[str, Path] = {}
    for label, root in roots:
        for path in sorted(root.rglob("*")):
            relative_path = path.relative_to(root)
            if label == "stdlib" and relative_path.parts[:1] == ("site-packages",):
                continue
            if path.is_symlink():
                raise ExportToolchainProbeError(f"interpreter closure contains a symlink: {label}/{path.name}")
            if not path.is_file():
                continue
            relative = relative_path.as_posix()
            logical_path = f"{label}/{relative}"
            rows[logical_path] = (path.stat().st_size, _sha256(path))
            sources[logical_path] = path
    for path in explicit_files:
        logical_path = f"runtime/{path.name}"
        rows[logical_path] = (path.stat().st_size, _sha256(path))
        sources[logical_path] = path
    ordered = [{"path": path, "size_bytes": size, "sha256": digest} for path, (size, digest) in sorted(rows.items())]
    if not ordered:
        raise ExportToolchainProbeError("interpreter dependency closure is empty")
    canonical = "".join(f"{row['path']}\0{row['size_bytes']}\0{row['sha256']}\n" for row in ordered).encode("utf-8")
    return {
        "schema_version": 1,
        "implementation": sys.implementation.name,
        "version": ".".join(str(part) for part in sys.version_info[:3]),
        "file_count": len(ordered),
        "size_bytes": sum(int(row["size_bytes"]) for row in ordered),
        "closure_sha256": hashlib.sha256(canonical).hexdigest(),
        "files": ordered,
    }, sources


def _interpreter_closure_payload() -> dict[str, Any]:
    """Hash the effective interpreter, environment, standard library, and DLL closure."""
    return _interpreter_closure_capture()[0]


def _interpreter_closure_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_interpreter_closure_atomic(path: Path, *, objects_path: Path | None = None) -> dict[str, Any]:
    payload, sources = _interpreter_closure_capture()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_interpreter_closure_bytes(payload))
    if objects_path is not None:
        objects_path.parent.mkdir(parents=True, exist_ok=True)
        objects_temporary = objects_path.with_name(f".{objects_path.name}.{os.getpid()}.tmp")
        unique_sources: dict[str, Path] = {}
        for row in payload["files"]:
            unique_sources.setdefault(row["sha256"], sources[row["path"]])
        with zipfile.ZipFile(objects_temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for digest, source in sorted(unique_sources.items()):
                info = zipfile.ZipInfo(f"objects/{digest}", date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_STORED
                with source.open("rb") as input_stream, archive.open(info, "w", force_zip64=True) as output_stream:
                    shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        os.replace(objects_temporary, objects_path)
    os.replace(temporary, path)
    return payload


def _capture_interpreter_closure(
    converter_python: Path,
    root: Path,
    *,
    timeout_seconds: int,
    objects_output: Path | None = None,
) -> dict[str, Any]:
    output = root / f"interpreter-closure-{os.getpid()}.json"
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    try:
        command = [str(converter_python), str(Path(__file__).resolve()), "--write-interpreter-closure", str(output)]
        if objects_output is not None:
            command.extend(["--write-interpreter-closure-objects", str(objects_output)])
        completed = subprocess.run(
            command,
            cwd=_REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportToolchainProbeError("converter interpreter closure could not be captured") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-2_000:]
        raise ExportToolchainProbeError(f"converter interpreter closure capture failed: {detail}")
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportToolchainProbeError("converter interpreter closure capture is unreadable") from exc
    output.unlink(missing_ok=True)
    return payload


def _validate_interpreter_closure_objects(path: Path, closure: dict[str, Any]) -> None:
    expected_sizes: dict[str, int] = {}
    for row in closure["files"]:
        digest = row["sha256"]
        size = row["size_bytes"]
        if digest in expected_sizes and expected_sizes[digest] != size:
            raise ExportToolchainProbeError("interpreter closure aliases one digest to multiple sizes")
        expected_sizes[digest] = size
    expected_members = {f"objects/{digest}" for digest in expected_sizes}
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or set(names) != expected_members:
                raise ExportToolchainProbeError("interpreter closure object archive member set is invalid")
            for info in infos:
                digest = info.filename.removeprefix("objects/")
                observed = hashlib.sha256()
                size = 0
                with archive.open(info) as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        observed.update(chunk)
                        size += len(chunk)
                if size != expected_sizes[digest] or observed.hexdigest() != digest:
                    raise ExportToolchainProbeError(f"interpreter closure object bytes are invalid: {digest}")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ExportToolchainProbeError("interpreter closure object archive is unreadable") from exc


def _load_interpreter_closure(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportToolchainProbeError("expected interpreter closure is unreadable") from exc
    expected_fields = {
        "schema_version",
        "implementation",
        "version",
        "file_count",
        "size_bytes",
        "closure_sha256",
        "files",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields or payload["schema_version"] != 1:
        raise ExportToolchainProbeError("expected interpreter closure schema is invalid")
    rows = payload["files"]
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or set(row) != {"path", "size_bytes", "sha256"}
        or not isinstance(row["path"], str)
        or PurePosixPath(row["path"]).is_absolute()
        or ".." in PurePosixPath(row["path"]).parts
        or "\\" in row["path"]
        or not isinstance(row["size_bytes"], int)
        or row["size_bytes"] < 0
        or re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"])) is None
        for row in rows
    ):
        raise ExportToolchainProbeError("expected interpreter closure rows are invalid")
    canonical = "".join(f"{row['path']}\0{row['size_bytes']}\0{row['sha256']}\n" for row in rows).encode("utf-8")
    if (
        rows != sorted(rows, key=lambda row: row["path"])
        or not rows
        or not isinstance(payload["implementation"], str)
        or not payload["implementation"]
        or not isinstance(payload["version"], str)
        or not payload["version"]
        or len({row["path"] for row in rows}) != len(rows)
        or payload["file_count"] != len(rows)
        or payload["size_bytes"] != sum(int(row["size_bytes"]) for row in rows)
        or payload["closure_sha256"] != hashlib.sha256(canonical).hexdigest()
    ):
        raise ExportToolchainProbeError("expected interpreter closure identity is invalid")
    return payload


def _require_interpreter_closure(
    converter_python: Path,
    root: Path,
    expected: dict[str, Any],
    *,
    timeout_seconds: int,
) -> None:
    observed = _capture_interpreter_closure(converter_python, root, timeout_seconds=timeout_seconds)
    if observed != expected:
        raise ExportToolchainProbeError("converter interpreter dependency closure changed after its protected snapshot")


def _snapshot_path(path: Path, *, prefix: str) -> tuple[dict[str, bytes], dict[str, Any]]:
    if path.is_symlink():
        raise ExportToolchainProbeError(f"probe step snapshot contains a symlink: {path.name}")
    if path.is_dir():
        return _collect_evidence_tree(path, prefix=prefix)
    if not path.is_file():
        raise ExportToolchainProbeError(f"probe step snapshot input is absent: {path.name}")
    payload = path.read_bytes()
    if not payload:
        raise ExportToolchainProbeError(f"probe step snapshot input is empty: {path.name}")
    member = f"{prefix}/{path.name}"
    return {member: payload}, _evidence_file_record(payload, member=member)


def _snapshot_identity(record: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(record.get(key) for key in ("kind", "file_count", "size_bytes", "sha256"))


def _write_evidence_archive_atomic(
    path: Path,
    *,
    interpreter_closure: dict[str, Any],
    interpreter_closure_objects: Path,
    snapshot_members: dict[str, bytes],
    bundle_tools: dict[str, Path],
    steps: dict[str, dict[str, Any]],
) -> dict[str, int | str]:
    members = dict(snapshot_members)
    closure_bytes = _interpreter_closure_bytes(interpreter_closure)
    closure_member = "runtime/interpreter-closure.json"
    members[closure_member] = closure_bytes
    unique_object_sizes: dict[str, int] = {}
    for row in interpreter_closure["files"]:
        unique_object_sizes.setdefault(row["sha256"], row["size_bytes"])
    object_prefix = "runtime/interpreter-closure-objects"
    execution = {
        "schema_version": 2,
        "cwd": "workspace",
        "interpreter_closure": {
            **_evidence_file_record(closure_bytes, member=closure_member),
            "file_count": interpreter_closure["file_count"],
            "closure_sha256": interpreter_closure["closure_sha256"],
            "objects_prefix": f"{object_prefix}/",
            "object_count": len(unique_object_sizes),
            "object_size_bytes": sum(unique_object_sizes.values()),
        },
        "bundle_tools": {
            name: {"member": path.name, "sha256": _sha256(path)} for name, path in sorted(bundle_tools.items())
        },
        "steps": steps,
    }
    members["execution.json"] = (json.dumps(execution, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_rows = [
        {"path": member, "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        for member, payload in sorted(members.items())
    ]
    manifest_rows.extend(
        {
            "path": f"{object_prefix}/{digest}",
            "size_bytes": size,
            "sha256": digest,
        }
        for digest, size in sorted(unique_object_sizes.items())
    )
    manifest = {
        "schema_version": 1,
        "files": manifest_rows,
    }
    members["evidence-manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for member, payload in sorted(members.items()):
            info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
        with zipfile.ZipFile(interpreter_closure_objects) as object_archive:
            for digest in sorted(unique_object_sizes):
                source_info = object_archive.getinfo(f"objects/{digest}")
                info = zipfile.ZipInfo(f"{object_prefix}/{digest}", date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_STORED
                with object_archive.open(source_info) as source, archive.open(info, "w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
    os.replace(temporary, path)
    return {"file": path.name, "size_bytes": path.stat().st_size, "sha256": _sha256(path)}


def _write_receipt_atomic(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def run_probe(
    *,
    user_dir: Path | None,
    receipt_path: Path,
    converter_python: Path = Path(sys.executable),
    timeout_seconds: int = _COMMAND_TIMEOUT_SECONDS,
    bootstrap_bundle_root: Path | None = None,
    bootstrap_platform: str | None = None,
    bootstrap_accelerator: str | None = None,
    bootstrap_inner_manifest_sha256: str | None = None,
    bootstrap_source_commit: str | None = None,
    expected_interpreter_closure_path: Path | None = None,
    expected_interpreter_closure_objects_path: Path | None = None,
) -> dict[str, Any]:
    """Run merge, conversion, imatrix, and quantization from the canonical bundle."""
    resolved_python = converter_python.resolve(strict=True)
    if not resolved_python.is_absolute() or not resolved_python.is_file():
        raise ExportToolchainProbeError("converter interpreter must be an absolute existing file")
    bootstrap_values = (
        bootstrap_bundle_root,
        bootstrap_platform,
        bootstrap_accelerator,
        bootstrap_inner_manifest_sha256,
        bootstrap_source_commit,
        expected_interpreter_closure_path,
        expected_interpreter_closure_objects_path,
    )
    if any(value is not None for value in bootstrap_values) and not all(
        value is not None for value in bootstrap_values
    ):
        raise ExportToolchainProbeError("release-bootstrap trust arguments must be supplied together")
    logical_tools = ("convert_hf_to_gguf", "convert_lora_to_gguf", "imatrix", "quantize")
    if bootstrap_bundle_root is not None:
        if user_dir is not None:
            raise ExportToolchainProbeError("release-bootstrap mode cannot accept a canonical user directory")
        if re.fullmatch(r"[0-9a-f]{40}", str(bootstrap_source_commit)) is None:
            raise ExportToolchainProbeError("release-bootstrap source commit is invalid")
        try:
            _enable_repository_runtime_import()
            from vetinari.engine.binary_bundle import resolve_bootstrap_bundle_tool

            tools = {
                name: resolve_bootstrap_bundle_tool(
                    name,
                    bundle_root=bootstrap_bundle_root,
                    platform=str(bootstrap_platform),
                    accelerator=str(bootstrap_accelerator),
                    expected_inner_manifest_sha256=str(bootstrap_inner_manifest_sha256),
                )
                for name in logical_tools
            }
        except Exception as exc:
            raise ExportToolchainProbeError(str(exc)) from exc
        trust = {
            "mode": "release-bootstrap",
            "source_commit": bootstrap_source_commit,
            "platform": bootstrap_platform,
            "accelerator": bootstrap_accelerator,
            "inner_manifest_sha256": bootstrap_inner_manifest_sha256,
        }
    else:
        tools = {name: resolve_bundle_tool(name, user_dir=user_dir) for name in logical_tools}
        trust = _canonical_authority_receipt()
    if len({path.parent for path in tools.values()}) != 1:
        raise ExportToolchainProbeError("resolved export tools do not share one canonical installation")

    with tempfile.TemporaryDirectory(prefix="amw-export-probe-") as temporary:
        root = Path(temporary)
        if expected_interpreter_closure_path is not None:
            interpreter_closure = _load_interpreter_closure(expected_interpreter_closure_path.resolve(strict=True))
            interpreter_closure_objects = expected_interpreter_closure_objects_path.resolve(strict=True)
        else:
            interpreter_closure_objects = root / "interpreter-closure-objects.zip"
            interpreter_closure = _capture_interpreter_closure(
                resolved_python,
                root,
                timeout_seconds=timeout_seconds,
                objects_output=interpreter_closure_objects,
            )
        _validate_interpreter_closure_objects(interpreter_closure_objects, interpreter_closure)
        _require_interpreter_closure(
            resolved_python,
            root,
            interpreter_closure,
            timeout_seconds=timeout_seconds,
        )
        merged_model, adapter = _create_and_merge_lora_fixture_with_interpreter(
            resolved_python,
            root,
            timeout_seconds=timeout_seconds,
        )
        _require_interpreter_closure(
            resolved_python,
            root,
            interpreter_closure,
            timeout_seconds=timeout_seconds,
        )
        f16 = root / "tiny-model-f16.gguf"
        imatrix = root / "tiny-model.imatrix"
        quantized = root / "tiny-model-q4_0.gguf"
        corpus = root / "calibration.txt"
        corpus.write_text("hello export toolchain\nhello toolchain\n", encoding="utf-8", newline="\n")
        adapter_gguf = root / "adapter.gguf"
        commands = {
            "convert_hf_to_gguf": [
                str(resolved_python),
                str(tools["convert_hf_to_gguf"]),
                str(merged_model),
                "--outfile",
                str(f16),
                "--outtype",
                "f16",
            ],
            "convert_lora_to_gguf": [
                str(resolved_python),
                str(tools["convert_lora_to_gguf"]),
                "--base",
                str(merged_model),
                "--outfile",
                str(adapter_gguf),
                str(adapter),
            ],
            "imatrix": [
                str(tools["imatrix"]),
                "-m",
                str(f16),
                "-f",
                str(corpus),
                "-o",
                str(imatrix),
                "-ngl",
                "0",
            ],
            "quantize": [
                str(tools["quantize"]),
                "--imatrix",
                str(imatrix),
                str(f16),
                str(quantized),
                "Q4_0",
            ],
        }
        step_contracts = {
            "convert_hf_to_gguf": {
                "executable": "converter_python",
                "argv": [
                    "bundle-tool:convert_hf_to_gguf",
                    "object:merged_model",
                    "--outfile",
                    "object:f16",
                    "--outtype",
                    "f16",
                ],
                "inputs": ["converter_python", "convert_hf_to_gguf", "merged_model"],
                "outputs": ["f16"],
            },
            "convert_lora_to_gguf": {
                "executable": "converter_python",
                "argv": [
                    "bundle-tool:convert_lora_to_gguf",
                    "--base",
                    "object:merged_model",
                    "--outfile",
                    "object:adapter_gguf",
                    "object:lora_adapter",
                ],
                "inputs": ["converter_python", "convert_lora_to_gguf", "merged_model", "lora_adapter"],
                "outputs": ["adapter_gguf"],
            },
            "imatrix": {
                "executable": "bundle-tool:imatrix",
                "argv": [
                    "-m",
                    "object:f16",
                    "-f",
                    "object:corpus",
                    "-o",
                    "object:imatrix_output",
                    "-ngl",
                    "0",
                ],
                "inputs": ["imatrix", "f16", "corpus"],
                "outputs": ["imatrix_output"],
            },
            "quantize": {
                "executable": "bundle-tool:quantize",
                "argv": ["--imatrix", "object:imatrix_output", "object:f16", "object:quantized", "Q4_0"],
                "inputs": ["quantize", "imatrix_output", "f16"],
                "outputs": ["quantized"],
            },
        }
        object_paths = {
            "converter_python": resolved_python,
            **tools,
            "merged_model": merged_model,
            "lora_adapter": adapter,
            "corpus": corpus,
            "f16": f16,
            "adapter_gguf": adapter_gguf,
            "imatrix_output": imatrix,
            "quantized": quantized,
        }
        snapshot_members: dict[str, bytes] = {}
        established_outputs: dict[str, tuple[Path, dict[str, Any]]] = {}
        first_input_identities: dict[str, tuple[Any, ...]] = {}
        steps: dict[str, dict[str, Any]] = {}
        for ordinal, (name, contract) in enumerate(step_contracts.items(), start=1):
            step_prefix = f"steps/{ordinal:02d}-{name}"
            for output_name in contract["outputs"]:
                if object_paths[output_name].exists() or output_name in established_outputs:
                    raise ExportToolchainProbeError(f"export process output existed before execution: {output_name}")
            _require_interpreter_closure(
                resolved_python,
                root,
                interpreter_closure,
                timeout_seconds=timeout_seconds,
            )
            input_snapshots: dict[str, dict[str, Any]] = {}
            for input_name in contract["inputs"]:
                members, record = _snapshot_path(
                    object_paths[input_name],
                    prefix=f"{step_prefix}/inputs/{input_name}",
                )
                if set(snapshot_members).intersection(members):
                    raise ExportToolchainProbeError(f"probe input snapshot member collision: {input_name}")
                snapshot_members.update(members)
                input_snapshots[input_name] = record
                identity = _snapshot_identity(record)
                if input_name in established_outputs and identity != _snapshot_identity(
                    established_outputs[input_name][1]
                ):
                    raise ExportToolchainProbeError(
                        f"export process input no longer matches producer bytes: {name}/{input_name}"
                    )
                if input_name in first_input_identities and first_input_identities[input_name] != identity:
                    raise ExportToolchainProbeError(f"reused export process input changed between steps: {input_name}")
                first_input_identities.setdefault(input_name, identity)
            process_result = _run(commands[name], cwd=root, timeout_seconds=timeout_seconds)
            output_snapshots: dict[str, dict[str, Any]] = {}
            for output_name in contract["outputs"]:
                members, record = _snapshot_path(
                    object_paths[output_name],
                    prefix=f"{step_prefix}/outputs/{output_name}",
                )
                if set(snapshot_members).intersection(members):
                    raise ExportToolchainProbeError(f"probe output snapshot member collision: {output_name}")
                snapshot_members.update(members)
                output_snapshots[output_name] = record
                established_outputs[output_name] = (object_paths[output_name], record)
            for input_name, before in input_snapshots.items():
                _members, after = _snapshot_path(
                    object_paths[input_name],
                    prefix=f"verification/{name}/inputs/{input_name}",
                )
                if _snapshot_identity(after) != _snapshot_identity(before):
                    raise ExportToolchainProbeError(f"export process mutated its input bytes: {name}/{input_name}")
            for prior_name, (prior_path, prior_record) in established_outputs.items():
                _members, observed = _snapshot_path(
                    prior_path,
                    prefix=f"verification/{name}/retained/{prior_name}",
                )
                if _snapshot_identity(observed) != _snapshot_identity(prior_record):
                    raise ExportToolchainProbeError(f"later export process overwrote prior output bytes: {prior_name}")
            _require_interpreter_closure(
                resolved_python,
                root,
                interpreter_closure,
                timeout_seconds=timeout_seconds,
            )
            steps[name] = {
                **contract,
                "input_snapshots": input_snapshots,
                "output_snapshots": output_snapshots,
                "interpreter_closure_sha256": interpreter_closure["closure_sha256"],
                "cwd": "workspace",
                "returncode": process_result["returncode"],
                "stdout_sha256": process_result["stdout_sha256"],
                "stderr_sha256": process_result["stderr_sha256"],
            }
        _require_interpreter_closure(
            resolved_python,
            root,
            interpreter_closure,
            timeout_seconds=timeout_seconds,
        )
        _validate_interpreter_closure_objects(interpreter_closure_objects, interpreter_closure)
        artifact_paths = [f16, root / "adapter.gguf", imatrix, quantized]
        artifacts = {path.stem: _artifact_record(path) for path in artifact_paths}
        if len({row["sha256"] for row in artifacts.values()}) != len(artifacts):
            raise ExportToolchainProbeError("export artifacts are not byte-distinct")
        evidence_path = receipt_path.with_name(f"{receipt_path.stem}.evidence.zip")
        evidence_archive = _write_evidence_archive_atomic(
            evidence_path,
            interpreter_closure=interpreter_closure,
            interpreter_closure_objects=interpreter_closure_objects,
            snapshot_members=snapshot_members,
            bundle_tools=tools,
            steps=steps,
        )
        receipt = {
            "schema_version": 1,
            "status": "passed",
            "trust": trust,
            "tools": {name: {"path": str(path), "sha256": _sha256(path)} for name, path in sorted(tools.items())},
            "interpreter_closure": {
                "file_count": interpreter_closure["file_count"],
                "closure_sha256": interpreter_closure["closure_sha256"],
                "object_count": len({row["sha256"] for row in interpreter_closure["files"]}),
            },
            "steps": steps,
            "artifacts": artifacts,
            "evidence_archive": evidence_archive,
        }
        _write_receipt_atomic(receipt_path, receipt)
        return receipt


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-dir", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--converter-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--timeout-seconds", type=int, default=_COMMAND_TIMEOUT_SECONDS)
    parser.add_argument("--prepare-fixture", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--write-interpreter-closure", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--write-interpreter-closure-objects", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--expected-interpreter-closure", type=Path)
    parser.add_argument("--expected-interpreter-closure-objects", type=Path)
    parser.add_argument("--bootstrap-bundle-root", type=Path)
    parser.add_argument("--bootstrap-platform", choices=("windows", "linux"))
    parser.add_argument("--bootstrap-accelerator", choices=("cpu", "cuda"))
    parser.add_argument("--bootstrap-inner-manifest-sha256")
    parser.add_argument("--bootstrap-source-commit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.write_interpreter_closure_objects is not None and args.write_interpreter_closure is None:
        print("AM Engine interpreter closure capture failed: object output requires closure output", file=sys.stderr)
        return 1
    if args.write_interpreter_closure is not None:
        try:
            payload = _write_interpreter_closure_atomic(
                args.write_interpreter_closure,
                objects_path=args.write_interpreter_closure_objects,
            )
        except (ExportToolchainProbeError, OSError, ValueError) as exc:
            print(f"AM Engine interpreter closure capture failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"file_count": payload["file_count"], "closure_sha256": payload["closure_sha256"]}))
        return 0
    if args.prepare_fixture is not None:
        try:
            merged, adapter = _create_and_merge_lora_fixture(args.prepare_fixture.resolve(strict=True))
        except (ExportToolchainProbeError, OSError, ValueError) as exc:
            print(f"AM Engine PEFT fixture preparation failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"merged_model": str(merged), "adapter": str(adapter)}, sort_keys=True))
        return 0
    try:
        if args.receipt is None:
            with tempfile.TemporaryDirectory(prefix="amw-export-receipt-") as temporary:
                receipt = run_probe(
                    user_dir=args.user_dir,
                    receipt_path=Path(temporary) / "receipt.json",
                    converter_python=args.converter_python,
                    timeout_seconds=args.timeout_seconds,
                    bootstrap_bundle_root=args.bootstrap_bundle_root,
                    bootstrap_platform=args.bootstrap_platform,
                    bootstrap_accelerator=args.bootstrap_accelerator,
                    bootstrap_inner_manifest_sha256=args.bootstrap_inner_manifest_sha256,
                    bootstrap_source_commit=args.bootstrap_source_commit,
                    expected_interpreter_closure_path=args.expected_interpreter_closure,
                    expected_interpreter_closure_objects_path=args.expected_interpreter_closure_objects,
                )
        else:
            receipt = run_probe(
                user_dir=args.user_dir,
                receipt_path=args.receipt,
                converter_python=args.converter_python,
                timeout_seconds=args.timeout_seconds,
                bootstrap_bundle_root=args.bootstrap_bundle_root,
                bootstrap_platform=args.bootstrap_platform,
                bootstrap_accelerator=args.bootstrap_accelerator,
                bootstrap_inner_manifest_sha256=args.bootstrap_inner_manifest_sha256,
                bootstrap_source_commit=args.bootstrap_source_commit,
                expected_interpreter_closure_path=args.expected_interpreter_closure,
                expected_interpreter_closure_objects_path=args.expected_interpreter_closure_objects,
            )
    except (ExportToolchainProbeError, OSError, ValueError) as exc:
        print(f"AM Engine export toolchain probe failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
