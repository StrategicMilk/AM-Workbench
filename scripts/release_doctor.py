"""Release doctor for Vetinari.

Validates a release end-to-end in a clean, isolated environment and writes a
``ReleaseProof`` artifact to ``outputs/release/<version>/proof.json``.

Pipeline (each step halts on first failure):
    1. Build wheel + sdist via ``python -m build``.
    2. Create a fresh venv in a tempdir.
    3. Install the built wheel with default deps (no extras) -- this validates
       the canonical user install path, which includes the full training stack
       (torch, unsloth, peft, trl) because self-learning is a core Vetinari
       feature, not an optional extra.  See SHARD-02 adaptation note.
    4. Run ``python -m vetinari --doctor`` in the new venv; capture exit code.
    5. Run a minimal smoke request (probe the ``/health`` endpoint on a random
       port); capture latency and exit code.
    6. Compute wheel SHA-256 and optionally sign the proof.
    7. Write ``outputs/release/<version>/proof.json``.

Smoke test installs default deps including training stack -- validates the
canonical user install path.

Any step that cannot run due to a missing dependency or unavailable tool is
treated as FAIL, never as PASS or SKIP.  This enforces the
"Unavailable-dependency pass-through" anti-pattern rule.

Usage::

    python scripts/release_doctor.py --version 0.9.0
    python scripts/release_doctor.py --version dev --quiet

Exit codes:
    0  All steps passed; proof written.
    1  One or more steps failed; proof written with non-zero exit codes.
    2  Script invocation error (bad args, missing build tools).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import logging
import platform
import random
import subprocess
import sys
import tempfile
import time
import venv
from datetime import datetime, timezone
from pathlib import Path

# Allow importing from the project root without installing in dev mode.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from vetinari.release import (
    PROOF_SCHEMA_VERSION,
    ClaimKind,
    ReleaseClaimRecord,
    ReleaseProof,
)

logger = logging.getLogger(__name__)

# Static smoke script written to a temp file at runtime.
# Port is passed as sys.argv[1] -- NO f-string interpolation into subprocess payloads.
# Output prefixes consumed by step_smoke: SMOKE_LATENCY_MS, SMOKE_STATUS, SMOKE_FAIL.
_SMOKE_SCRIPT = """\
import sys
import time
import subprocess
import urllib.request

port = int(sys.argv[1])
server_proc = subprocess.Popen(
    [sys.executable, "-m", "vetinari", "serve", "--port", str(port)],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)

# Wait up to 15s for server to become ready.
deadline = time.monotonic() + 15
ready = False
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            if r.status == 200:
                ready = True
                break
    except Exception:
        time.sleep(0.5)

if not ready:
    server_proc.terminate()
    server_proc.wait(timeout=5)
    print("SMOKE_FAIL: server did not become ready", flush=True)
    sys.exit(1)

# Measure the actual /health latency.
t0 = time.monotonic()
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as r:
        body = r.read()
    latency_ms = (time.monotonic() - t0) * 1000
    print(f"SMOKE_LATENCY_MS:{latency_ms:.2f}", flush=True)
    print(f"SMOKE_STATUS:{r.status}", flush=True)
    exit_code = 0 if r.status == 200 else 1
except Exception as exc:
    latency_ms = (time.monotonic() - t0) * 1000
    print(f"SMOKE_FAIL: {exc}", flush=True)
    exit_code = 1
finally:
    server_proc.terminate()
    server_proc.wait(timeout=10)

