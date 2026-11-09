import base64
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from time import monotonic

import httpx
from patchright.sync_api import BrowserContext, Page, sync_playwright
from patchright.sync_api import Error as PlaywrightError

from docmost_to_claude_project_converter.exceptions import (
    BrowserLoginError,
    CloudflareBlockedError,
    DisallowedRequestUrlError,
)
from docmost_to_claude_project_converter.profile import open_profile, save_profile

_SIGN_IN_POLL_INTERVAL_MILLISECONDS = 250
_SIGN_IN_TIMEOUT_MILLISECONDS = 300_000

_HTTPX_FORWARDED_REQUEST_HEADERS = ("content-type", "accept", "anthropic-client-platform")
_HTTPX_DROPPED_RESPONSE_HEADERS = ("content-encoding", "content-length", "transfer-encoding")

_SEND_REQUEST_FROM_PAGE_SCRIPT = """async ({url, method, headers, body}) => {
    const response = await fetch(url, {
        method, headers, credentials: 'same-origin', redirect: 'error',
        signal: AbortSignal.timeout(60000),
        body: body === null ? undefined : Uint8Array.from(atob(body), c => c.charCodeAt(0)),
    });
    return {status: response.status, headers: Object.fromEntries(response.headers), text: await response.text()};
}"""


class BrowserLauncher(ABC):
    @abstractmethod
    def launch(
        self,
        profile_directory: Path,
        *,
        should_run_headless: bool,
    ) -> AbstractContextManager[BrowserContext]: ...


class PatchrightBrowserLauncher(BrowserLauncher):
    @contextmanager
    def launch(self, profile_directory: Path, *, should_run_headless: bool) -> Generator[BrowserContext]:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_directory.resolve()),
                headless=should_run_headless,
                no_viewport=True,
            )

            try:
                yield context
            finally:
                context.close()


class BrowserTransport(httpx.BaseTransport):
    def __init__(self, browser_launcher: BrowserLauncher, session_path: Path, *, site_url: str, api_url: str) -> None:
        self._browser_launcher = browser_launcher
        self._session_path = session_path
        self._site_url = site_url
        self._api_url = api_url

        self._browser_page: Page | None = None
        self._browser_resources = ExitStack()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not str(request.url).startswith(f"{self._api_url}/"):
            raise DisallowedRequestUrlError(str(request.url))

        page = self._browser_page or self._open_site_page()
        if not page.url.startswith(f"{self._site_url}/"):
            raise BrowserLoginError(f"The browser left '{self._site_url}'.")

        request_body = request.read()
        request_headers = {
            name: value for name, value in request.headers.items() if name.lower() in _HTTPX_FORWARDED_REQUEST_HEADERS
        }
        fetch_result = page.evaluate(
            _SEND_REQUEST_FROM_PAGE_SCRIPT,
            {
                "url": str(request.url),
                "method": request.method,
                "headers": request_headers,
                "body": base64.b64encode(request_body).decode() if request_body else None,
            },
        )

        response_headers = {
            name: value
            for name, value in fetch_result["headers"].items()
            if name.lower() not in _HTTPX_DROPPED_RESPONSE_HEADERS
        }
        return httpx.Response(
            fetch_result["status"],
            headers=response_headers,
            text=fetch_result["text"],
            request=request,
        )

    def close(self) -> None:
        self._browser_page = None
        self._browser_resources.close()

    def _open_site_page(self) -> Page:
        try:
            profile_directory = self._browser_resources.enter_context(open_profile(self._session_path))

            self._browser_resources.callback(save_profile, self._session_path, profile_directory)
            context = self._browser_resources.enter_context(
                self._browser_launcher.launch(profile_directory, should_run_headless=True)
            )

            page = context.pages[0] if context.pages else context.new_page()
            page.goto(self._site_url, wait_until="domcontentloaded")
            self._browser_page = page

            return page
        except BaseException:
            self.close()
            raise


def create_session(
    session_path: Path,
    browser_launcher: BrowserLauncher,
    *,
    site_url: str,
    verification_url: str,
) -> None:
    try:
        with open_profile(session_path, create_if_missing=True) as profile_directory:
            with browser_launcher.launch(profile_directory, should_run_headless=False) as context:
                _wait_for_login(context, site_url=site_url, verification_url=verification_url)

            save_profile(session_path, profile_directory)
    except PlaywrightError as error:
        raise BrowserLoginError("The window closed, timed out, or could not start.") from error


def _wait_for_login(
    context: BrowserContext,
    *,
    site_url: str,
    verification_url: str,
    timeout_milliseconds: float = _SIGN_IN_TIMEOUT_MILLISECONDS,
) -> None:
    sign_in_deadline = monotonic() + timeout_milliseconds / 1000
    sign_in_page = context.pages[0] if context.pages else context.new_page()
    sign_in_page.goto(site_url, wait_until="domcontentloaded", timeout=timeout_milliseconds)

    while monotonic() < sign_in_deadline:
        if _has_session_cookie(context, site_url):
            remaining_milliseconds = max(1, (sign_in_deadline - monotonic()) * 1000)
            _verify_session(context, verification_url, remaining_milliseconds=remaining_milliseconds)
            return

        sign_in_page.wait_for_timeout(_SIGN_IN_POLL_INTERVAL_MILLISECONDS)

    raise BrowserLoginError("Timed out waiting for sign-in, finish it within five minutes.")


def _has_session_cookie(context: BrowserContext, site_url: str) -> bool:
    return any(cookie.get("name") == "sessionKey" and cookie.get("value") for cookie in context.cookies([site_url]))


def _verify_session(context: BrowserContext, verification_url: str, *, remaining_milliseconds: float) -> None:
    verification_page = context.new_page()

    try:
        organizations_response = verification_page.goto(
            verification_url,
            wait_until="domcontentloaded",
            timeout=remaining_milliseconds,
        )
        if organizations_response is None:
            raise BrowserLoginError("Session rejected.")

        if organizations_response.headers.get("cf-mitigated"):
            raise CloudflareBlockedError()

        if organizations_response.status != 200:
            raise BrowserLoginError("Session rejected.")

        organizations = organizations_response.json()
        if not isinstance(organizations, list) or not organizations:
            raise BrowserLoginError("The browser did not return an valid verification url response.")
    finally:
        verification_page.close()
