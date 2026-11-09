import re
from abc import ABC, abstractmethod
from typing import Literal
from urllib.parse import urlparse

import httpx
from loguru import logger

from docmost_to_claude_project_converter.config import ConverterConfig

ExportFormat = Literal["markdown", "html"]

# Page UUID: 01a1b7ed-94e5-73d2-9632-c4410cb14d2e
_PAGE_UUID_PATTERN = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)


def extract_docmost_page_id(value: str) -> str:
    if _PAGE_UUID_PATTERN.fullmatch(value):
        return value
    # Slug ids never contain hyphens, so they follow the last one: /s/general/p/getting-started-tUFRDoevgH
    page_slug = urlparse(value).path.rstrip("/").rsplit("/", 1)[-1]
    return page_slug.rsplit("-", 1)[-1]


class DocmostClient(ABC):
    @abstractmethod
    def get_page_id(self, page_id_or_slug_id: str) -> str: ...

    @abstractmethod
    def export_page(self, page_id: str, export_format: ExportFormat) -> bytes: ...


class HttpxDocmostClient(DocmostClient):
    def __init__(
        self,
        url: str,
        *,
        api_key: str | None = None,
        email: str | None = None,
        password: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._email = email
        self._password = password

        # API keys (business and enterprise licenses) replace the login
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = httpx.Client(base_url=url, timeout=60, transport=transport, headers=headers)
        self._is_authenticated = api_key is not None

    def get_page_id(self, page_id_or_slug_id: str) -> str:
        return self._post("/api/pages/info", {"pageId": page_id_or_slug_id}).json()["data"]["id"]

    def export_page(self, page_id: str, export_format: ExportFormat) -> bytes:
        logger.info("Exporting page '{page_id}' as '{export_format}'", page_id=page_id, export_format=export_format)

        export_request = {
            "pageId": page_id,
            "format": export_format,
            "includeChildren": True,
            "includeAttachments": True,
        }

        return self._post("/api/pages/export", export_request).content

    def _post(self, path: str, body: dict[str, object]) -> httpx.Response:
        self._ensure_client_is_authenticated()

        response = self._client.post(path, json=body)
        response.raise_for_status()

        return response

    def _ensure_client_is_authenticated(self) -> None:
        if self._is_authenticated:
            return

        # Without an API key, the page and export endpoints of the open-source core accept the JWT cookie set by a
        # regular login.
        response = self._client.post("/api/auth/login", json={"email": self._email, "password": self._password})
        response.raise_for_status()

        self._is_authenticated = True


def create_docmost_client(config: ConverterConfig) -> DocmostClient:
    return HttpxDocmostClient(
        config.DOCMOST_URL,
        api_key=config.DOCMOST_API_KEY,
        email=config.DOCMOST_EMAIL,
        password=config.DOCMOST_PASSWORD,
    )
