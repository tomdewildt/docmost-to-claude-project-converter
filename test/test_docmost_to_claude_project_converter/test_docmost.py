import json

import httpx
import pytest

from docmost_to_claude_project_converter.docmost import extract_docmost_page_id

from .conftest import BuildDocmostClient


class TestExtractDocmostPageId:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("https://docs.example.com/s/general/p/getting-started-tUFRDoevgH", "tUFRDoevgH"),
            ("https://docs.example.com/s/general/p/tUFRDoevgH?edit=1", "tUFRDoevgH"),
            ("01a1b7ed-94e5-73d2-9632-c4410cb14d2e", "01a1b7ed-94e5-73d2-9632-c4410cb14d2e"),
            ("tUFRDoevgH", "tUFRDoevgH"),
            ("getting-started-tUFRDoevgH", "tUFRDoevgH"),
            ("01A1B7ED-94E5-73D2-9632-C4410CB14D2E", "01A1B7ED-94E5-73D2-9632-C4410CB14D2E"),
            ("https://docs.example.com/s/general/p/getting-started-tUFRDoevgH/", "tUFRDoevgH"),
        ],
        ids=[
            "page-url",
            "untitled-page-url",
            "bare-uuid",
            "bare-slug-id",
            "page-slug",
            "uppercase-uuid",
            "trailing-slash",
        ],
    )
    def test_extract_docmost_page_id_returns_id_for_urls_and_bare_ids(self, value: str, expected: str) -> None:
        # Act
        page_id = extract_docmost_page_id(value)

        # Assert
        assert page_id == expected

    def test_export_page_requests_children_and_attachments(self, build_docmost_client: BuildDocmostClient) -> None:
        # Arrange
        export_requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/pages/export":
                export_requests.append(json.loads(request.content))
                return httpx.Response(200, content=b"zip")
            return httpx.Response(200)

        docmost_client = build_docmost_client(handler)

        # Act
        export = docmost_client.export_page("page", "html")

        # Assert
        assert export == b"zip"
        assert export_requests == [
            {"pageId": "page", "format": "html", "includeChildren": True, "includeAttachments": True}
        ]

    def test_logs_in_once_for_multiple_requests(self, build_docmost_client: BuildDocmostClient) -> None:
        # Arrange
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, json={"data": {"id": "id"}})

        docmost_client = build_docmost_client(handler)

        # Act
        docmost_client.get_page_id("page")
        docmost_client.get_page_id("other-page")

        # Assert
        assert paths == ["/api/auth/login", "/api/pages/info", "/api/pages/info"]

    def test_api_key_is_sent_as_bearer_token_instead_of_logging_in(
        self, build_docmost_client: BuildDocmostClient
    ) -> None:
        # Arrange
        requests: list[tuple[str, str | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.url.path, request.headers.get("Authorization")))
            return httpx.Response(200, json={"data": {"id": "id"}})

        docmost_client = build_docmost_client(handler, api_key="secret")

        # Act
        docmost_client.get_page_id("page")

        # Assert
        assert requests == [("/api/pages/info", "Bearer secret")]

    def test_rejected_login_raises_without_requesting_the_page(self, build_docmost_client: BuildDocmostClient) -> None:
        # Arrange
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(401 if request.url.path == "/api/auth/login" else 200, json={"data": {"id": "id"}})

        docmost_client = build_docmost_client(handler)

        # Act
        with pytest.raises(httpx.HTTPStatusError):
            docmost_client.get_page_id("page")

        # Assert
        assert paths == ["/api/auth/login"]

    def test_get_page_id_raises_for_an_unknown_page(self, build_docmost_client: BuildDocmostClient) -> None:
        # Arrange
        docmost_client = build_docmost_client(
            lambda request: httpx.Response(200 if request.url.path == "/api/auth/login" else 404, json={})
        )

        # Act / Assert
        with pytest.raises(httpx.HTTPStatusError):
            docmost_client.get_page_id("missing")
