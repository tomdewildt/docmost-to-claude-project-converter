import stat
import zipfile
from pathlib import Path

import pytest

from docmost_to_claude_project_converter.exceptions import SessionArchiveError, SessionInUseError, SessionNotFoundError
from docmost_to_claude_project_converter.profile import open_profile, save_profile


class TestProfileArchive:
    def test_save_and_open_profile_round_trip_through_a_private_temporary_directory(self, tmp_path: Path) -> None:
        # Arrange
        session_path = tmp_path / "session.profile"

        # Act
        with open_profile(session_path, create_if_missing=True) as profile_directory:
            (profile_directory / "Default").mkdir()
            (profile_directory / "Default" / "Cookies").write_bytes(b"fake-browser-database")
            save_profile(session_path, profile_directory)

        # Assert
        assert not profile_directory.exists()
        assert stat.S_IMODE(session_path.stat().st_mode) == 0o600
        with open_profile(session_path) as restored_directory:
            cookies = restored_directory / "Default" / "Cookies"
            assert cookies.read_bytes() == b"fake-browser-database"
            assert stat.S_IMODE(restored_directory.stat().st_mode) == 0o700
            assert stat.S_IMODE(cookies.stat().st_mode) == 0o600
        assert not restored_directory.exists()
        assert {path.name for path in tmp_path.iterdir()} == {"session.profile"}

    def test_open_profile_locks_the_archive_until_the_profile_is_closed(self, tmp_path: Path) -> None:
        # Arrange
        session_path = tmp_path / "session.profile"

        # Act
        with open_profile(session_path, create_if_missing=True):
            with pytest.raises(SessionInUseError):
                with open_profile(session_path, create_if_missing=True):
                    pytest.fail("A second writer must not be allowed")

        # Assert
        assert not list(tmp_path.iterdir())

    def test_open_profile_without_an_archive_fails_without_leaving_a_lock(self, tmp_path: Path) -> None:
        # Act
        with pytest.raises(SessionNotFoundError):
            with open_profile(tmp_path / "missing.profile"):
                pytest.fail("Missing archive must not open")

        # Assert
        assert not list(tmp_path.iterdir())

    def test_open_profile_does_not_overwrite_a_directory(self, tmp_path: Path) -> None:
        # Arrange
        legacy_directory = tmp_path / "legacy.browser"
        legacy_directory.mkdir()
        (legacy_directory / "keep").write_text("keep")

        # Act
        with pytest.raises(SessionArchiveError, match="not a directory"):
            with open_profile(legacy_directory, create_if_missing=True):
                pytest.fail("A directory is not an archive file")

        # Assert
        assert (legacy_directory / "keep").read_text() == "keep"
        assert {path.name for path in tmp_path.iterdir()} == {"legacy.browser"}

    @pytest.mark.parametrize("name", ["../escape", "/absolute", "Default/../../escape", "C:/escape", "Default\\escape"])
    def test_open_profile_rejects_unsafe_session_paths(self, tmp_path: Path, name: str) -> None:
        # Arrange
        session_path = tmp_path / "session.profile"
        with zipfile.ZipFile(session_path, "w") as archive:
            archive.writestr(name, "unsafe")

        # Act
        with pytest.raises(SessionArchiveError, match="unsafe"):
            with open_profile(session_path):
                pytest.fail("Unsafe archive must not open")

        # Assert
        assert {path.name for path in tmp_path.iterdir()} == {"session.profile"}

    def test_open_profile_rejects_symbolic_links_in_the_archive(self, tmp_path: Path) -> None:
        # Arrange
        session_path = tmp_path / "session.profile"
        entry = zipfile.ZipInfo("link")
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(session_path, "w") as archive:
            archive.writestr(entry, "../outside")

        # Act / Assert
        with pytest.raises(SessionArchiveError, match="symbolic link"):
            with open_profile(session_path):
                pytest.fail("Symlink entries must not be extracted")

    def test_open_profile_rejects_an_archive_that_is_not_a_zip(self, tmp_path: Path) -> None:
        # Arrange
        session_path = tmp_path / "session.profile"
        session_path.write_bytes(b"not a zip")

        # Act
        with pytest.raises(SessionArchiveError, match="Could not safely unpack"):
            with open_profile(session_path):
                pytest.fail("A corrupt archive must not open")

        # Assert
        assert session_path.read_bytes() == b"not a zip"

    def test_save_profile_skips_links_to_files_outside_the_profile(self, tmp_path: Path) -> None:
        # Arrange
        outside_file = tmp_path / "outside"
        outside_file.write_text("not part of the profile")

        # Act
        with open_profile(tmp_path / "session.profile", create_if_missing=True) as profile_directory:
            (profile_directory / "regular").write_text("profile data")
            (profile_directory / "link").symlink_to(outside_file)
            save_profile(tmp_path / "session.profile", profile_directory)

        # Assert
        with zipfile.ZipFile(tmp_path / "session.profile") as archive:
            assert archive.namelist() == ["regular"]

    def test_save_profile_skips_files_that_lock_a_running_browser(self, tmp_path: Path) -> None:
        # Arrange
        session_path = tmp_path / "session.profile"

        # Act
        with open_profile(session_path, create_if_missing=True) as profile_directory:
            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort", "Cookies"):
                (profile_directory / name).write_text(name)
            save_profile(session_path, profile_directory)

        # Assert
        with zipfile.ZipFile(session_path) as archive:
            assert archive.namelist() == ["Cookies"]

    def test_save_profile_failure_preserves_previous_archive_and_removes_temporary_file(self, tmp_path: Path) -> None:
        # Arrange
        session_path = tmp_path / "session.profile"
        with zipfile.ZipFile(session_path, "w") as archive:
            archive.writestr("existing", "original")
        previous_archive = session_path.read_bytes()

        # Act
        with open_profile(session_path) as profile_directory:
            unreadable_file = profile_directory / "unreadable"
            unreadable_file.write_text("browser state")
            unreadable_file.chmod(0o000)
            with pytest.raises(PermissionError):
                save_profile(session_path, profile_directory)
            unreadable_file.chmod(0o600)

        # Assert
        assert session_path.read_bytes() == previous_archive
        assert {path.name for path in tmp_path.iterdir()} == {"session.profile"}
