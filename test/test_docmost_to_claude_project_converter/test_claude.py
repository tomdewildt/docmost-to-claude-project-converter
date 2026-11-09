import json
import stat
import zipfile
from pathlib import Path

import httpx
import pytest

from docmost_to_claude_project_converter.browser import BrowserTransport
from docmost_to_claude_project_converter.claude import (
    _CLAUDE_API_ORGANIZATIONS_URL,
    _CLAUDE_MAX_FILE_SIZE_BYTES,
    BrowserClaudeClient,
    ProjectDoc,
    ProjectFile,
    create_claude_client,
    extract_claude_project_id,
)
from docmost_to_claude_project_converter.config import ConverterConfig
from docmost_to_claude_project_converter.exceptions import (
    BrowserLoginError,
    CloudflareBlockedError,
    KnowledgeFileTooLargeError,
    ProjectNotFoundError,
    SessionExpiredError,
)

from .conftest import BuildClaudeClient, FakeBrowserContext, FakeBrowserLauncher, FakeBrowserResponse


class TestBrowserClaudeClient:
    def test_login_saves_the_signed_in_browser_profile_to_the_session_archive(
        self,
        claude_client: BrowserClaudeClient,
        fake_browser_launcher: FakeBrowserLauncher,
        fake_browser_context: FakeBrowserContext,
        session_path: Path,
    ) -> None:
        # Arrange
        fake_browser_context.sign_in()

        # Act
        claude_client.login()

        # Assert
        [(profile_directory, should_run_headless)] = fake_browser_launcher.launches
        assert not should_run_headless
        assert not profile_directory.exists()
        assert fake_browser_context.is_closed
        with zipfile.ZipFile(session_path) as archive:
            assert archive.namelist() == ["Default/Cookies"]
        assert stat.S_IMODE(session_path.stat().st_mode) == 0o600
        assert {path.name for path in session_path.parent.iterdir()} == {"session.profile"}

    def test_login_failure_preserves_the_existing_session(
        self,
        claude_client: BrowserClaudeClient,
        fake_browser_context: FakeBrowserContext,
        session_path: Path,
    ) -> None:
        # Arrange
        fake_browser_context.sign_in()
        fake_browser_context.verification_page.responses[_CLAUDE_API_ORGANIZATIONS_URL] = FakeBrowserResponse(401, [])
        previous_session = session_path.read_bytes()

        # Act
        with pytest.raises(BrowserLoginError):
            claude_client.login()

        # Assert
        assert session_path.read_bytes() == previous_session
        assert {path.name for path in session_path.parent.iterdir()} == {"session.profile"}
        assert fake_browser_context.is_closed

    def test_get_docs_resolves_organization_once_and_caches_it(self, build_claude_client: BuildClaudeClient) -> None:
        # Arrange
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            assert "cookie" not in request.headers  # The browser, not Python/httpx, attaches credentials.
            assert request.headers["anthropic-client-platform"] == "web_claude_ai"
            match request.url.path:
                case "/api/organizations":
                    return httpx.Response(200, json=[{"uuid": "wrong"}, {"uuid": "org"}])
                case "/api/organizations/wrong/projects/project":
                    return httpx.Response(404)
                case "/api/organizations/org/projects/project":
                    return httpx.Response(200, json={"uuid": "project"})
                case _:
                    return httpx.Response(200, json=[{"uuid": "doc", "file_name": "Page.md", "content": "Hello"}])

        claude_client = build_claude_client(handler)

        # Act
        first_docs = claude_client.get_docs("project")
        claude_client.get_docs("project")

        # Assert
        assert first_docs == [ProjectDoc("doc", "Page.md", "Hello")]
        assert paths == [
            "/api/organizations",
            "/api/organizations/wrong/projects/project",
            "/api/organizations/org/projects/project",
            "/api/organizations/org/projects/project/docs",
            "/api/organizations/org/projects/project/docs",
        ]

    def test_doc_and_file_requests_use_project_paths_and_payloads(self, build_claude_client: BuildClaudeClient) -> None:
        # Arrange
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/organizations":
                return httpx.Response(200, json=[{"uuid": "org"}])
            if request.url.path.endswith("/files") and request.method == "GET":
                return httpx.Response(
                    200, json=[{"uuid": "association", "file_uuid": "file", "file_name": "Image.pdf"}]
                )
            return httpx.Response(200, json={})

        claude_client = build_claude_client(handler)

        # Act
        claude_client.create_doc("project", "Page.md", "Hello")
        claude_client.delete_doc("project", "doc")
        files = claude_client.get_files("project")
        claude_client.upload_file("project", "Image.pdf", b"%PDF-test")
        claude_client.delete_file("project", "file")

        # Assert
        assert files == [ProjectFile("file", "Image.pdf")]
        assert [(request.method, request.url.path) for request in requests[2:]] == [
            ("POST", "/api/organizations/org/projects/project/docs"),
            ("DELETE", "/api/organizations/org/projects/project/docs/doc"),
            ("GET", "/api/organizations/org/projects/project/files"),
            ("POST", "/api/organizations/org/projects/project/upload"),
            ("POST", "/api/organizations/org/projects/project/files/delete_many"),
        ]
        assert json.loads(requests[2].content) == {"file_name": "Page.md", "content": "Hello"}
        assert 'name="file"; filename="Image.pdf"' in requests[5].content.decode()
        assert b"Content-Type: application/pdf" in requests[5].content
        assert b"%PDF-test" in requests[5].content
        assert json.loads(requests[6].content) == {"file_uuids": ["file"]}

    @pytest.mark.parametrize(
        ("status", "headers", "error"),
        [
            (401, {}, SessionExpiredError),
            (403, {}, SessionExpiredError),
            (403, {"cf-mitigated": "challenge"}, CloudflareBlockedError),
            (429, {"cf-mitigated": "challenge"}, CloudflareBlockedError),
        ],
    )
    def test_get_docs_raises_for_a_rejected_session(
        self,
        build_claude_client: BuildClaudeClient,
        status: int,
        headers: dict[str, str],
        error: type[Exception],
    ) -> None:
        # Arrange
        claude_client = build_claude_client(lambda request: httpx.Response(status, headers=headers))

        # Act / Assert
        with pytest.raises(error):
            claude_client.get_docs("project")

    def test_get_docs_raises_for_a_server_error(self, build_claude_client: BuildClaudeClient) -> None:
        # Arrange
        claude_client = build_claude_client(lambda request: httpx.Response(500))

        # Act / Assert
        with pytest.raises(httpx.HTTPStatusError):
            claude_client.get_docs("project")

    def test_get_docs_raises_when_an_organization_fails_to_look_up_the_project(
        self,
        build_claude_client: BuildClaudeClient,
    ) -> None:
        # Arrange
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/organizations":
                return httpx.Response(200, json=[{"uuid": "org"}])
            if request.url.path == "/api/organizations/org/projects/project":
                return httpx.Response(500)
            return httpx.Response(200, json=[])

        claude_client = build_claude_client(handler)

        # Act / Assert
        with pytest.raises(httpx.HTTPStatusError):
            claude_client.get_docs("project")

    def test_get_docs_raises_when_no_organization_has_the_project(self, build_claude_client: BuildClaudeClient) -> None:
        # Arrange
        claude_client = build_claude_client(lambda request: httpx.Response(200, json=[]))

        # Act / Assert
        with pytest.raises(ProjectNotFoundError, match="project"):
            claude_client.get_docs("project")

    def test_upload_file_rejects_an_oversize_file_before_requests(self, build_claude_client: BuildClaudeClient) -> None:
        # Arrange
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("No requests expected")

        claude_client = build_claude_client(handler)

        # Act / Assert
        with pytest.raises(KnowledgeFileTooLargeError):
            claude_client.upload_file("project", "large.pdf", b"x" * (_CLAUDE_MAX_FILE_SIZE_BYTES + 1))

    def test_create_doc_rejects_an_oversize_doc_before_requests(self, build_claude_client: BuildClaudeClient) -> None:
        # Arrange
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("No requests expected")

        claude_client = build_claude_client(handler)

        # Act / Assert
        with pytest.raises(KnowledgeFileTooLargeError):
            claude_client.create_doc("project", "large.md", "x" * (_CLAUDE_MAX_FILE_SIZE_BYTES + 1))

    def test_close_forgets_the_resolved_organization(self, build_claude_client: BuildClaudeClient) -> None:
        # Arrange
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, json=[{"uuid": "org"}] if request.url.path == "/api/organizations" else [])

        claude_client = build_claude_client(handler)
        claude_client.get_docs("project")

        # Act
        claude_client.close()
        claude_client.get_docs("project")

        # Assert
        assert paths.count("/api/organizations") == 2


class TestExtractClaudeProjectId:
    @pytest.mark.parametrize(
        "value", ["abc", "https://claude.ai/project/abc", "https://claude.ai/project/abc/?foo=bar#x"]
    )
    def test_extract_claude_project_id_returns_id_for_urls_and_bare_ids(self, value: str) -> None:
        # Act
        project_id = extract_claude_project_id(value)

        # Assert
        assert project_id == "abc"


class TestCreateClaudeClient:
    def test_create_claude_client_uses_the_configured_session(self, tmp_path: Path) -> None:
        # Arrange
        claude_config = ConverterConfig(
            _env_file=None,  # type: ignore[call-arg]
            DOCMOST_URL="http://docmost.test",
            DOCMOST_EMAIL="user@example.com",
            DOCMOST_PASSWORD="password",
            CLAUDE_SESSION_PATH=tmp_path / "session.profile",
        )

        # Act
        claude_client = create_claude_client(claude_config)

        # Assert
        assert isinstance(claude_client, BrowserClaudeClient)
        assert claude_client._session_path == tmp_path / "session.profile"
        assert isinstance(claude_client._transport, BrowserTransport)