sys.exit(exit_code)
"""

# Output directory for release artifacts (relative to repo root).
_OUTPUTS_DIR = _REPO_ROOT / "outputs" / "release"

# Timeout for subprocess steps in seconds.
_BUILD_TIMEOUT_S = 300  # 5 min -- wheel builds can be slow
_VENV_TIMEOUT_S = 600  # 10 min -- training stack install is large
_DOCTOR_TIMEOUT_S = 120
_SMOKE_TIMEOUT_S = 60


# ── Helpers ────────────────────────────────────────────────────────────────


def _utcnow() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _sha256_of_file(path: Path) -> str:
    """Compute hex-encoded SHA-256 of a file.

    Args:
        path: Path to the file to hash.

    Returns:
        Lowercase hex string of the SHA-256 digest.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
    capture: bool = True,
    label: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return the completed process.

    Always uses check=False so callers can inspect the exit code.

    Args:
        cmd: Command and arguments list.
        cwd: Working directory; defaults to repo root.
        timeout: Maximum seconds to wait.
        capture: If True, capture stdout+stderr; otherwise inherit.
        label: Human-readable label for log messages.

    Returns:
        ``subprocess.CompletedProcess`` with ``returncode``, ``stdout``,
        ``stderr`` populated.
    """
    effective_cwd = cwd or _REPO_ROOT
    logger.debug("Running %s: %s", label or cmd[0], " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=str(effective_cwd),
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def _relative_to_repo(path: Path) -> str:
    """Return a repo-relative path string, or the absolute path if outside the repo.

    Args:
        path: Absolute path to convert.

    Returns:
        Relative path string if *path* is under ``_REPO_ROOT``, otherwise the
        absolute path string.  Using the absolute path as evidence_path is safe
        and correct for temp-dir artifacts.
    """
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def _find_wheel(dist_dir: Path) -> Path | None:
    """Return the first ``.whl`` file found in *dist_dir*, or None.

    Args:
        dist_dir: Directory to search for wheel files.

    Returns:
        Path to the wheel file, or ``None`` if none found.
    """
    wheels = sorted(dist_dir.glob("*.whl"))
    return wheels[-1] if wheels else None


def _random_port() -> int:
    """Return a random unprivileged port number.

    Returns:
        Integer port in range [49152, 65535].
    """
    return random.randint(49152, 65535)


def _make_claim(
    claim_id: str,
    text: str,
    kind: ClaimKind = ClaimKind.TOOL_EVIDENCE,
    evidence_path: str = "",
) -> ReleaseClaimRecord:
    """Construct a ``ReleaseClaimRecord`` timestamped now.

    Args:
        claim_id: Unique identifier for this claim.
        text: Human-readable description of what was verified.
        kind: Evidence category.
        evidence_path: Optional path to the evidence file.

    Returns:
        A new ``ReleaseClaimRecord`` with ``verified_at`` set to now.
    """
    return ReleaseClaimRecord(
        id=claim_id,
        text=text,
        evidence_path=evidence_path,
        kind=kind,
        verified_at=_utcnow(),
    )


# ── Step implementations ───────────────────────────────────────────────────


def step_build(out_dir: Path) -> tuple[bool, Path | None, list[ReleaseClaimRecord]]:
    """Build wheel and sdist via ``python -m build``.

    Args:
        out_dir: Directory to write built artifacts into.

    Returns:
        Tuple of (success, wheel_path, claims).  ``wheel_path`` is None on
        failure.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Building wheel and sdist -> %s", out_dir)
    result = _run(
        [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(out_dir)],
        timeout=_BUILD_TIMEOUT_S,
        label="build",
    )
    if result.returncode != 0:
        logger.error(
            "Build failed (exit %d) -- release cannot proceed.\nstderr:\n%s",
            result.returncode,
            result.stderr[-2000:],
        )
        return (
            False,
            None,
            [
                _make_claim(
                    "wheel-build",
                    f"python -m build FAILED (exit {result.returncode})",
                )
            ],
        )
    wheel = _find_wheel(out_dir)
    if wheel is None:
        logger.error("Build succeeded but no .whl file found in %s", out_dir)
        return False, None, [_make_claim("wheel-build", "Build produced no .whl file")]
    logger.info("Built wheel: %s (%d bytes)", wheel.name, wheel.stat().st_size)
    return (
        True,
        wheel,
        [
            _make_claim(
                "wheel-build",
                f"Wheel built successfully: {wheel.name}",
                evidence_path=_relative_to_repo(wheel),
            )
        ],
    )


