import zipfile
from pathlib import Path

import httpx
import pytest

from docmost_to_claude_project_converter.browser import BrowserTransport, _wait_for_login
from docmost_to_claude_project_converter.claude import _CLAUDE_API_ORGANIZATIONS_URL, _CLAUDE_SITE_URL
from docmost_to_claude_project_converter.exceptions import (
    BrowserLoginError,
    CloudflareBlockedError,
    DisallowedRequestUrlError,
    SessionNotFoundError,
)

from .conftest import FakeBrowserContext, FakeBrowserLauncher, FakeBrowserResponse


class TestWaitForLogin:
    def test_wait_for_login_returns_once_the_session_cookie_is_verified(
        self,
        fake_browser_context: FakeBrowserContext,
    ) -> None:
        # Arrange
        fake_browser_context.sign_in()

        # Act
        _wait_for_login(fake_browser_context, site_url=_CLAUDE_SITE_URL, verification_url=_CLAUDE_API_ORGANIZATIONS_URL)  # type: ignore[arg-type]

        # Assert
        assert fake_browser_context.verification_page.is_closed

    @pytest.mark.parametrize(
        ("organizations_response", "error"),
        [
            (FakeBrowserResponse(403, "Challenge", {"cf-mitigated": "challenge"}), CloudflareBlockedError),
            (FakeBrowserResponse(401, [{"uuid": "org"}]), BrowserLoginError),
            (FakeBrowserResponse(200, []), BrowserLoginError),
        ],
        ids=["cloudflare", "rejected", "no-organizations"],
    )
    def test_wait_for_login_rejects_a_session_claude_does_not_accept(
        self,
        fake_browser_context: FakeBrowserContext,
        organizations_response: FakeBrowserResponse,
        error: type[Exception],
    ) -> None:
        # Arrange
        fake_browser_context.sign_in()
        fake_browser_context.verification_page.responses[_CLAUDE_API_ORGANIZATIONS_URL] = organizations_response

        # Act
        with pytest.raises(error):
            _wait_for_login(
                fake_browser_context,  # type: ignore[arg-type]
                site_url=_CLAUDE_SITE_URL,
                verification_url=_CLAUDE_API_ORGANIZATIONS_URL,
            )

        # Assert
        assert fake_browser_context.verification_page.is_closed

    def test_wait_for_login_times_out_while_the_session_cookie_is_empty(
        self,
        fake_browser_context: FakeBrowserContext,
    ) -> None:
        # Arrange
        fake_browser_context.session_cookies = [{"name": "sessionKey", "value": ""}]

        # Act / Assert
        with pytest.raises(BrowserLoginError, match="Timed out"):
            _wait_for_login(
                fake_browser_context,  # type: ignore[arg-type]
                site_url=_CLAUDE_SITE_URL,
                verification_url=_CLAUDE_API_ORGANIZATIONS_URL,
                timeout_milliseconds=1,
            )


class TestBrowserTransport:
    @pytest.mark.parametrize(
        "url",
        [
            "http://claude.ai/api/organizations",
            "https://example.com/api/organizations",
            "https://claude.ai.example.com/api/organizations",
            "https://claude.ai/new",
        ],
    )
    def test_handle_request_rejects_requests_outside_the_claude_api_before_opening_a_browser(
        self,
        browser_transport: BrowserTransport,
        fake_browser_launcher: FakeBrowserLauncher,
        url: str,
    ) -> None:
        # Act
        with pytest.raises(DisallowedRequestUrlError):
            browser_transport.handle_request(httpx.Request("GET", url))

        # Assert
        assert fake_browser_launcher.launches == []

    def test_handle_request_without_a_session_raises_before_opening_a_browser(
        self, fake_browser_launcher: FakeBrowserLauncher, tmp_path: Path
    ) -> None:
        # Arrange
        session_path = tmp_path / "session.profile"
        browser_transport = BrowserTransport(
            fake_browser_launcher,
            session_path,
            site_url="https://claude.ai",
            api_url="https://claude.ai/api",
        )

        # Act
        with pytest.raises(SessionNotFoundError, match="login"):
            browser_transport.handle_request(httpx.Request("GET", "https://claude.ai/api/organizations"))

        # Assert
        assert fake_browser_launcher.launches == []

    def test_handle_request_fetches_through_the_page_and_forwards_only_api_headers(
        self,
        browser_transport: BrowserTransport,
        fake_browser_context: FakeBrowserContext,
    ) -> None:
        # Arrange
        fake_browser_context.page.fetch_result = {
            "status": 200,
            "headers": {"content-type": "application/json", "content-encoding": "gzip", "content-length": "999"},
            "text": '{"ok": true}',
        }
        request = httpx.Request(
            "GET",
            "https://claude.ai/api/organizations",
            headers={"accept": "application/json", "anthropic-client-platform": "web_claude_ai", "x-custom": "drop"},
        )

        # Act
        response = browser_transport.handle_request(request)

        # Assert
        assert fake_browser_context.page.fetch_arguments[0]["headers"] == {
            "accept": "application/json",
            "anthropic-client-platform": "web_claude_ai",
        }
        assert response.json() == {"ok": True}

    def test_handle_request_rejects_requests_after_the_browser_left_claude(
        self,
        browser_transport: BrowserTransport,
        fake_browser_context: FakeBrowserContext,
    ) -> None:
        # Arrange
        browser_transport.handle_request(httpx.Request("GET", "https://claude.ai/api/organizations"))
        fake_browser_context.page.url = "https://accounts.google.com/signin"

        # Act / Assert
        with pytest.raises(BrowserLoginError, match="left 'https://claude.ai'"):
            browser_transport.handle_request(httpx.Request("GET", "https://claude.ai/api/organizations"))

    def test_close_saves_the_closed_browser_profile_to_the_session_archive(
        self,
        browser_transport: BrowserTransport,
        fake_browser_context: FakeBrowserContext,
        session_path: Path,
    ) -> None:
        # Arrange
        browser_transport.handle_request(httpx.Request("GET", "https://claude.ai/api/organizations"))

        # Act
        browser_transport.close()

        # Assert
        assert fake_browser_context.is_closed
        with zipfile.ZipFile(session_path) as archive:
            assert "Default/Cookies" in archive.namelist()
        assert {path.name for path in session_path.parent.iterdir()} == {"session.profile"}
