import io
import os
import time
import zipfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from patchright.sync_api import Route, sync_playwright

from docmost_to_claude_project_converter.browser import BrowserLauncher, BrowserTransport
from docmost_to_claude_project_converter.claude import (
    _CLAUDE_API_ORGANIZATIONS_URL,
    BrowserClaudeClient,
    ClaudeClient,
    ProjectDoc,
    ProjectFile,
)
from docmost_to_claude_project_converter.config import config
from docmost_to_claude_project_converter.docmost import (
    DocmostClient,
    ExportFormat,
    HttpxDocmostClient,
    create_docmost_client,
)
from docmost_to_claude_project_converter.pdf import PdfRenderer
from docmost_to_claude_project_converter.service import ConversionService

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

HttpHandler = Callable[[httpx.Request], httpx.Response]
BuildZip = Callable[[dict[str, str | bytes]], bytes]
BuildDocmostClient = Callable[..., HttpxDocmostClient]
BuildClaudeClient = Callable[[HttpHandler], BrowserClaudeClient]
ImportPages = Callable[[dict[str, str | bytes]], None]


class FakeDocmostClient(DocmostClient):
    def __init__(self) -> None:
        self.exports: dict[tuple[str, ExportFormat], bytes] = {}
        self.calls: list[tuple[str, ...]] = []

    def get_page_id(self, page_id_or_slug_id: str) -> str:
        self.calls.append(("get_page_id", page_id_or_slug_id))
        return page_id_or_slug_id

    def export_page(self, page_id: str, export_format: ExportFormat) -> bytes:
        self.calls.append(("export_page", page_id, export_format))
        return self.exports[page_id, export_format]


class FakeClaudeClient(ClaudeClient):
    def __init__(self) -> None:
        self.docs: list[ProjectDoc] = []
        self.files: list[ProjectFile] = []
        self.calls: list[tuple[str, ...]] = []
        self.is_closed = False
        self.create_doc_error: Exception | None = None
        self._created_count = 0

    def login(self) -> None:
        self.calls.append(("login",))

    def get_docs(self, project_id: str) -> list[ProjectDoc]:
        return self.docs.copy()

    def get_files(self, project_id: str) -> list[ProjectFile]:
        return self.files.copy()

    def create_doc(self, project_id: str, file_name: str, content: str) -> None:
        self.calls.append(("create_doc", project_id, file_name, content))
        if self.create_doc_error is not None:
            raise self.create_doc_error
        self._created_count += 1
        self.docs.append(ProjectDoc(f"new-doc-{self._created_count}", file_name, content))

    def upload_file(self, project_id: str, file_name: str, content: bytes) -> None:
        self.calls.append(("upload_file", project_id, file_name))
        self._created_count += 1
        self.files.append(ProjectFile(f"new-file-{self._created_count}", file_name))

    def delete_doc(self, project_id: str, doc_id: str) -> None:
        self.calls.append(("delete_doc", project_id, doc_id))
        self.docs = [doc for doc in self.docs if doc.id != doc_id]

    def delete_file(self, project_id: str, file_id: str) -> None:
        self.calls.append(("delete_file", project_id, file_id))
        self.files = [file for file in self.files if file.id != file_id]

    def close(self) -> None:
        self.is_closed = True


class FakePdfRenderer(PdfRenderer):
    def __init__(self) -> None:
        self.calls: list[Path] = []
        self.files_next_to_html: list[set[str]] = []

    def render(self, html_path: Path) -> bytes:
        assert html_path.is_file()
        self.calls.append(html_path)
        self.files_next_to_html.append(
            {file.relative_to(html_path.parent).as_posix() for file in html_path.parent.rglob("*") if file.is_file()}
        )
        return b"%PDF-fake"


class FakeBrowserResponse:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body

    def json(self) -> Any:
        return self._body