def step_install_in_clean_venv(wheel: Path, venv_dir: Path) -> tuple[bool, list[ReleaseClaimRecord]]:
    """Create a fresh venv and install the wheel with default deps.

    Installs without extras so the full default dependency set (including the
    training stack) is exercised -- this is the canonical user install path.

    Args:
        wheel: Path to the built ``.whl`` file.
        venv_dir: Directory to create the venv in (will be created).

    Returns:
        Tuple of (success, claims).
    """
    logger.info("Creating clean venv at %s", venv_dir)
    venv.create(str(venv_dir), with_pip=True, clear=True)

    # Determine the venv's python executable path.
    if platform.system() == "Windows":
        venv_python = venv_dir / "Scripts" / "python.exe"
        venv_pip = venv_dir / "Scripts" / "pip.exe"
    else:
        venv_python = venv_dir / "bin" / "python"
        venv_pip = venv_dir / "bin" / "pip"

    if not venv_python.exists():
        logger.error("Venv python not found at %s", venv_python)
        return False, [
            _make_claim(
                "venv-install",
                f"Venv python not found at {venv_python}",
            )
        ]

    # Upgrade pip first to avoid legacy resolver issues.
    upgrade_result = _run(
        [str(venv_pip), "install", "--upgrade", "pip"],
        timeout=120,
        label="pip-upgrade",
    )
    if upgrade_result.returncode != 0:
        logger.warning(
            "pip upgrade failed (exit %d) -- proceeding with bundled pip",
            upgrade_result.returncode,
        )

    # Install the wheel with default deps (no extras).
    # Validates the canonical user install path (the bare wheel + default deps).
    # Default deps include the full training stack (torch/unsloth/peft/trl)
    # because self-learning is a core feature, not optional.
    logger.info("Installing %s (default deps, no extras)", wheel.name)
    install_result = _run(
        [str(venv_pip), "install", str(wheel)],
        timeout=_VENV_TIMEOUT_S,
        label="pip-install",
    )
    if install_result.returncode != 0:
        logger.error(
            "wheel install failed (exit %d) -- release cannot proceed.\nstderr:\n%s",
            install_result.returncode,
            install_result.stderr[-2000:],
        )
        return False, [
            _make_claim(
                "venv-install",
                f"wheel install FAILED (exit {install_result.returncode}): "
                "unavailability of any dep is a FAIL, not a PASS",
            )
        ]
    return True, [
        _make_claim(
            "venv-install",
            f"Wheel installed successfully in clean venv: {wheel.name}",
        )
    ]


def step_doctor(venv_dir: Path) -> tuple[int, list[ReleaseClaimRecord]]:
    """Run ``python -m vetinari --doctor`` in the clean venv.

    Args:
        venv_dir: Path to the clean venv produced by ``step_install_in_clean_venv``.

    Returns:
        Tuple of (exit_code, claims).
    """
    if platform.system() == "Windows":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"

    logger.info("Running vetinari --doctor in clean venv")
    result = _run(
        [str(venv_python), "-m", "vetinari", "--doctor"],
        timeout=_DOCTOR_TIMEOUT_S,
        label="vetinari-doctor",
    )
    status = "PASS" if result.returncode == 0 else f"FAIL (exit {result.returncode})"
    return result.returncode, [
        _make_claim(
            "doctor-check",
            f"python -m vetinari --doctor: {status}",
        )
    ]


def step_smoke(venv_dir: Path) -> tuple[int, float, list[ReleaseClaimRecord]]:
    """Run a minimal smoke request against the web stack in the clean venv.

    Starts the Vetinari web server on a random port, probes ``/health``, then
    shuts it down.  Captures latency from request start to response.

    Any failure (server won't start, health endpoint not 200, timeout) is
    treated as a hard FAIL.

    Args:
        venv_dir: Path to the clean venv.

    Returns:
        Tuple of (exit_code, latency_ms, claims).  exit_code 0 = passed.
    """
    if platform.system() == "Windows":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"

    port = _random_port()
    logger.info("Starting smoke server on port %d", port)

    # Write the static smoke script to a file inside the venv tempdir so that
    # the port is passed as argv[1] -- eliminates f-string interpolation into a
    # subprocess payload (injection-safe regardless of future port type changes).
    smoke_script_path = venv_dir.parent / "smoke.py"
    smoke_script_path.write_text(_SMOKE_SCRIPT, encoding="utf-8")

    t_start = time.monotonic()
    result = _run(
        [str(venv_python), str(smoke_script_path), str(port)],
        timeout=_SMOKE_TIMEOUT_S,
        label="smoke",
    )
    elapsed_ms = (time.monotonic() - t_start) * 1000

    # Extract latency from output if available.
    latency_ms = elapsed_ms
    for line in (result.stdout or "").splitlines():
        if line.startswith("SMOKE_LATENCY_MS:"):
            with contextlib.suppress(ValueError):
                latency_ms = float(line.split(":", 1)[1])

    status = "PASS" if result.returncode == 0 else f"FAIL (exit {result.returncode})"
    return (
        result.returncode,
        latency_ms,
        [
            _make_claim(
                "smoke-health",
                f"Smoke /health request: {status}, latency={latency_ms:.1f}ms",
            )
        ],
    )


