from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from docmost_to_claude_project_converter.browser import (
    BrowserLauncher,
    BrowserTransport,
    PatchrightBrowserLauncher,
    create_session,
)
from docmost_to_claude_project_converter.config import ConverterConfig
from docmost_to_claude_project_converter.exceptions import (
    CloudflareBlockedError,
    KnowledgeFileTooLargeError,
    ProjectNotFoundError,
    SessionExpiredError,
)

_CLAUDE_SITE_URL = "https://claude.ai"
_CLAUDE_API_URL = f"{_CLAUDE_SITE_URL}/api"
_CLAUDE_API_ORGANIZATIONS_URL = f"{_CLAUDE_API_URL}/organizations"
_CLAUDE_MAX_FILE_SIZE_BYTES = 30 * 1024 * 1024  # 30 MB
_CLAUDE_WEB_CLIENT_HEADERS = {"anthropic-client-platform": "web_claude_ai"}


@dataclass(frozen=True)
class ProjectDoc:
    id: str
    file_name: str
    content: str


@dataclass(frozen=True)
class ProjectFile:
    id: str
    file_name: str


def extract_claude_project_id(value: str) -> str:
    project_url = urlparse(value)
    if project_url.hostname == "claude.ai" and project_url.path.startswith("/project/"):
        return project_url.path.split("/")[2]
    return value


def ensure_file_size_is_within_claude_limits(file_name: str, content: bytes) -> None:
    if len(content) > _CLAUDE_MAX_FILE_SIZE_BYTES:
        raise KnowledgeFileTooLargeError(file_name)


class ClaudeClient(ABC):
    @abstractmethod
    def login(self) -> None: ...

    @abstractmethod
    def get_docs(self, project_id: str) -> list[ProjectDoc]: ...

    @abstractmethod
    def get_files(self, project_id: str) -> list[ProjectFile]: ...

    @abstractmethod
    def create_doc(self, project_id: str, file_name: str, content: str) -> None: ...

    @abstractmethod
    def upload_file(self, project_id: str, file_name: str, content: bytes) -> None: ...

    @abstractmethod
    def delete_doc(self, project_id: str, doc_id: str) -> None: ...

    @abstractmethod
    def delete_file(self, project_id: str, file_id: str) -> None: ...

    def close(self) -> None:
        return None


class BrowserClaudeClient(ClaudeClient):
    def __init__(
        self,
        session_path: Path,
        browser_launcher: BrowserLauncher,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._session_path = session_path
        self._browser_launcher = browser_launcher

        # The browser only starts on the first request, so commands that never reach Claude never open it.
        self._transport = transport or BrowserTransport(
            browser_launcher,
            session_path,
            site_url=_CLAUDE_SITE_URL,
            api_url=_CLAUDE_API_URL,
        )
        self._client: httpx.Client | None = None
        self._organization_id_by_project_id: dict[str, str] = {}

    def login(self) -> None:
        create_session(
            self._session_path,
            self._browser_launcher,
            site_url=_CLAUDE_SITE_URL,
            verification_url=_CLAUDE_API_ORGANIZATIONS_URL,
        )

    def get_docs(self, project_id: str) -> list[ProjectDoc]:
        response = self._request("GET", f"{self._get_project_path(project_id)}/docs")

        return [ProjectDoc(doc["uuid"], doc["file_name"], doc["content"]) for doc in response.json()]

    def get_files(self, project_id: str) -> list[ProjectFile]:
        response = self._request("GET", f"{self._get_project_path(project_id)}/files")

        return [ProjectFile(file["file_uuid"], file["file_name"]) for file in response.json()]

    def create_doc(self, project_id: str, file_name: str, content: str) -> None:
        ensure_file_size_is_within_claude_limits(file_name, content.encode())

        self._request(
            "POST",
            f"{self._get_project_path(project_id)}/docs",
            json={"file_name": file_name, "content": content},
        )

    def upload_file(self, project_id: str, file_name: str, content: bytes) -> None:
        ensure_file_size_is_within_claude_limits(file_name, content)

        self._request(
            "POST",
            f"{self._get_project_path(project_id)}/upload",
            files={"file": (file_name, content, "application/pdf")},
        )

    def delete_doc(self, project_id: str, doc_id: str) -> None:
        self._request("DELETE", f"{self._get_project_path(project_id)}/docs/{doc_id}")

    def delete_file(self, project_id: str, file_id: str) -> None:
        # Claude's web client uses the bulk endpoint even for one file. DELETE /files/{uuid} returns 404.
        self._request("POST", f"{self._get_project_path(project_id)}/files/delete_many", json={"file_uuids": [file_id]})

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._organization_id_by_project_id.clear()

    def _get_project_path(self, project_id: str) -> str:
        if project_id not in self._organization_id_by_project_id:
            self._organization_id_by_project_id[project_id] = self._get_project_organization_id(project_id)

        return f"organizations/{self._organization_id_by_project_id[project_id]}/projects/{project_id}"

    def _get_project_organization_id(self, project_id: str) -> str:
        for organization in self._request("GET", "organizations").json():
            organization_id = organization["uuid"]

            project_response = self._send("GET", f"organizations/{organization_id}/projects/{project_id}")
            if project_response.status_code == 404:
                continue
            self._raise_for_claude_error(project_response)

            return organization_id

        raise ProjectNotFoundError(project_id)

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self._send(method, path, **kwargs)
        self._raise_for_claude_error(response)
        return response

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._client is None:
            self._client = httpx.Client(
                base_url=_CLAUDE_API_URL,
                headers=_CLAUDE_WEB_CLIENT_HEADERS,
                transport=self._transport,
            )

        return self._client.request(method, path, **kwargs)

    @staticmethod
    def _raise_for_claude_error(response: httpx.Response) -> None:
        if response.headers.get("cf-mitigated"):
            raise CloudflareBlockedError()
        if response.status_code in (401, 403):
            raise SessionExpiredError()
        response.raise_for_status()


def create_claude_client(config: ConverterConfig) -> ClaudeClient:
    return BrowserClaudeClient(config.CLAUDE_SESSION_PATH, PatchrightBrowserLauncher())
