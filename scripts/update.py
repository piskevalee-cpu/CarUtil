#!/usr/bin/env python3
"""Check for and install CarUtil updates.

This runs before the private virtual environment exists, so it uses only the
standard library and Python 3.9 syntax. It must never prevent a conversion:
``--auto`` always exits 0 and the launcher carries on with whatever version is
on disk.

A Git clone is updated with ``git merge --ff-only`` after refusing a dirty
working tree. A ZIP install is updated by downloading the branch archive,
validating it, and overlaying it file by file; ``.git``, ``.venv`` and custom
logo files are preserved, and every overwritten file is backed up so a failed
apply can be rolled back.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".carutil-update"
MARKER = STATE_DIR / "in-progress.json"

DEFAULT_REPO = "piskevalee-cpu/CarUtil"
DEFAULT_BRANCH = "main"
REPO = os.environ.get("CARUTIL_UPDATE_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO
BRANCH = os.environ.get("CARUTIL_UPDATE_BRANCH", DEFAULT_BRANCH).strip() or DEFAULT_BRANCH

RAW_VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"

HTTP_TIMEOUT = 5.0
GIT_TIMEOUT = 20.0
DOWNLOAD_TIMEOUT = 60.0
MAX_DOWNLOAD = 200 * 1024 * 1024
MAX_UNPACKED = 300 * 1024 * 1024

EXIT_CURRENT = 0
EXIT_AVAILABLE = 10
EXIT_OFFLINE = 20
EXIT_FAILED = 30

VERSION_RE = re.compile(r"^v?([0-9]+)\.([0-9])\.([0-9])$")

REQUIRED = (
    "VERSION",
    "carutil.sh",
    "carutil.bat",
    "scripts/carutil.py",
    "scripts/update.py",
    "scripts/backend/sii.py",
    "scripts/requirements.txt",
)


class OfflineError(Exception):
    """The network could not be reached."""


class UpdateError(Exception):
    """The update could not be checked or applied."""


@dataclass
class Result:
    status: str                 # current, available, skipped
    local: str = "unknown"
    remote: str = ""
    detail: str = ""


def say(message: str = "") -> None:
    """Print a line that a narrow Windows console can encode."""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(encoding, "backslashreplace").decode(encoding))


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """Parse the repository's MAJOR.MINOR.PATCH form, or return None."""
    match = VERSION_RE.fullmatch((text or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def read_local_version() -> str | None:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _open(url: str, timeout: float):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "CarUtil-updater"},
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout,
            ConnectionError, OSError) as exc:
        raise OfflineError(str(exc)) from exc


def fetch_text(url: str, timeout: float = HTTP_TIMEOUT, limit: int = 4096) -> str:
    with _open(url, timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError(f"response from {url} is too large")
    return data.decode("utf-8", "strict")


def _download(url: str, destination: Path) -> None:
    total = 0
    with _open(url, DOWNLOAD_TIMEOUT) as response, destination.open("wb") as handle:
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise UpdateError("the update archive is unexpectedly large")
            handle.write(chunk)
    if total == 0:
        raise UpdateError("the update archive was empty")


def is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError):
        return False


def _confirm(default_yes: bool = True) -> bool:
    suffix = " [Y/n] " if default_yes else " [y/N] "
    try:
        answer = input("Install it now?" + suffix)
    except (EOFError, KeyboardInterrupt):
        say()
        return False
    answer = answer.strip().lower()
    if not answer:
        return default_yes
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# Git clones
# ---------------------------------------------------------------------------

def _git(args: list[str], timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT)] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OfflineError(f"git timed out: {exc}") from exc
    except OSError as exc:
        raise UpdateError(f"git could not be run: {exc}") from exc


