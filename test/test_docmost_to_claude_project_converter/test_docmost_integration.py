import io
import uuid
import zipfile

import pytest

from docmost_to_claude_project_converter.docmost import DocmostClient

from .conftest import DocmostPages, ImportPages


def _read_markdown_pages(markdown_export: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(markdown_export)) as archive:
        return {name: archive.read(name).decode() for name in archive.namelist() if name.endswith(".md")}


@pytest.mark.integration()
class TestHttpxDocmostClientIntegration:
    def test_export_page_returns_the_page_tree_with_attached_images(
        self,
        docmost_client: DocmostClient,
        docmost_pages: DocmostPages,
        import_pages: ImportPages,
        png: bytes,
    ) -> None:
        # Arrange
        title = f"Parent {uuid.uuid4()}"
        import_pages(
            {
                "parent.md": f"# {title}\n\nThe parent.\n",
                "parent/child.md": "# Child\n\n![diagram](image/diagram.png)\n",
                "parent/image/diagram.png": png,
            }
        )
        parent_id = next(page["id"] for page in docmost_pages.root_pages() if page["title"] == title)

        # Act
        pages = _read_markdown_pages(docmost_client.export_page(parent_id, "markdown"))

        # Assert
        assert sorted(pages) == [f"{title}.md", f"{title}/Child.md"]
        assert "![diagram](" in pages[f"{title}/Child.md"]
        assert "](files/" in pages[f"{title}/Child.md"]

    def test_get_page_id_resolves_slug_id_from_page_url(
        self,
        docmost_client: DocmostClient,
        docmost_pages: DocmostPages,
        import_pages: ImportPages,
    ) -> None:
        # Arrange
        title = f"Slug {uuid.uuid4()}"
        import_pages({"slug.md": f"# {title}\n\nText.\n"})
        page = next(page for page in docmost_pages.root_pages() if page["title"] == title)

        # Act
        page_id = docmost_client.get_page_id(page["slugId"])

        # Assert
        assert page_id == page["id"]
