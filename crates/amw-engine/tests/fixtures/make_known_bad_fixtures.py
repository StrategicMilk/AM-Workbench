"""Generate the deterministic, network-free GGUF contract fixtures."""

from __future__ import annotations

import struct
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("<Q", len(encoded)) + encoded


def _metadata_string(key: str, value: str) -> bytes:
    return _string(key) + struct.pack("<I", 8) + _string(value)


def build_tiny_gguf() -> bytes:
    payload = bytearray(b"GGUF")
    payload += struct.pack("<IQQ", 3, 1, 2)
    payload += _metadata_string("general.architecture", "amw-test")
    payload += _metadata_string("general.name", "tiny-cpu")
    payload += _string("weight")
    payload += struct.pack("<I", 1)
    payload += struct.pack("<Q", 1)
    payload += struct.pack("<IQ", 0, 0)
    payload += b"\0" * ((-len(payload)) % 32)
    payload += struct.pack("<f", 1.0)
    return bytes(payload)


def main() -> None:
    tiny = build_tiny_gguf()
    (FIXTURE_DIR / "tiny-cpu.gguf").write_bytes(tiny)
    corrupt = bytearray(tiny)
    corrupt[:4] = b"NOPE"
    (FIXTURE_DIR / "corrupt-header.gguf").write_bytes(corrupt)
    (FIXTURE_DIR / "truncated-tensor.gguf").write_bytes(tiny[:-2])


if __name__ == "__main__":
    main()