def step_wheel_hash(wheel: Path) -> tuple[str, int, list[ReleaseClaimRecord]]:
    """Compute SHA-256 and size of the wheel.

    Args:
        wheel: Path to the built ``.whl`` file.

    Returns:
        Tuple of (sha256_hex, size_bytes, claims).
    """
    sha256 = _sha256_of_file(wheel)
    size = wheel.stat().st_size
    logger.info("Wheel SHA-256: %s  size: %d bytes", sha256, size)
    return (
        sha256,
        size,
        [
            _make_claim(
                "wheel-hash",
                f"Wheel SHA-256: {sha256}  size: {size} bytes",
                evidence_path=_relative_to_repo(wheel),
            )
        ],
    )


def step_sign(
    proof_json: str,
    proof_path: Path,
    key_path: Path | None,
) -> tuple[bool, list[ReleaseClaimRecord]]:
    """Optionally produce a detached GPG signature for the proof artifact.

    Accepts the proof JSON content as a string rather than a pre-written file
    so that ``run_release_doctor`` can collect the signing result before
    performing the single definitive ``_write_proof`` call.  When signing is
    requested the content is written to a temporary file for GPG; the caller
    is responsible for writing the final ``proof.json``.

    If no signing key is configured, records ``signed=False`` but does NOT
    fail -- absence of a signing key is a known acceptable state for dev/CI
    builds.  Production releases should configure a key.

    Args:
        proof_json: JSON string of the proof to sign.
        proof_path: Destination path for ``proof.json`` (used to derive the
            ``.sig`` filename; the proof is NOT written here by this function).
        key_path: Path to a GPG key file, or ``None`` to skip signing.

    Returns:
        Tuple of (signed_bool, claims).
    """
    if key_path is None:
        logger.info("No signing key configured -- skipping signature (signed=False)")
        return False, [
            _make_claim(
                "proof-signature",
                "No signing key configured; proof is unsigned (not a failure for dev builds)",
                kind=ClaimKind.HUMAN_ATTESTED,
            )
        ]

    # Write content to a tempfile so GPG has a file to sign.
    # The final proof.json is written by _write_proof after this function returns.
    sig_path = proof_path.with_suffix(".sig")
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".proof_tmp.json",
        delete=False,
        encoding="utf-8",
        dir=proof_path.parent,
    ) as tmp_fh:
        tmp_fh.write(proof_json)
        tmp_name = tmp_fh.name
    try:
        result = _run(
            ["gpg", "--detach-sign", "--armor", "--output", str(sig_path), tmp_name],
            timeout=30,
            label="gpg-sign",
        )
    finally:
        Path(tmp_name).unlink(missing_ok=True)

    if result.returncode != 0:
        logger.warning(
            "GPG signing failed (exit %d) -- proof unsigned.\nstderr:\n%s",
            result.returncode,
            result.stderr,
        )
        return False, [
            _make_claim(
                "proof-signature",
                f"GPG signing FAILED (exit {result.returncode}); proof unsigned",
            )
        ]
    logger.info("Signed proof -> %s", sig_path)
    return True, [
        _make_claim(
            "proof-signature",
            f"Detached GPG signature written: {sig_path.name}",
            evidence_path=_relative_to_repo(sig_path),
        )
    ]


