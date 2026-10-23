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
import threading
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


def _format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1048576:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


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


def _content_length(response) -> int:
    try:
        value = response.headers.get("Content-Length")
    except AttributeError:
        return 0
    if not value:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _download(
    url: str, destination: Path, progress: "Progress | None" = None
) -> None:
    total = 0
    last_draw = 0.0
    with _open(url, DOWNLOAD_TIMEOUT) as response, destination.open("wb") as handle:
        expected = _content_length(response)
        if progress is not None and expected:
            progress.set_determinate()
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise UpdateError("the update archive is unexpectedly large")
            handle.write(chunk)
            if progress is not None:
                now = time.monotonic()
                if now - last_draw >= 0.05:
                    if expected:
                        progress.update(
                            total / expected,
                            f"{_format_size(total)}/{_format_size(expected)}",
                        )
                    else:
                        progress.set_detail(_format_size(total))
                    last_draw = now
    if total == 0:
        raise UpdateError("the update archive was empty")
    if progress is not None:
        # Only record the final figure: finish_spin() draws the 100% line, so
        # redrawing here would leave the same step on screen twice.
        if expected:
            progress.set_detail(
                f"{_format_size(total)}/{_format_size(expected)}"
            )
        else:
            progress.set_detail(_format_size(total))


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


class Progress:
    """ASCII progress that reports the work actually finished.

    A terminal gets a redrawn bar or a spinner; a pipe gets one line per
    completed step, so a redirected run stays readable. No step invents a
    percentage: a determinate step divides by bytes or files it knows about,
    and a step whose total cannot be known (the archive size is not advertised
    by GitHub) shows a spinner with the live byte count instead.
    """

    BAR = 20
    FRAMES = ("|", "/", "-", "\\")

    def __init__(self) -> None:
        self.live = is_interactive()
        self.index = 0
        self.total = 0
        self.label = ""
        self.detail = ""
        self.started = time.monotonic()
        self._width = 0
        self._last_draw = 0.0
        self._determinate = False
        self._lock = threading.Lock()
        self._spinner_stop: threading.Event | None = None
        self._spinner_thread: threading.Thread | None = None

    # -- drawing ---------------------------------------------------------
    def _write(self, text: str) -> None:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except (AttributeError, OSError, ValueError):
            pass

    def _prefix(self) -> str:
        return f"[{self.index}/{self.total}] " if self.total else ""

    def _draw(self, text: str) -> None:
        with self._lock:
            pad = max(0, self._width - len(text))
            self._write("\r" + text + " " * pad)
            self._width = len(text)

    def _end_line(self, text: str) -> None:
        with self._lock:
            pad = max(0, self._width - len(text))
            self._write("\r" + text + " " * pad + "\n")
            self._width = 0

    def _clear(self) -> None:
        with self._lock:
            if self._width:
                self._write("\r" + " " * self._width + "\r")
                self._width = 0

    def _elapsed(self) -> float:
        return time.monotonic() - self.started

    def _render(self, fraction: float) -> str:
        fraction = min(1.0, max(0.0, fraction))
        filled = int(fraction * self.BAR + 0.5)
        bar = "#" * filled + "-" * (self.BAR - filled)
        detail = self.detail[:16]
        return (
            f"{self._prefix()}{self.label[:20]:<20} [{bar}] "
            f"{fraction * 100:3.0f}% {detail:<16} {self._elapsed():5.1f}s"
        )

    # -- indefinite steps -------------------------------------------------
    def _spin(self, stop: threading.Event) -> None:
        frame = 0
        while not stop.wait(0.1):
            detail = f" {self.detail}" if self.detail else ""
            self._draw(
                f"{self._prefix()}{self.label[:20]:<20}{detail} "
                f"{self.FRAMES[frame % len(self.FRAMES)]} "
                f"{self._elapsed():5.1f}s"
            )
            frame += 1

    def _stop_spinner(self) -> None:
        if self._spinner_stop is not None:
            self._spinner_stop.set()
        if self._spinner_thread is not None:
            self._spinner_thread.join(timeout=1.0)
        self._spinner_stop = None
        self._spinner_thread = None

    def set_detail(self, detail: str) -> None:
        """Update the live counter of an indefinite step."""
        self.detail = detail

    def set_determinate(self) -> None:
        """A step that started unknown now has a real denominator."""
        self._stop_spinner()
        self._determinate = True
        if self.live:
            self._draw(self._render(0.0))

    # -- public steps -----------------------------------------------------
    def begin(self, index: int, total: int, label: str) -> None:
        self._stop_spinner()
        self.index, self.total, self.label = index, total, label
        self.detail = ""
        self.started = time.monotonic()
        self._last_draw = 0.0
        self._determinate = True
        if self.live:
            self._draw(self._render(0.0))

    def begin_spin(self, index: int, total: int, label: str) -> None:
        self._stop_spinner()
        self.index, self.total, self.label = index, total, label
        self.detail = ""
        self.started = time.monotonic()
        self._determinate = False
        if not self.live:
            return
        self._spinner_stop = threading.Event()
        self._spinner_thread = threading.Thread(
            target=self._spin, args=(self._spinner_stop,), daemon=True
        )
        self._spinner_thread.start()

    def update(self, fraction: float, detail: str = "") -> None:
        self.detail = detail
        # finish() owns the completed line, so the last update only records
        # its detail instead of painting a second 100% line.
        if not self.live or not self._determinate or fraction >= 1.0:
            return
        now = time.monotonic()
        if now - self._last_draw < 0.05:
            return
        self._last_draw = now
        self._draw(self._render(fraction))

    def finish(self, detail: str | None = None) -> None:
        if detail is not None:
            self.detail = detail
        if not self.live:
            self._log_done(self.detail)
            return
        self._last_draw = 0.0
        self._end_line(self._render(1.0))

    def finish_spin(self, detail: str | None = None) -> None:
        self._stop_spinner()
        if detail is not None:
            self.detail = detail
        if self._determinate:
            self.finish()
            return
        if not self.live:
            self._log_done(self.detail)
            return
        text = (
            f"{self._prefix()}{self.label[:20]:<20} done "
            f"{self._elapsed():5.1f}s"
        )
        if self.detail:
            text += f" ({self.detail})"
        self._end_line(text)

    def _log_done(self, detail: str = "") -> None:
        extra = f" ({detail})" if detail else ""
        say(
            f"{self._prefix()}{self.label} done in "
            f"{self._elapsed():.1f}s{extra}"
        )

    def stop(self) -> None:
        """End the check spinner without claiming a step completed."""
        self._stop_spinner()
        if self.live:
            self._clear()

    def abort(self) -> None:
        """Clear a failed step; the caller prints the error."""
        self._stop_spinner()
        if self.live:
            self._clear()


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


