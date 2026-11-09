import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time

import pytest
from patchright.sync_api import Route, sync_playwright

from docmost_to_claude_project_converter.browser import _wait_for_login
from docmost_to_claude_project_converter.claude import (
    _CLAUDE_API_ORGANIZATIONS_URL,
    _CLAUDE_SITE_URL,
    BrowserClaudeClient,
    ProjectDoc,
)
from docmost_to_claude_project_converter.exceptions import CloudflareBlockedError
from docmost_to_claude_project_converter.profile import save_profile

from .conftest import RoutedBrowserLauncher


@pytest.mark.integration()
class TestBrowserTransportIntegration:
    def test_handle_request_fetches_with_browser_cookies_and_encodes_json_and_multipart(
        self,
        session_path: Path,
    ) -> None:
        # Arrange
        requests: list[tuple[str, str, bytes, dict[str, str]]] = []

        def route_request(route: Route) -> None:
            request = route.request
            requests.append((request.method, request.url, request.post_data_buffer or b"", request.all_headers()))
            if request.url == "https://claude.ai/":
                route.fulfill(content_type="text/html", body="<html><body>Local transport test</body></html>")
            elif request.url.endswith("/api/organizations"):
                route.fulfill(json=[{"uuid": "org"}])
            elif request.url.endswith("/docs") and request.method == "GET":
                route.fulfill(json=[{"uuid": "doc", "file_name": "Page.md", "content": "Hello"}])
            elif request.url.endswith("/files"):
                route.fulfill(status=403, headers={"cf-mitigated": "challenge"}, body="Challenge")
            else:
                route.fulfill(json={})

        # All claude.ai traffic is fulfilled locally. No live cookies, accounts, or network calls are involved.
        browser_launcher = RoutedBrowserLauncher(
            route_request, cookies=[{"name": "sessionKey", "value": "fake-cookie", "url": "https://claude.ai"}]
        )
        claude_client = BrowserClaudeClient(session_path, browser_launcher)

        # Act
        try:
            docs = claude_client.get_docs("project")
            claude_client.create_doc("project", "Unicode.md", "Hello 🌍")
            claude_client.upload_file("project", "Image.pdf", b"%PDF-\x00\xff\x80")
            claude_client.delete_doc("project", "doc")
            with pytest.raises(CloudflareBlockedError):
                claude_client.get_files("project")
        finally:
            claude_client.close()

        # Assert
        assert docs == [ProjectDoc("doc", "Page.md", "Hello")]
        api_requests = [request for request in requests if "/api/" in request[1]]
        assert all(request[3]["cookie"] == "sessionKey=fake-cookie" for request in api_requests)
        assert all(request[3]["referer"].startswith("https://claude.ai/") for request in api_requests)
        create = next(request for request in api_requests if request[0] == "POST" and request[1].endswith("/docs"))
        assert json.loads(create[2]) == {"file_name": "Unicode.md", "content": "Hello 🌍"}
        upload = next(request for request in api_requests if request[1].endswith("/upload"))
        assert 'name="file"; filename="Image.pdf"' in upload[2].decode(errors="replace")
        assert b"%PDF-\x00\xff\x80" in upload[2]
        assert upload[3]["content-type"].startswith("multipart/form-data; boundary=")
        assert any(request[0] == "DELETE" and request[1].endswith("/docs/doc") for request in api_requests)

    def test_handle_request_reopens_the_profile_archive_with_its_saved_cookie(self, tmp_path: Path) -> None:
        # Arrange
        observed_cookies: list[str] = []

        def route_request(route: Route) -> None:
            if route.request.url.endswith("/api/organizations"):
                observed_cookies.append(route.request.all_headers().get("cookie", ""))
                route.fulfill(json=[{"uuid": "org"}])
            elif route.request.url.endswith("/docs"):
                route.fulfill(json=[])
            else:
                route.fulfill(content_type="text/html", body="<html>Local test</html>")

        session_path = tmp_path / "session.profile"
        # Seed one real persistent cookie, then archive the closed profile (not a separate cookie snapshot).
        with TemporaryDirectory(dir=tmp_path) as temporary_directory:
            profile_directory = Path(temporary_directory)
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(str(profile_directory), headless=True)
                context.add_cookies(
                    [
                        {
                            "name": "sessionKey",
                            "value": "fake-session",
                            "domain": ".claude.ai",
                            "path": "/",
                            "expires": time() + 3600,
                            "httpOnly": True,
                            "secure": True,
                            "sameSite": "Lax",
                        }
                    ]
                )
                context.close()
            save_profile(session_path, profile_directory)
        browser_launcher = RoutedBrowserLauncher(route_request)

        # Act
        for _ in range(2):
            claude_client = BrowserClaudeClient(session_path, browser_launcher)
            try:
                assert claude_client.get_docs("project") == []
            finally:
                claude_client.close()

        # Assert
        assert browser_launcher.headless_launches == [True, True]
        assert observed_cookies == ["sessionKey=fake-session", "sessionKey=fake-session"]
        assert {path.name for path in tmp_path.iterdir()} == {"session.profile"}


@pytest.mark.integration()
class TestWaitForLoginIntegration:
    def test_wait_for_login_verifies_the_session_without_relying_on_the_apps_own_requests(self) -> None:
        # Arrange
        def route_request(route: Route) -> None:
            if route.request.url.endswith("/api/organizations"):
                route.fulfill(json=[{"uuid": "org"}])
            else:
                route.fulfill(
                    content_type="text/html",
                    body="<html>Signed in</html>",
                    headers={"set-cookie": "sessionKey=fake-cookie; Path=/; Secure; HttpOnly"},
                )

        # Act / Assert
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            context.route("**/*", route_request)
            try:
                _wait_for_login(
                    context,
                    site_url=_CLAUDE_SITE_URL,
                    verification_url=_CLAUDE_API_ORGANIZATIONS_URL,
                    timeout_milliseconds=5000,
                )
            finally:
                browser.close()