def step_verify_signature(
    artifact_path: Path,
    sig_path: Path,
) -> tuple[bool, list[ReleaseClaimRecord]]:
    """Verify a detached GPG signature against an artifact and fail closed.

    Uses ``--status-fd 2`` to receive machine-readable status on stderr, then
    checks for ``[GNUPG:] GOODSIG`` and ``[GNUPG:] VALIDSIG`` in the output.
    Any failure — missing sig file, non-zero exit, absent GOODSIG/VALIDSIG — is
    treated as a hard failure rather than a warning, because a verification step
    that can succeed without a valid signature is governance theater.

    Note: The correct ``gpg --verify`` argument order is signature file FIRST,
    then the artifact (``gpg --verify <sig> <artifact>``).

    Args:
        artifact_path: Path to the file whose signature is being checked.
        sig_path: Path to the detached ``.sig`` file produced by ``step_sign``.

    Returns:
        Tuple of (ok, claims).  ``ok`` is ``False`` when verification fails
        or the sig file is absent.
    """
    if not sig_path.exists():
        logger.warning(
            "Signature file %s not found — proof signature verification skipped (fail closed)",
            sig_path,
        )
        return False, [
            _make_claim(
                "proof-signature-verify",
                f"FAIL: sig file absent ({sig_path.name}) — signature not verified",
            )
        ]

    result = _run(
        ["gpg", "--verify", "--status-fd", "2", str(sig_path), str(artifact_path)],
        timeout=30,
        label="gpg-verify",
    )
    stderr_out = result.stderr or ""
    has_goodsig = "[GNUPG:] GOODSIG" in stderr_out
    has_validsig = "[GNUPG:] VALIDSIG" in stderr_out

    if result.returncode != 0 or not has_goodsig or not has_validsig:
        logger.warning(
            "GPG signature verification FAILED (exit %d, GOODSIG=%s, VALIDSIG=%s).\nstderr:\n%s",
            result.returncode,
            has_goodsig,
            has_validsig,
            stderr_out,
        )
        return False, [
            _make_claim(
                "proof-signature-verify",
                f"FAIL: GPG verify exit={result.returncode} GOODSIG={has_goodsig} VALIDSIG={has_validsig}",
            )
        ]

    logger.info("GPG signature verified OK for %s", artifact_path.name)
    return True, [
        _make_claim(
            "proof-signature-verify",
            f"GPG signature valid: {sig_path.name} verifies {artifact_path.name}",
            evidence_path=_relative_to_repo(sig_path),
        )
    ]


# ── Wheel tamper detection ─────────────────────────────────────────────────


def _verify_wheel_hash_unchanged(
    wheel: Path,
    expected_sha256: str,
) -> tuple[bool, list[ReleaseClaimRecord]]:
    """Recompute the wheel's SHA-256 and compare against the recorded value.

    Called between ``step_wheel_hash`` and ``step_install_in_clean_venv`` to
    detect any modification to the wheel file between the build and install
    steps.  A mismatch fails closed -- the pipeline halts rather than
    installing a potentially-tampered wheel.

    Args:
        wheel: Path to the ``.whl`` file on disk.
        expected_sha256: The SHA-256 hex digest recorded immediately after the
            build step.

    Returns:
        Tuple of (ok, claims).  ``ok`` is False when the hash differs.
    """
    actual_sha256 = _sha256_of_file(wheel)
    if actual_sha256 != expected_sha256:
        logger.error(
            "Wheel hash mismatch: recorded=%s  on-disk=%s -- halting install",
            expected_sha256[:16],
            actual_sha256[:16],
        )
        return False, [
            _make_claim(
                "wheel-hash",
                f"FAIL: wheel SHA-256 mismatch (recorded {expected_sha256[:16]}... "
                f"vs on-disk {actual_sha256[:16]}...) -- possible tampering",
                kind=ClaimKind.TOOL_EVIDENCE,
            )
        ]
    return True, []


# ── Main orchestrator ──────────────────────────────────────────────────────