def _stage_archive(
    zip_path: Path, staging: Path, progress: "Progress | None" = None
) -> str:
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError(f"the update archive could not be opened: {exc}") from exc

    with archive:
        top, members = _validate_members(archive)
        files = [member for member in members if not member.is_dir()]
        # read() verifies the member's CRC, so a damaged entry is named here
        # without a second full pass over the archive. Sizes are the
        # compressed bytes actually read, which is the work this step does.
        total = sum(member.compress_size for member in files)
        done = 0
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
            done += member.compress_size
            if progress is not None:
                fraction = done / total if total else 1.0
                progress.update(
                    fraction,
                    f"{_format_size(done)}/{_format_size(total)}",
                )

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


def _apply_staging(
    staging: Path, progress: "Progress | None" = None
) -> None:
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

    backup_sizes: dict[str, int] = {}
    for relative in overwritten:
        try:
            backup_sizes[relative] = (ROOT / relative).stat().st_size
        except OSError:
            backup_sizes[relative] = 0
    write_total = 0
    for source, _ in plans:
        try:
            write_total += source.stat().st_size
        except OSError:
            pass
    total_work = sum(backup_sizes.values()) + write_total
    total_files = len(overwritten) + len(plans)
    done_work = 0
    done_files = 0

    def report() -> None:
        if progress is None:
            return
        if total_work:
            progress.update(
                done_work / total_work,
                f"{_format_size(done_work)}/{_format_size(total_work)}",
            )
        else:
            progress.update(
                done_files / total_files if total_files else 1.0,
                f"{done_files}/{total_files} files",
            )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    backup = STATE_DIR / f"backup-{int(time.time())}-{os.getpid()}"
    try:
        backup.mkdir(parents=True, exist_ok=True)
        for relative in overwritten:
            source = ROOT / relative
            destination = backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            done_work += backup_sizes[relative]
            done_files += 1
            report()

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
            done_work += len(data)
            done_files += 1
            report()
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


