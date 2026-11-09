import os
import shutil
import stat
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import IO

from docmost_to_claude_project_converter.exceptions import (
    SessionArchiveError,
    SessionInUseError,
    SessionNotFoundError,
)

# Chromium creates these while it runs. Restoring them would make the next launch think the profile is in use.
_BROWSER_LOCK_FILE_NAMES = frozenset({"SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"})


@contextmanager
def open_profile(session_path: Path, *, create_if_missing: bool = False) -> Generator[Path]:
    with _lock_session(session_path), TemporaryDirectory(prefix="docmost-claude-profile-") as temporary_directory:
        profile_directory = Path(temporary_directory)

        if session_path.is_file():
            _unzip_session_archive(session_path, profile_directory)
        elif session_path.exists():
            raise SessionArchiveError("The session path must be a .profile archive file, not a directory.")
        elif not create_if_missing:
            raise SessionNotFoundError(session_path)

        yield profile_directory


def save_profile(session_path: Path, profile_directory: Path) -> None:
    # Write next to the archive and swap it in, so a failed save never leaves a half-written session behind.
    temporary_file = NamedTemporaryFile(
        dir=session_path.parent,
        prefix=f".{session_path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary_path = Path(temporary_file.name)

    try:
        with temporary_file:
            _zip_profile_directory(profile_directory, temporary_file)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        temporary_path.replace(session_path)
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _lock_session(session_path: Path) -> Generator[None]:
    session_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = session_path.with_name(f".{session_path.name}.lock")

    try:
        lock_file_descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise SessionInUseError(lock_path) from error

    try:
        with os.fdopen(lock_file_descriptor, "w") as lock_file:
            lock_file.write(str(os.getpid()))

        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _unzip_session_archive(session_path: Path, profile_directory: Path) -> None:
    try:
        with zipfile.ZipFile(session_path) as archive:
            for entry in archive.infolist():
                _unzip_session_archive_entry(archive, entry, profile_directory)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        raise SessionArchiveError(
            "Could not safely unpack the session archive. The original archive was not changed."
        ) from error


def _unzip_session_archive_entry(archive: zipfile.ZipFile, entry: zipfile.ZipInfo, profile_directory: Path) -> None:
    if _is_unsafe_archive_entry(entry):
        raise SessionArchiveError("The session archive contains an unsafe path or symbolic link.")

    destination = profile_directory.joinpath(*PurePosixPath(entry.filename).parts)
    if entry.is_dir():
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        return

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    file_descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(file_descriptor, "wb") as target, archive.open(entry) as source:
        shutil.copyfileobj(source, target)


def _is_unsafe_archive_entry(entry: zipfile.ZipInfo) -> bool:
    entry_path = PurePosixPath(entry.filename)

    is_empty = not entry_path.parts
    is_absolute = entry_path.is_absolute()
    is_outside_profile = ".." in entry_path.parts
    has_backslash = "\\" in entry.filename
    # A drive letter like C:/escape would be absolute on Windows.
    has_drive_letter = not is_empty and ":" in entry_path.parts[0]
    is_symbolic_link = stat.S_ISLNK(entry.external_attr >> 16)

    return is_empty or is_absolute or is_outside_profile or has_backslash or has_drive_letter or is_symbolic_link


def _zip_profile_directory(profile_directory: Path, archive_file: IO[bytes]) -> None:
    with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        for file in sorted(profile_directory.rglob("*")):
            relative_path = file.relative_to(profile_directory)

            is_regular_file = file.is_file() and not file.is_symlink()
            is_browser_lock_file = relative_path.parts[0] in _BROWSER_LOCK_FILE_NAMES

            if is_regular_file and not is_browser_lock_file:
                archive.write(file, relative_path.as_posix())