def _git_out(args: list[str], timeout: float = GIT_TIMEOUT) -> str:
    proc = _git(args, timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise UpdateError(detail or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def _git_ok(args: list[str], timeout: float = GIT_TIMEOUT) -> bool:
    return _git(args, timeout).returncode == 0


def _git_offline(detail: str) -> bool:
    """Distinguish a missing network from a bad remote or credentials."""
    text = detail.lower()
    return any(marker in text for marker in (
        "could not resolve host",
        "could not resolve proxy",
        "temporary failure in name resolution",
        "name or service not known",
        "network is unreachable",
        "no route to host",
        "connection timed out",
        "connection refused",
        "connection reset",
        "connection closed",
        "failed to connect",
        "operation timed out",
    ))


def _git_failure(proc: subprocess.CompletedProcess, action: str) -> Exception:
    detail = (proc.stderr or proc.stdout or "").strip()
    if _git_offline(detail):
        return OfflineError(detail or f"{action} failed")
    return UpdateError(detail or f"{action} failed")


def _remote_git_version() -> str:
    try:
        return _git_out(["show", f"origin/{BRANCH}:VERSION"], 10).strip()
    except UpdateError:
        return ""


def check_git() -> Result:
    local_version = read_local_version() or "unknown"

    try:
        top = Path(_git_out(["rev-parse", "--show-toplevel"], 10)).resolve()
    except (UpdateError, OSError) as exc:
        return Result("skipped", local_version, detail=str(exc))
    if os.path.normcase(str(top)) != os.path.normcase(str(ROOT)):
        return Result(
            "skipped", local_version,
            detail="CarUtil is inside another Git repository",
        )

    branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], 10)
    if branch.returncode != 0 or branch.stdout.strip() != BRANCH:
        found = branch.stdout.strip() or "a detached HEAD"
        return Result(
            "skipped", local_version,
            detail=f"only the {BRANCH} branch updates automatically (on {found})",
        )

    if _git(["config", "--get", "remote.origin.url"], 10).returncode != 0:
        return Result("skipped", local_version, detail="no origin remote is configured")

    status = _git(["status", "--porcelain", "--untracked-files=no"], 15)
    if status.returncode != 0:
        return Result("skipped", local_version, detail="Git status could not be read")
    if status.stdout.strip():
        return Result(
            "skipped", local_version,
            detail="the Git working tree has local changes",
        )

    local_sha = _git_out(["rev-parse", "HEAD"], 10)
    remote = _git(["ls-remote", "--heads", "origin", BRANCH], 15)
    if remote.returncode != 0:
        raise _git_failure(remote, "git ls-remote")
    remote_sha = remote.stdout.split()[0] if remote.stdout.split() else ""
    if not remote_sha:
        raise UpdateError(f"origin has no {BRANCH} branch")
    if remote_sha == local_sha:
        return Result("current", local_version)

    fetch = _git(["fetch", "--quiet", "origin", BRANCH], 45)
    if fetch.returncode != 0:
        raise _git_failure(fetch, "git fetch")

    remote_ref = f"origin/{BRANCH}"
    if _git_ok(["merge-base", "--is-ancestor", "HEAD", remote_ref], 10):
        remote_version = _remote_git_version() or remote_sha[:12]
        return Result("available", local_version, remote_version)
    if _git_ok(["merge-base", "--is-ancestor", remote_ref, "HEAD"], 10):
        return Result(
            "skipped", local_version,
            detail="the local branch has commits that are not on origin",
        )
    return Result(
        "skipped", local_version,
        detail="the local branch and origin have diverged",
    )


def apply_git() -> str:
    proc = _git(["merge", "--ff-only", f"origin/{BRANCH}"], 120)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise UpdateError(detail or "git merge failed")
    return read_local_version() or "unknown"


# ---------------------------------------------------------------------------
# ZIP installs
# ---------------------------------------------------------------------------

def check_zip() -> Result:
    local = read_local_version()
    local_key = parse_version(local)
    if local and local_key is None:
        raise UpdateError(f"the installed VERSION is not valid: {local!r}")

    remote = fetch_text(RAW_VERSION_URL).strip()
    remote_key = parse_version(remote)
    if remote_key is None:
        raise UpdateError(f"the remote VERSION is not valid: {remote!r}")

    if local_key is not None and remote_key <= local_key:
        return Result("current", local or "unknown", remote)

    return Result("available", local or "unknown", remote)