def _sync_requirements(before: str) -> tuple[bool, str]:
    """Install changed requirements. Returns (ok, what happened)."""
    after = _requirements_text()
    if before == after:
        return True, "no changes"
    if os.name == "nt":
        python = ROOT / "scripts/.venv/Scripts/python.exe"
    else:
        python = ROOT / "scripts/.venv/bin/python"
    if not python.is_file():
        return True, "no venv yet"
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
        return False, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else "pip failed"
    return True, "updated"


def apply_zip(result: Result, progress: Progress | None = None) -> str:
    progress = progress or Progress()
    before = _requirements_text()
    with tempfile.TemporaryDirectory(prefix="carutil-update-") as temporary:
        root = Path(temporary)
        archive = root / "update.zip"
        staging = root / "staging"
        staging.mkdir()
        progress.begin_spin(1, 4, "Downloading archive")
        _download(ZIP_URL, archive, progress)
        progress.finish_spin()
        progress.begin(2, 4, "Verifying archive")
        _stage_archive(archive, staging, progress)
        progress.finish()
        version = _stage_version(staging, result.remote)
        progress.begin(3, 4, "Installing files")
        _apply_staging(staging, progress)
        progress.finish()
    progress.begin_spin(4, 4, "Python packages")
    ok, detail = _sync_requirements(before)
    progress.finish_spin(detail)
    if not ok:
        say(f"Warning: Python packages were not updated: {detail}")
    _clean_bytecode()
    return version


def apply_git_update(progress: Progress | None = None) -> str:
    progress = progress or Progress()
    before = _requirements_text()
    progress.begin_spin(1, 2, "Applying update")
    version = apply_git()
    progress.finish_spin(version)
    progress.begin_spin(2, 2, "Python packages")
    ok, detail = _sync_requirements(before)
    progress.finish_spin(detail)
    if not ok:
        say(f"Warning: Python packages were not updated: {detail}")
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


def apply_update(result: Result, progress: Progress | None = None) -> str:
    if (ROOT / ".git").exists():
        return apply_git_update(progress)
    return apply_zip(result, progress)


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

    checker = Progress()
    checker.begin_spin(0, 0, "Checking for updates")
    try:
        result = check_update()
    except OfflineError:
        checker.stop()
        say("No internet connection; could not check for updates.")
        return EXIT_OFFLINE if args.check else EXIT_CURRENT
    except UpdateError as exc:
        checker.stop()
        say(f"Could not check for updates: {exc}")
        return EXIT_FAILED if args.check else EXIT_CURRENT
    except Exception as exc:
        checker.stop()
        say(f"Could not check for updates: {exc}")
        return EXIT_FAILED if args.check else EXIT_CURRENT
    checker.stop()

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

    updater = Progress()
    try:
        version = apply_update(result, updater)
    except OfflineError:
        updater.abort()
        say("No internet connection; the update was not installed.")
        return EXIT_CURRENT
    except (UpdateError, OSError) as exc:
        updater.abort()
        say(f"Update failed: {exc}")
        say("CarUtil will continue with the current version.")
        return EXIT_CURRENT
    except Exception as exc:
        updater.abort()
        say(f"Update failed: {exc}")
        say("CarUtil will continue with the current version.")
        return EXIT_CURRENT

    say(f"Updated to v{version}.")
    return EXIT_CURRENT


if __name__ == "__main__":
    raise SystemExit(main())