class FakeBrowserPage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.responses: dict[str, FakeBrowserResponse] = {}
        self.fetch_result: dict[str, Any] = {"status": 200, "headers": {}, "text": "{}"}
        self.fetch_arguments: list[dict[str, Any]] = []
        self.is_closed = False

    def goto(self, url: str, **kwargs: Any) -> FakeBrowserResponse | None:
        # Browsers report a bare origin with a trailing slash: https://claude.ai -> https://claude.ai/
        self.url = f"{url}/" if url.count("/") == 2 else url
        return self.responses.get(url)

    def evaluate(self, script: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.fetch_arguments.append(arguments)
        return self.fetch_result

    def wait_for_timeout(self, milliseconds: float) -> None:
        return None

    def close(self) -> None:
        self.is_closed = True


class FakeBrowserContext:
    def __init__(self) -> None:
        self.page = FakeBrowserPage()
        self.verification_page = FakeBrowserPage()
        self.pages = [self.page]
        self.session_cookies: list[dict[str, str]] = []
        self.is_closed = False

    def sign_in(self) -> None:
        self.session_cookies = [{"name": "sessionKey", "value": "secret"}]
        self.verification_page.responses[_CLAUDE_API_ORGANIZATIONS_URL] = FakeBrowserResponse(200, [{"uuid": "org"}])

    def cookies(self, urls: list[str]) -> list[dict[str, str]]:
        return self.session_cookies

    def new_page(self) -> FakeBrowserPage:
        return self.verification_page

    def close(self) -> None:
        self.is_closed = True


class FakeBrowserLauncher(BrowserLauncher):
    def __init__(self, context: FakeBrowserContext) -> None:
        self.context = context
        self.launches: list[tuple[Path, bool]] = []

    @contextmanager
    def launch(self, profile_directory: Path, *, should_run_headless: bool) -> Generator[Any]:
        self.launches.append((profile_directory, should_run_headless))
        # Like Chromium, keep state in the profile so tests can see whether it is archived.
        (profile_directory / "Default").mkdir(exist_ok=True)
        (profile_directory / "Default" / "Cookies").write_bytes(b"fake-browser-database")
        try:
            yield self.context
        finally:
            self.context.close()


class RoutedBrowserLauncher(BrowserLauncher):
    def __init__(self, route_request: Callable[[Route], None], cookies: list[Any] | None = None) -> None:
        self.headless_launches: list[bool] = []
        self._route_request = route_request
        self._cookies = cookies or []

    @contextmanager
    def launch(self, profile_directory: Path, *, should_run_headless: bool) -> Generator[Any]:
        self.headless_launches.append(should_run_headless)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile_directory), headless=True)
            context.route("**/*", self._route_request)
            if self._cookies:
                context.add_cookies(self._cookies)
            try:
                yield context
            finally:
                context.close()


class DocmostPages:
    def __init__(self) -> None:
        self._client = httpx.Client(base_url=config.DOCMOST_URL, timeout=30)
        self._client.post(
            "/api/auth/login", json={"email": config.DOCMOST_EMAIL, "password": config.DOCMOST_PASSWORD}
        ).raise_for_status()
        self.space_id = self._post("/api/spaces/info", {"spaceId": "general"})["id"]

    def root_pages(self) -> list[dict[str, Any]]:
        return self._post("/api/pages/sidebar-pages", {"spaceId": self.space_id})["items"]

    def import_zip(self, content: bytes) -> str:
        response = self._client.post(
            "/api/pages/import-zip",
            files={"file": ("pages.zip", content, "application/zip")},
            data={"spaceId": self.space_id, "source": "generic"},
        )
        response.raise_for_status()
        return response.json()["data"]["id"]

    def import_job_status(self, import_job_id: str) -> str:
        return self._post("/api/file-tasks/info", {"fileTaskId": import_job_id})["status"]

    def delete(self, page_id: str) -> None:
        self._post("/api/pages/delete", {"pageId": page_id, "permanentlyDelete": True})

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        response = self._client.post(path, json=body)
        response.raise_for_status()
        return response.json().get("data")


@pytest.fixture()
def fake_docmost_client() -> FakeDocmostClient:
    return FakeDocmostClient()


@pytest.fixture()
def fake_claude_client() -> FakeClaudeClient:
    return FakeClaudeClient()


@pytest.fixture()
def fake_pdf_renderer() -> FakePdfRenderer:
    return FakePdfRenderer()


@pytest.fixture()
def fake_browser_context() -> FakeBrowserContext:
    return FakeBrowserContext()