def run_release_doctor(
    version: str,
    *,
    quiet: bool = False,
    signing_key: Path | None = None,
) -> ReleaseProof:
    """Execute the full release doctor pipeline and return the proof artifact.

    Steps run serially; any failure halts the pipeline.  The proof artifact is
    written even on failure so callers can inspect partial results.

    Smoke test installs default deps including training stack -- validates the
    canonical user install path.

    Args:
        version: Release version string written into the proof (e.g. ``"0.9.0"``).
        quiet: If True, suppress INFO-level console output.
        signing_key: Optional path to a GPG key for signing the proof.

    Returns:
        A ``ReleaseProof`` instance written to
        ``outputs/release/<version>/proof.json``.
    """
    if not quiet:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    proof_dir = _OUTPUTS_DIR / version
    proof_dir.mkdir(parents=True, exist_ok=True)
    dist_dir = proof_dir / "dist"

    all_claims: list[ReleaseClaimRecord] = []
    built_at = _utcnow()
    wheel_sha256 = ""
    wheel_size_bytes = 0
    doctor_exit_code = -1
    smoke_exit_code = -1
    smoke_latency_ms = 0.0
    signed = False

    with tempfile.TemporaryDirectory(prefix="vetinari-release-venv-") as tmp_venv_root:
        venv_dir = Path(tmp_venv_root) / "venv"

        # Step 1: Build.
        build_ok, wheel, build_claims = step_build(dist_dir)
        all_claims.extend(build_claims)
        if not build_ok or wheel is None:
            logger.error("HALT: build step failed")
            return _write_proof(
                proof_dir,
                version,
                built_at,
                wheel_sha256,
                wheel_size_bytes,
                doctor_exit_code,
                smoke_exit_code,
                smoke_latency_ms,
                signed,
                all_claims,
            )

        # Step 2: Wheel hash (record before install so tampering is detectable).
        wheel_sha256, wheel_size_bytes, hash_claims = step_wheel_hash(wheel)
        all_claims.extend(hash_claims)

        # Step 2b: Re-verify wheel hash before install to detect tampering between
        # the build step and the install step.
        tamper_ok, tamper_claims = _verify_wheel_hash_unchanged(wheel, wheel_sha256)
        all_claims.extend(tamper_claims)
        if not tamper_ok:
            logger.error("HALT: wheel hash mismatch detected -- possible tampering")
            return _write_proof(
                proof_dir,
                version,
                built_at,
                wheel_sha256,
                wheel_size_bytes,
                doctor_exit_code,
                smoke_exit_code,
                smoke_latency_ms,
                signed,
                all_claims,
            )

        # Step 3: Install in clean venv.
        install_ok, install_claims = step_install_in_clean_venv(wheel, venv_dir)
        all_claims.extend(install_claims)
        if not install_ok:
            logger.error("HALT: clean-venv install failed")
            return _write_proof(
                proof_dir,
                version,
                built_at,
                wheel_sha256,
                wheel_size_bytes,
                doctor_exit_code,
                smoke_exit_code,
                smoke_latency_ms,
                signed,
                all_claims,
            )

        # Step 4: Doctor.
        doctor_exit_code, doctor_claims = step_doctor(venv_dir)
        all_claims.extend(doctor_claims)
        if doctor_exit_code != 0:
            logger.error("HALT: vetinari --doctor failed (exit %d)", doctor_exit_code)
            return _write_proof(
                proof_dir,
                version,
                built_at,
                wheel_sha256,
                wheel_size_bytes,
                doctor_exit_code,
                smoke_exit_code,
                smoke_latency_ms,
                signed,
                all_claims,
            )

        # Step 5: Smoke.
        smoke_exit_code, smoke_latency_ms, smoke_claims = step_smoke(venv_dir)
        all_claims.extend(smoke_claims)
        if smoke_exit_code != 0:
            logger.error("HALT: smoke request failed (exit %d)", smoke_exit_code)
            return _write_proof(
                proof_dir,
                version,
                built_at,
                wheel_sha256,
                wheel_size_bytes,
                doctor_exit_code,
                smoke_exit_code,
                smoke_latency_ms,
                signed,
                all_claims,
            )

    # Step 6: Sign then write proof ONCE.
    # Collect the signing result before writing so the final proof.json reflects
    # the definitive signed flag and includes the signature claim -- eliminating
    # the intermediate write that previously wrote a signed=False copy.
    proof_path = proof_dir / "proof.json"

    # Build a preliminary proof JSON for GPG to sign (same content as the final
    # write except signed=False, which is the correct state at sign-time).
    preliminary_proof = _build_proof(
        version,
        built_at,
        wheel_sha256,
        wheel_size_bytes,
        doctor_exit_code,
        smoke_exit_code,
        smoke_latency_ms,
        signed=False,
        claims=all_claims,
    )
    signed, sign_claims = step_sign(preliminary_proof.to_json(), proof_path, signing_key)
    all_claims.extend(sign_claims)

    # Single definitive write: includes the signature claim and final signed flag.
    proof = _write_proof(
        proof_dir,
        version,
        built_at,
        wheel_sha256,
        wheel_size_bytes,
        doctor_exit_code,
        smoke_exit_code,
        smoke_latency_ms,
        signed,
        all_claims,
    )
    logger.info("Proof written: %s", proof_path)
    return proof


