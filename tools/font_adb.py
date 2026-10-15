"""Conservative, opt-in replacement of an existing external Android bundle.

This module never discovers a cache path or starts the game.  The caller must
provide a verified path and the SHA256 of the file that is allowed to be
replaced.  A dry run performs no ADB calls and creates no directories.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from datetime import datetime, timezone
import uuid


class FontDeploymentError(RuntimeError):
    """A deployment failed; a completed local backup is never removed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_remote(package: str, remote_path: str) -> None:
    if not isinstance(package, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package
    ):
        raise ValueError("package must be an Android application ID")
    prefix = f"/sdcard/Android/data/{package}/files/"
    if (
        not isinstance(remote_path, str)
        or not remote_path.startswith(prefix)
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote_path)
        or any(part in ("", ".", "..") for part in remote_path.split("/")[1:])
    ):
        raise ValueError(
            "remote_path must name a file below " + prefix
            + " using only letters, digits, '.', '_', '-', and '/'"
        )


def deploy_bundle(
    adb: str | os.PathLike[str],
    serial: str | None,
    package: str,
    remote_path: str,
    bundle_path: str | os.PathLike[str],
    original_sha256: str,
    backup_dir: str | os.PathLike[str],
    apply: bool = False,
    restore: bool = False,
) -> dict:
    """Return a dry-run plan, or back up and replace one existing bundle.

    ``apply=True`` requires an explicit device serial.  ``original_sha256`` is
    always the expected hash of the CURRENT remote file.  With ``restore=True``,
    ``bundle_path`` names a saved original bundle and ``original_sha256`` must
    therefore be the known hash of the currently installed replacement.

    Every ADB call is pinned with ``-s`` and has a timeout.  Device shell access
    remains at normal ADB privileges; there is no root, run-as, APK installation,
    permission bypass, or claim that a particular game loads the given file.
    A failure after the rename may mean that replacement succeeded but the
    final verification could not complete.  The exception describes this case
    and includes the retained backup path.
    """
    _validate_remote(package, remote_path)
    if not isinstance(original_sha256, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", original_sha256
    ):
        raise ValueError("original_sha256 must contain exactly 64 hex digits")
    expected_hash = original_sha256.lower()
    if serial == "":
        serial = None
    if serial is not None and (
        not isinstance(serial, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", serial)
    ):
        raise ValueError("serial contains unsupported characters")
    if apply and not serial:
        raise ValueError("--apply requires an explicit ADB device serial")
    adb_command = os.fspath(adb)
    if not adb_command or "\x00" in adb_command:
        raise ValueError("adb must name the ADB executable")
    source = Path(bundle_path).resolve(strict=True)
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError("bundle_path must be a non-empty local file")
    replacement_hash = _sha256(source)
    backup_root = Path(backup_dir).resolve()
    plan = {
        "status": "dry-run",
        "operation": "restore" if restore else "deploy",
        "serial": serial,
        "package": package,
        "remote_path": remote_path,
        "bundle_path": str(source),
        "original_sha256": expected_hash,
        "replacement_sha256": replacement_hash,
        "backup_dir": str(backup_root),
        "steps": [
            "Verify the explicitly selected device and existing remote SHA256",
            "Force-stop the package and recheck the remote SHA256",
            "Pull a uniquely named local backup and verify its SHA256",
            "Push a temporary file in the target directory and verify SHA256",
            "Recheck the current target SHA256 and rename the temporary file",
            "Verify the final SHA256; leave the game stopped",
        ],
    }
    if not apply:
        return plan

    def run(arguments: list[str], timeout: int = 120) -> str:
        command = [adb_command, "-s", serial, *arguments]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            raise FontDeploymentError("ADB command failed: " + detail.strip()) from exc
        return result.stdout.strip()

    def shell(command: str) -> str:
        return run(["shell", command], timeout=30)

    def remote_sha256(path: str) -> str:
        quoted = shlex.quote(path)
        output = shell(
            f"if [ -f {quoted} ] && [ ! -L {quoted} ]; then "
            f"sha256sum {quoted}; else "
            "echo 'Expected an existing regular file' >&2; exit 71; fi"
        )
        fields = output.split()
        if not fields or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
            raise FontDeploymentError("Device returned an invalid SHA256 result")
        return fields[0].lower()

    def require_original() -> None:
        actual = remote_sha256(remote_path)
        if actual != expected_hash:
            raise FontDeploymentError(
                f"Remote SHA256 mismatch: expected {expected_hash}, found {actual}"
            )

    identifier = uuid.uuid4().hex
    parent, filename = remote_path.rsplit("/", 1)
    remote_temp = f"{parent}/.{filename}.fontpatch-{identifier}.tmp"
    backup = None
    temp_may_exist = False
    committed = False
    commit_attempted = False
    try:
        if run(["get-state"], timeout=15) != "device":
            raise FontDeploymentError("The selected ADB device is not ready")
        if shell("id -u") != "2000":
            raise FontDeploymentError("Deployment requires the normal ADB shell UID 2000")
        require_original()
        shell("am force-stop " + shlex.quote(package))
        require_original()
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_root / (
            f"{package}.{filename}.{stamp}.{identifier}.backup"
        )
        # Exclusive reservation prevents overwriting an older local backup.
        with backup.open("xb"):
            pass
        run(["pull", remote_path, str(backup)])
        if _sha256(backup) != expected_hash:
            raise FontDeploymentError("The pulled backup does not match the expected SHA256")
        metadata = {
            **plan,
            "status": "backup-created",
            "backup_path": str(backup),
            "backup_sha256": expected_hash,
            "created_utc": stamp,
        }
        with Path(str(backup) + ".json").open("x", encoding="utf-8") as stream:
            json.dump(metadata, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        quoted_temp = shlex.quote(remote_temp)
        shell(
            f"if [ -e {quoted_temp} ] || [ -L {quoted_temp} ]; then "
            "echo 'Temporary destination already exists' >&2; exit 72; fi"
        )
        temp_may_exist = True
        run(["push", str(source), remote_temp])
        if remote_sha256(remote_temp) != replacement_hash:
            raise FontDeploymentError("The uploaded temporary bundle failed SHA256 verification")
        quoted_target = shlex.quote(remote_path)
        # Keep the final hash check and same-directory rename in one shell call.
        commit_attempted = True
        shell(
            f"[ -f {quoted_target} ] && [ ! -L {quoted_target} ] || exit 73; "
            f"actual=$(sha256sum {quoted_target}) || exit 74; "
            'actual=${actual%% *}; '
            f'[ "$actual" = {shlex.quote(expected_hash)} ] || exit 75; '
            f"mv -f {quoted_temp} {quoted_target}"
        )
        committed = True
        temp_may_exist = False
        if remote_sha256(remote_path) != replacement_hash:
            raise FontDeploymentError("Final remote SHA256 differs from the replacement")
        return {
            **plan,
            "status": "restored" if restore else "deployed",
            "backup_path": str(backup),
            "backup_sha256": expected_hash,
        }
    except Exception as exc:
        cleanup_note = ""
        if temp_may_exist:
            try:
                shell("rm -f " + shlex.quote(remote_temp))
            except Exception:
                cleanup_note = " Temporary cleanup failed; inspect " + remote_temp + "."
        backup_note = ""
        if backup is not None:
            backup_note = " Retained local backup (verify before restoring): " + str(backup) + "."
        commit_note = ""
        if committed or commit_attempted:
            commit_note = (
                " The final rename was attempted; verify the remote SHA256 before retrying."
            )
        raise FontDeploymentError(
            str(exc) + backup_note + commit_note + cleanup_note
        ) from exc