@pytest.fixture()
def fake_browser_launcher(fake_browser_context: FakeBrowserContext) -> FakeBrowserLauncher:
    return FakeBrowserLauncher(fake_browser_context)


@pytest.fixture()
def service(
    fake_docmost_client: FakeDocmostClient,
    fake_claude_client: FakeClaudeClient,
    fake_pdf_renderer: FakePdfRenderer,
    tmp_path: Path,
) -> ConversionService:
    return ConversionService(fake_docmost_client, fake_claude_client, fake_pdf_renderer, tmp_path / "output")


@pytest.fixture()
def claude_client(session_path: Path, fake_browser_launcher: FakeBrowserLauncher) -> BrowserClaudeClient:
    return BrowserClaudeClient(session_path, fake_browser_launcher)


@pytest.fixture()
def browser_transport(session_path: Path, fake_browser_launcher: FakeBrowserLauncher) -> Generator[BrowserTransport]:
    browser_transport = BrowserTransport(
        fake_browser_launcher,
        session_path,
        site_url="https://claude.ai",
        api_url="https://claude.ai/api",
    )
    yield browser_transport
    browser_transport.close()


@pytest.fixture()
def environment_without_converter_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings always read the environment, and CI sets CONV_* variables for the integration tests.
    for name in list(os.environ):
        if name.startswith("CONV_"):
            monkeypatch.delenv(name)


# Docmost rate-limits logins, so integration tests share one session per test module.
@pytest.fixture(scope="module")
def docmost_client() -> DocmostClient:
    return create_docmost_client(config)


# Docmost rate-limits logins, so integration tests share one session per test module.
@pytest.fixture(scope="module")
def docmost_pages_session() -> DocmostPages:
    return DocmostPages()


@pytest.fixture()
def docmost_pages(docmost_pages_session: DocmostPages) -> Generator[DocmostPages]:
    existing_page_ids = {page["id"] for page in docmost_pages_session.root_pages()}
    yield docmost_pages_session
    for page in docmost_pages_session.root_pages():
        if page["id"] not in existing_page_ids:
            docmost_pages_session.delete(page["id"])


@pytest.fixture()
def import_pages(docmost_pages: DocmostPages, build_zip: BuildZip) -> ImportPages:
    def import_and_wait(files: dict[str, str | bytes]) -> None:
        import_job_id = docmost_pages.import_zip(build_zip(files))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if docmost_pages.import_job_status(import_job_id) == "success":
                return
            time.sleep(1)
        raise AssertionError(f"Import job '{import_job_id}' did not succeed in time")

    return import_and_wait


@pytest.fixture()
def build_zip() -> BuildZip:
    def build(files: dict[str, str | bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path, content in files.items():
                archive.writestr(path, content)
        return buffer.getvalue()

    return build


@pytest.fixture()
def build_docmost_client() -> BuildDocmostClient:
    def build(handler: HttpHandler, *, api_key: str | None = None) -> HttpxDocmostClient:
        return HttpxDocmostClient(
            "http://docmost.test",
            api_key=api_key,
            email="user@example.com",
            password="password",
            transport=httpx.MockTransport(handler),
        )

    return build


@pytest.fixture()
def build_claude_client(session_path: Path, fake_browser_launcher: FakeBrowserLauncher) -> BuildClaudeClient:
    def build(handler: HttpHandler) -> BrowserClaudeClient:
        return BrowserClaudeClient(session_path, fake_browser_launcher, transport=httpx.MockTransport(handler))

    return build


@pytest.fixture()
def session_path(tmp_path: Path) -> Path:
    path = tmp_path / "session.profile"
    with zipfile.ZipFile(path, "w"):
        pass
    return path


@pytest.fixture()
def png() -> bytes:
    return (FIXTURES_DIR / "image.png").read_bytes()


@pytest.fixture()
def docmost_markdown_export() -> bytes:
    return (FIXTURES_DIR / "docmost-export-markdown.zip").read_bytes()


@pytest.fixture()
def docmost_html_export() -> bytes:
    return (FIXTURES_DIR / "docmost-export-html.zip").read_bytes()