def _build_proof(
    version: str,
    built_at: str,
    wheel_sha256: str,
    wheel_size_bytes: int,
    doctor_exit_code: int,
    smoke_exit_code: int,
    smoke_latency_ms: float,
    signed: bool,
    claims: list[ReleaseClaimRecord],
) -> ReleaseProof:
    """Construct a ``ReleaseProof`` without writing it to disk.

    Args:
        version: Release version string.
        built_at: ISO-8601 UTC build timestamp.
        wheel_sha256: SHA-256 hex digest of the wheel.
        wheel_size_bytes: Wheel file size in bytes.
        doctor_exit_code: Exit code from ``vetinari --doctor``.
        smoke_exit_code: Exit code from smoke request.
        smoke_latency_ms: Smoke request latency in milliseconds.
        signed: Whether the proof was GPG-signed.
        claims: Ordered list of verified claim records.

    Returns:
        A ``ReleaseProof`` instance (not yet persisted).
    """
    return ReleaseProof(
        schema_version=PROOF_SCHEMA_VERSION,
        version=version,
        built_at=built_at,
        python_version=platform.python_version(),
        host_os=platform.platform(),
        wheel_sha256=wheel_sha256,
        wheel_size_bytes=wheel_size_bytes,
        doctor_exit_code=doctor_exit_code,
        smoke_exit_code=smoke_exit_code,
        smoke_latency_ms=smoke_latency_ms,
        signed=signed,
        claims=tuple(claims),
    )


def _write_proof(
    proof_dir: Path,
    version: str,
    built_at: str,
    wheel_sha256: str,
    wheel_size_bytes: int,
    doctor_exit_code: int,
    smoke_exit_code: int,
    smoke_latency_ms: float,
    signed: bool,
    claims: list[ReleaseClaimRecord],
) -> ReleaseProof:
    """Construct and write the proof artifact to disk.

    Args:
        proof_dir: Directory to write ``proof.json`` into.
        version: Release version string.
        built_at: ISO-8601 UTC build timestamp.
        wheel_sha256: SHA-256 hex digest of the wheel.
        wheel_size_bytes: Wheel file size in bytes.
        doctor_exit_code: Exit code from ``vetinari --doctor``.
        smoke_exit_code: Exit code from smoke request.
        smoke_latency_ms: Smoke request latency in milliseconds.
        signed: Whether the proof was GPG-signed.
        claims: Ordered list of verified claim records.

    Returns:
        The ``ReleaseProof`` instance written to disk.
    """
    proof = _build_proof(
        version,
        built_at,
        wheel_sha256,
        wheel_size_bytes,
        doctor_exit_code,
        smoke_exit_code,
        smoke_latency_ms,
        signed,
        claims,
    )
    proof_path = proof_dir / "proof.json"
    proof_path.write_text(proof.to_json(), encoding="utf-8")
    return proof


# ── CLI entry point ────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the release doctor.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Parsed namespace with ``version``, ``quiet``, ``signing_key`` fields.
    """
    parser = argparse.ArgumentParser(
        description="Vetinari release doctor -- build, install, verify, and sign a release.",
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Release version string to embed in the proof (e.g. '0.9.0' or 'dev').",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress INFO-level output.",
    )
    parser.add_argument(
        "--signing-key",
        dest="signing_key",
        default=None,
        help="Path to GPG key for signing the proof artifact (optional).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python scripts/release_doctor.py``.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Exit code: 0 if all steps passed, 1 if any failed, 2 for usage errors.
    """
    args = _parse_args(argv)
    signing_key = Path(args.signing_key) if args.signing_key else None

    try:
        proof = run_release_doctor(
            version=args.version,
            quiet=args.quiet,
            signing_key=signing_key,
        )
    except Exception as exc:
        logger.exception(
            "Release doctor encountered an unexpected error -- cannot produce proof: %s",
            exc,
        )
        return 2

    overall_passed = proof.doctor_exit_code == 0 and proof.smoke_exit_code == 0

    # Emit a RELEASE_STEP receipt so the Control Center reflects pipeline
    # outcome without polling proof.json. Failures inside the helper are
    # logged and never crash the release doctor.
    try:
        from vetinari.receipts import record_release_step

        proof_path = _OUTPUTS_DIR / proof.version / "proof.json"
        record_release_step(
            project_id="release",
            version=proof.version,
            step_name="run_release_doctor",
            success=overall_passed,
            proof_path=proof_path,
            linked_claim_ids=tuple(getattr(c, "id", "") for c in proof.claims if getattr(c, "id", "")),
            error="" if overall_passed else f"doctor={proof.doctor_exit_code} smoke={proof.smoke_exit_code}",
        )
    except Exception:
        logger.warning("Failed to emit RELEASE_STEP receipt", exc_info=True)

    if overall_passed:
        print(f"RELEASE DOCTOR PASSED  version={proof.version}  wheel_sha256={proof.wheel_sha256[:16]}...")
        return 0
    else:
        print(
            f"RELEASE DOCTOR FAILED  doctor_exit={proof.doctor_exit_code}  smoke_exit={proof.smoke_exit_code}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