def _validate_members(archive: zipfile.ZipFile) -> tuple[str, list[zipfile.ZipInfo]]:
    names: list[str] = []
    total = 0
    for member in archive.infolist():
        name = member.filename
        if not name:
            raise UpdateError("the update archive contains an unnamed entry")
        if "\0" in name:
            raise UpdateError(f"unsafe path in the update archive: {name!r}")
        if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
            raise UpdateError(f"unsafe path in the update archive: {name!r}")
        if "\\" in name:
            raise UpdateError(f"unsafe path in the update archive: {name!r}")
        path = PurePosixPath(name)
        if not path.parts or ".." in path.parts:
            raise UpdateError(f"unsafe path in the update archive: {name!r}")
        mode = (member.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise UpdateError(f"symlink in the update archive: {name!r}")
        if not member.is_dir():
            if len(path.parts) < 2:
                raise UpdateError(
                    "the update archive must have one top-level folder"
                )
            total += member.file_size
            if total > MAX_UNPACKED:
                raise UpdateError("the update archive expands to an unexpected size")
            names.append(name)

    tops = {PurePosixPath(name).parts[0] for name in names}
    if len(tops) != 1:
        raise UpdateError("the update archive must have one top-level folder")
    return tops.pop(), archive.infolist()


def _stage_archive(zip_path: Path, staging: Path) -> str:
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError(f"the update archive could not be opened: {exc}") from exc

    with archive:
        top, members = _validate_members(archive)
        # Validate the sizes before testzip() decompresses every member: a
        # small archive can otherwise expand without bound before the size
        # check in _validate_members is ever reached.
        bad = archive.testzip()
        if bad is not None:
            raise UpdateError(f"the update archive is damaged at {bad!r}")
        for member in members:
            rel = PurePosixPath(member.filename).relative_to(top)
            if member.is_dir():
                (staging / Path(*rel.parts)).mkdir(parents=True, exist_ok=True)
                continue
            target = staging / Path(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = archive.read(member)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise UpdateError(f"{member.filename}: {exc}") from exc
            suffix = target.suffix.lower()
            if suffix == ".bat":
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            elif suffix == ".sh":
                data = data.replace(b"\r\n", b"\n")
            target.write_bytes(data)

    for required in REQUIRED:
        if not (staging / required).is_file():
            raise UpdateError(f"the update archive is missing {required}")
    return top


def _stage_version(staging: Path, expected_remote: str) -> str:
    try:
        staged = (staging / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise UpdateError(f"the update archive has no readable VERSION: {exc}") from exc
    staged_key = parse_version(staged)
    if staged_key is None:
        raise UpdateError(f"the update archive VERSION is not valid: {staged!r}")

    local = read_local_version()
    local_key = parse_version(local)
    if local_key is not None and staged_key < local_key:
        raise UpdateError(
            f"the update archive is older ({staged}) than the install ({local})"
        )
    remote_key = parse_version(expected_remote)
    if remote_key is not None and staged_key < remote_key:
        raise UpdateError(
            f"the update archive is older ({staged}) than the advertised {expected_remote}"
        )
    return staged


def _preserve_logo(relative: Path) -> bool:
    """Leave an existing brand image alone; users replace these by name."""
    return (
        len(relative.parts) >= 2
        and relative.parts[0] == "scripts"
        and relative.parts[1] == "logos"
        and (ROOT / relative).is_file()
    )


def _safe_relative(name: str) -> bool:
    """True for a relative POSIX path that cannot escape ROOT."""
    if not name or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        return False
    path = PurePosixPath(name)
    return bool(path.parts) and ".." not in path.parts and "\\" not in name


def _clean_state_dir() -> None:
    """Remove the state folder once nothing is left to recover from."""
    try:
        STATE_DIR.rmdir()
    except OSError:
        pass


def _restore(overwritten: list[str], added: list[str], backup: Path) -> bool:
    """Put the old tree back. False means the backup must be kept."""
    restored = True
    for relative in reversed(added):
        if not _safe_relative(relative):
            continue
        try:
            (ROOT / relative).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            restored = False
    for relative in reversed(overwritten):
        if not _safe_relative(relative):
            continue
        source = backup / relative
        target = ROOT / relative
        if not source.is_file():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError:
            restored = False
    return restored


def recover_interrupted_update() -> None:
    if not MARKER.is_file():
        return
    try:
        payload = json.loads(MARKER.read_text(encoding="utf-8"))
        backup = Path(payload.get("backup", ""))
        overwritten = [str(name) for name in payload.get("overwritten", [])]
        added = [str(name) for name in payload.get("added", [])]
    except (OSError, ValueError, TypeError) as exc:
        say(f"Warning: an interrupted update could not be read: {exc}")
        try:
            MARKER.unlink()
        except OSError:
            pass
        _clean_state_dir()
        return

    state = STATE_DIR.resolve()
    try:
        inside_state = backup.is_absolute() and state in backup.resolve().parents
    except OSError:
        inside_state = False
    if not inside_state:
        say("Warning: an interrupted update pointed outside its backup folder.")
        try:
            MARKER.unlink()
        except OSError:
            pass
        _clean_state_dir()
        return

    if backup.is_dir():
        if not _restore(overwritten, added, backup):
            say("Warning: the previous files could not all be restored.")
            say(f"The backup is kept in {backup} and recovery will be retried.")
            return
        say("Recovered an interrupted update; the previous version is in place.")
    else:
        say("Warning: an interrupted update has no backup to restore.")
    try:
        MARKER.unlink()
    except OSError:
        pass
    shutil.rmtree(backup, ignore_errors=True)
    _clean_state_dir()


def _apply_staging(staging: Path) -> None:
    files = [path for path in staging.rglob("*") if path.is_file()]
    overwritten: list[str] = []
    added: list[str] = []
    plans: list[tuple[Path, Path]] = []

    for source in files:
        relative = source.relative_to(staging)
        if _preserve_logo(relative):
            continue
        target = ROOT / relative
        plans.append((source, target))
        if target.exists():
            overwritten.append(relative.as_posix())
        else:
            added.append(relative.as_posix())

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    backup = STATE_DIR / f"backup-{int(time.time())}-{os.getpid()}"
    try:
        backup.mkdir(parents=True, exist_ok=True)
        for relative in overwritten:
            source = ROOT / relative
            destination = backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        payload = {
            "backup": str(backup),
            "overwritten": overwritten,
            "added": added,
        }
        MARKER.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        shutil.rmtree(backup, ignore_errors=True)
        _clean_state_dir()
        raise UpdateError(f"the update could not be prepared: {exc}") from exc

    try:
        for source, target in plans:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(
                f"{target.name}.carutil-{os.getpid()}.tmp"
            )
            data = source.read_bytes()
            temporary.write_bytes(data)
            if target.suffix.lower() == ".sh":
                os.chmod(temporary, 0o755)
            else:
                os.chmod(temporary, 0o644)
            os.replace(temporary, target)
    except Exception as exc:
        if not _restore(overwritten, added, backup):
            say("Warning: the update failed and could not fully restore the old files.")
            say(f"The backup is kept in {backup} and recovery will be retried.")
            raise UpdateError(f"the update could not be applied: {exc}") from exc
        try:
            MARKER.unlink()
        except OSError:
            pass
        shutil.rmtree(backup, ignore_errors=True)
        _clean_state_dir()
        raise UpdateError(f"the update could not be applied: {exc}") from exc

    try:
        MARKER.unlink()
    except OSError:
        pass
    shutil.rmtree(backup, ignore_errors=True)
    _clean_state_dir()


def _clean_bytecode() -> None:
    for relative in ("scripts/__pycache__", "scripts/backend/__pycache__"):
        shutil.rmtree(ROOT / relative, ignore_errors=True)


def _requirements_text() -> str:
    try:
        return (ROOT / "scripts/requirements.txt").read_text(encoding="utf-8")
    except OSError:
        return ""


def _sync_requirements(before: str) -> None:
    after = _requirements_text()
    if before == after:
        return
    if os.name == "nt":
        python = ROOT / "scripts/.venv/Scripts/python.exe"
    else:
        python = ROOT / "scripts/.venv/bin/python"
    if not python.is_file():
        return
    say("Updating Python packages...")
    try:
        proc = subprocess.run(
            [str(python), "-m", "pip", "install", "-r",
             str(ROOT / "scripts/requirements.txt")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        say(f"Warning: Python packages were not updated: {exc}")
        return
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        say("Warning: Python packages were not updated"
            + (f": {detail[-1]}" if detail else "."))


def apply_zip(result: Result) -> str:
    before = _requirements_text()
    with tempfile.TemporaryDirectory(prefix="carutil-update-") as temporary:
        root = Path(temporary)
        archive = root / "update.zip"
        staging = root / "staging"
        staging.mkdir()
        _download(ZIP_URL, archive)
        _stage_archive(archive, staging)
        version = _stage_version(staging, result.remote)
        _apply_staging(staging)
    _sync_requirements(before)
    _clean_bytecode()
    return version


def apply_git_update() -> str:
    before = _requirements_text()
    version = apply_git()
    _sync_requirements(before)
    _clean_bytecode()
    return version


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def check_update() -> Result:
    if (ROOT / ".git").exists():
        if shutil.which("git") is None:
            return Result(
                "skipped", read_local_version() or "unknown",
                detail="Git is not installed; download a fresh ZIP instead",
            )
        return check_git()
    return check_zip()


def apply_update(result: Result) -> str:
    if (ROOT / ".git").exists():
        return apply_git_update()
    return apply_zip(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check for and install CarUtil updates."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--auto", action="store_true",
        help="check, ask, and install when a terminal is available (default)",
    )
    group.add_argument(
        "--check", action="store_true",
        help="report the result and exit; 10 means an update is available",
    )
    args = parser.parse_args(argv)

    try:
        recover_interrupted_update()
    except Exception as exc:  # recovery must not stop the launcher
        say(f"Warning: an interrupted update could not be recovered: {exc}")

    if not args.check and os.environ.get("CARUTIL_SKIP_UPDATE") == "1":
        return EXIT_CURRENT

    try:
        result = check_update()
    except OfflineError:
        say("No internet connection; could not check for updates.")
        return EXIT_OFFLINE if args.check else EXIT_CURRENT
    except UpdateError as exc:
        say(f"Could not check for updates: {exc}")
        return EXIT_FAILED if args.check else EXIT_CURRENT
    except Exception as exc:
        say(f"Could not check for updates: {exc}")
        return EXIT_FAILED if args.check else EXIT_CURRENT

    if result.status == "current":
        if args.check or result.local != "unknown":
            say(f"CarUtil is up to date (v{result.local}).")
        return EXIT_CURRENT
    if result.status == "skipped":
        say(f"Update check skipped: {result.detail}")
        return EXIT_CURRENT
    if result.status != "available":
        say(f"Could not check for updates: unexpected result {result.status!r}")
        return EXIT_FAILED if args.check else EXIT_CURRENT

    if parse_version(result.remote) is None:
        say(f"Update available: v{result.local} -> a newer commit ({result.remote})")
    else:
        say(f"Update available: v{result.local} -> v{result.remote}")
    if args.check:
        return EXIT_AVAILABLE
    if not is_interactive():
        say("Run CarUtil from a terminal to install it.")
        return EXIT_CURRENT
    if not _confirm(default_yes=True):
        say("Continuing without updating.")
        return EXIT_CURRENT

    try:
        version = apply_update(result)
    except OfflineError:
        say("No internet connection; the update was not installed.")
        return EXIT_CURRENT
    except (UpdateError, OSError) as exc:
        say(f"Update failed: {exc}")
        say("CarUtil will continue with the current version.")
        return EXIT_CURRENT
    except Exception as exc:
        say(f"Update failed: {exc}")
        say("CarUtil will continue with the current version.")
        return EXIT_CURRENT

    say(f"Updated to v{version}.")
    return EXIT_CURRENT


if __name__ == "__main__":
    raise SystemExit(main())
