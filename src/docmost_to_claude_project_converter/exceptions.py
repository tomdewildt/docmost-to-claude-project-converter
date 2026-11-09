from pathlib import Path


class BrowserLoginError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(f"Claude browser login failed: {detail} Run `mise run start -- login` to sign in again.")


class CloudflareBlockedError(Exception):
    def __init__(self) -> None:
        super().__init__("Cloudflare blocked the Claude request. Run `mise run start -- login` to sign in again.")


class DisallowedRequestUrlError(Exception):
    def __init__(self, url: str) -> None:
        super().__init__(f"The browser only sends requests to the Claude API, not to '{url}'")
        self.url = url


class KnowledgeFileNameCollisionError(Exception):
    def __init__(self) -> None:
        super().__init__("Docmost page names collide when flattened. Rename the conflicting pages before pulling.")


class KnowledgeFileTooLargeError(Exception):
    def __init__(self, file_name: str) -> None:
        super().__init__(f"'{file_name}' exceeds Claude's 30 MB file limit")
        self.file_name = file_name


class ProjectNotFoundError(Exception):
    def __init__(self, project_id: str) -> None:
        super().__init__(f"Claude project '{project_id}' was not found in any organization accessible to this session")
        self.project_id = project_id


class SessionArchiveError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(f"Claude session archive error: {detail}")


class SessionExpiredError(Exception):
    def __init__(self) -> None:
        super().__init__("The Claude session expired or was rejected. Run `mise run start -- login` to sign in again.")


class SessionInUseError(Exception):
    def __init__(self, lock_path: Path) -> None:
        super().__init__(
            f"The Claude session is already in use (lock: '{lock_path}'). Close the other command first. "
            "If a previous process crashed, remove the lock only after confirming it is no longer running."
        )
        self.lock_path = lock_path


class SessionNotFoundError(Exception):
    def __init__(self, session_path: Path) -> None:
        super().__init__(
            f"No valid Claude session at '{session_path}'. Run `mise run start -- login` to sign in again."
        )
        self.session_path = session_path


class UnknownCommandError(Exception):
    def __init__(self, command: str) -> None:
        super().__init__(f"Unknown command: '{command}'")
        self.command = command


class UnsafeExportEntryError(Exception):
    def __init__(self, entry_name: str) -> None:
        super().__init__(f"Docmost export contains an unsafe entry: '{entry_name}'")
        self.entry_name = entry_name


class UnsafeKnowledgeFileNameError(Exception):
    def __init__(self, file_name: str) -> None:
        super().__init__(f"Docmost exported an unsafe knowledge file name: '{file_name}'")
        self.file_name = file_name


class UnsafeOutputFileError(Exception):
    def __init__(self, path: Path) -> None:
        super().__init__(f"Refusing to overwrite symlink or directory '{path}'")
        self.path = path
