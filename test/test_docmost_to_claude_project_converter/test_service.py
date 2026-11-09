from pathlib import Path

import pytest

from docmost_to_claude_project_converter.claude import _CLAUDE_MAX_FILE_SIZE_BYTES, ProjectDoc, ProjectFile
from docmost_to_claude_project_converter.exceptions import (
    KnowledgeFileNameCollisionError,
    KnowledgeFileTooLargeError,
    UnsafeExportEntryError,
    UnsafeKnowledgeFileNameError,
    UnsafeOutputFileError,
)
from docmost_to_claude_project_converter.service import ConversionService

from .conftest import BuildZip, FakeClaudeClient, FakeDocmostClient, FakePdfRenderer


class TestConversionService:
    def test_login_signs_in_to_claude_with_the_browser(
        self,
        service: ConversionService,
        fake_claude_client: FakeClaudeClient,
    ) -> None:
        # Act
        service.login()

        # Assert
        assert fake_claude_client.calls == [("login",)]

    def test_pull_writes_markdown_and_pdf_and_removes_stale_files(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        fake_pdf_renderer: FakePdfRenderer,
        tmp_path: Path,
        docmost_markdown_export: bytes,
        docmost_html_export: bytes,
    ) -> None:
        # Arrange
        fake_docmost_client.exports = {
            ("root", "markdown"): docmost_markdown_export,
            ("root", "html"): docmost_html_export,
        }
        output = tmp_path / "output"
        output.mkdir()
        (output / "stale.md").write_text("stale")

        # Act
        service.pull("root")

        # Assert
        assert {file.name for file in output.iterdir()} == {
            "Handbook.md",
            "Handbook - Architecture.pdf",
            "Handbook - Onboarding.md",
        }
        assert (output / "Handbook - Architecture.pdf").read_bytes() == b"%PDF-fake"
        assert "Handbook - Onboarding.md" in (output / "Handbook.md").read_text()
        assert len(fake_pdf_renderer.calls) == 1

    def test_pull_resolves_the_page_id_and_exports_the_page_and_its_subpages(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        build_zip: BuildZip,
    ) -> None:
        # Arrange
        fake_docmost_client.exports["slug", "markdown"] = build_zip({"Page.md": "text"})

        # Act
        service.pull("slug")

        # Assert
        assert fake_docmost_client.calls == [("get_page_id", "slug"), ("export_page", "slug", "markdown")]

    def test_pull_failure_preserves_previous_output(
        self,
        service: ConversionService,
        tmp_path: Path,
    ) -> None:
        # Arrange
        output = tmp_path / "output"
        output.mkdir()
        (output / "previous.md").write_text("keep")

        # Act
        with pytest.raises(KeyError):
            service.pull("root")

        # Assert
        assert (output / "previous.md").read_text() == "keep"

    def test_pull_rejects_flattened_collisions(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        build_zip: BuildZip,
    ) -> None:
        # Arrange
        fake_docmost_client.exports["root", "markdown"] = build_zip({"Parent/Child.md": "a", "Parent - Child.md": "b"})

        # Act / Assert
        with pytest.raises(KnowledgeFileNameCollisionError):
            service.pull("root")

    def test_pull_rejects_html_zip_traversal(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        build_zip: BuildZip,
        tmp_path: Path,
    ) -> None:
        # Arrange
        fake_docmost_client.exports = {
            ("root", "markdown"): build_zip({"Page.md": "![image](files/image.png)"}),
            ("root", "html"): build_zip({"../escape.html": "bad"}),
        }

        # Act
        with pytest.raises(UnsafeExportEntryError):
            service.pull("root")

        # Assert
        assert not (tmp_path / "output").exists()

    def test_push_creates_missing_replaces_changed_and_skips_unchanged(
        self,
        service: ConversionService,
        fake_claude_client: FakeClaudeClient,
        tmp_path: Path,
    ) -> None:
        # Arrange
        output = tmp_path / "output"
        output.mkdir()
        for name, content in {"new.md": "new", "changed.md": "updated", "same.md": "same"}.items():
            (output / name).write_text(content)
        fake_claude_client.docs = [
            ProjectDoc("changed", "changed.md", "old"),
            ProjectDoc("same", "same.md", "same"),
            ProjectDoc("extra", "extra.md", "keep"),
        ]

        # Act
        service.push("project")

        # Assert
        assert fake_claude_client.calls == [
            ("delete_doc", "project", "changed"),
            ("create_doc", "project", "changed.md", "updated"),
            ("create_doc", "project", "new.md", "new"),
        ]
        assert any(doc.id == "extra" for doc in fake_claude_client.docs)

    def test_push_replaces_pdf_without_pruning_other_files(
        self,
        service: ConversionService,
        fake_claude_client: FakeClaudeClient,
        tmp_path: Path,
    ) -> None:
        # Arrange
        output = tmp_path / "output"
        output.mkdir()
        (output / "image.pdf").write_bytes(b"%PDF-fake")
        fake_claude_client.files = [ProjectFile("old", "image.pdf"), ProjectFile("extra", "other.pdf")]

        # Act
        service.push("project")

        # Assert
        assert fake_claude_client.calls == [("upload_file", "project", "image.pdf"), ("delete_file", "project", "old")]
        assert any(file.id == "extra" for file in fake_claude_client.files)

    def test_push_without_output_fails_before_changing_the_project(
        self,
        service: ConversionService,
        fake_claude_client: FakeClaudeClient,
    ) -> None:
        # Act
        with pytest.raises(FileNotFoundError):
            service.push("project")

        # Assert
        assert fake_claude_client.calls == []

    def test_sync_prunes_extra_knowledge(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        fake_claude_client: FakeClaudeClient,
        build_zip: BuildZip,
    ) -> None:
        # Arrange
        fake_docmost_client.exports["root", "markdown"] = build_zip({"Page.md": "new"})
        fake_claude_client.docs = [ProjectDoc("extra", "extra.md", "delete")]
        fake_claude_client.files = [ProjectFile("extra-file", "extra.pdf")]

        # Act
        service.sync("project", "root")

        # Assert
        assert [(doc.file_name, doc.content) for doc in fake_claude_client.docs] == [("Page.md", "new")]
        assert fake_claude_client.files == []

    def test_sync_does_not_prune_if_push_fails(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        fake_claude_client: FakeClaudeClient,
        build_zip: BuildZip,
    ) -> None:
        # Arrange
        fake_docmost_client.exports["root", "markdown"] = build_zip({"Page.md": "new"})
        fake_claude_client.docs = [ProjectDoc("extra", "extra.md", "keep")]
        fake_claude_client.create_doc_error = RuntimeError("upload failed")

        # Act
        with pytest.raises(RuntimeError, match="upload failed"):
            service.sync("project", "root")

        # Assert
        assert fake_claude_client.docs == [ProjectDoc("extra", "extra.md", "keep")]

    def test_pull_rejects_names_that_only_differ_in_case(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        build_zip: BuildZip,
    ) -> None:
        # Arrange
        fake_docmost_client.exports["root", "markdown"] = build_zip({"Page.md": "a", "page.md": "b"})

        # Act / Assert
        with pytest.raises(KnowledgeFileNameCollisionError):
            service.pull("root")

    @pytest.mark.parametrize("page_path", [".env.md", "...md"])
    def test_pull_rejects_hidden_knowledge_file_names(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        build_zip: BuildZip,
        page_path: str,
    ) -> None:
        # Arrange
        fake_docmost_client.exports["root", "markdown"] = build_zip({page_path: "text"})

        # Act / Assert
        with pytest.raises(UnsafeKnowledgeFileNameError):
            service.pull("root")

    def test_pull_exports_html_once_and_renders_every_page_with_images(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        fake_pdf_renderer: FakePdfRenderer,
        build_zip: BuildZip,
        tmp_path: Path,
    ) -> None:
        # Arrange
        fake_docmost_client.exports["root", "markdown"] = build_zip(
            {"Root.md": "![a](files/a.png)", "Root/Child.md": "![b](files/b.png)"}
        )
        fake_docmost_client.exports["root", "html"] = build_zip({"Root.html": "<img>", "Root/Child.html": "<img>"})

        # Act
        service.pull("root")

        # Assert
        html_exports = [call for call in fake_docmost_client.calls if call[0] == "export_page" and call[2] == "html"]
        assert html_exports == [("export_page", "root", "html")]
        assert {file.name for file in (tmp_path / "output").iterdir()} == {"Root.pdf", "Root - Child.pdf"}
        assert len(fake_pdf_renderer.calls) == 2

    def test_pull_extracts_attachments_with_a_leading_slash_next_to_the_html(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        fake_pdf_renderer: FakePdfRenderer,
        build_zip: BuildZip,
        png: bytes,
    ) -> None:
        # Arrange
        fake_docmost_client.exports = {
            ("root", "markdown"): build_zip({"Page.md": "![image](files/image.png)"}),
            ("root", "html"): build_zip({"Page.html": "<img src='files/image.png'>", "/files/image.png": png}),
        }

        # Act
        service.pull("root")

        # Assert
        assert fake_pdf_renderer.files_next_to_html == [{"Page.html", "files/image.png"}]

    @pytest.mark.parametrize("kind", ["symlink", "directory"])
    def test_pull_refuses_to_replace_a_symlink_or_directory(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        build_zip: BuildZip,
        tmp_path: Path,
        kind: str,
    ) -> None:
        # Arrange
        fake_docmost_client.exports["root", "markdown"] = build_zip({"Page.md": "new"})
        output = tmp_path / "output"
        output.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("keep")
        if kind == "symlink":
            (output / "Page.md").symlink_to(outside)
        else:
            (output / "Page.md").mkdir()

        # Act
        with pytest.raises(UnsafeOutputFileError):
            service.pull("root")

        # Assert
        assert outside.read_text() == "keep"

    def test_push_accepts_a_file_of_exactly_the_size_limit(
        self,
        service: ConversionService,
        fake_claude_client: FakeClaudeClient,
        tmp_path: Path,
    ) -> None:
        # Arrange
        output = tmp_path / "output"
        output.mkdir()
        (output / "large.pdf").write_bytes(b"x" * _CLAUDE_MAX_FILE_SIZE_BYTES)

        # Act
        service.push("project")

        # Assert
        assert fake_claude_client.calls == [("upload_file", "project", "large.pdf")]

    def test_push_rejects_an_oversize_file_before_changing_the_project(
        self,
        service: ConversionService,
        fake_claude_client: FakeClaudeClient,
        tmp_path: Path,
    ) -> None:
        # Arrange
        output = tmp_path / "output"
        output.mkdir()
        (output / "a.md").write_text("small")
        (output / "large.pdf").write_bytes(b"x" * (_CLAUDE_MAX_FILE_SIZE_BYTES + 1))

        # Act
        with pytest.raises(KnowledgeFileTooLargeError):
            service.push("project")

        # Assert
        assert fake_claude_client.calls == []

    def test_push_replaces_duplicate_docs_when_any_copy_is_outdated(
        self,
        service: ConversionService,
        fake_claude_client: FakeClaudeClient,
        tmp_path: Path,
    ) -> None:
        # Arrange
        output = tmp_path / "output"
        output.mkdir()
        (output / "Page.md").write_text("new")
        fake_claude_client.docs = [ProjectDoc("current", "Page.md", "new"), ProjectDoc("outdated", "Page.md", "old")]

        # Act
        service.push("project")

        # Assert
        assert [(doc.file_name, doc.content) for doc in fake_claude_client.docs] == [("Page.md", "new")]

    def test_push_ignores_other_files_and_directories_in_the_output(
        self,
        service: ConversionService,
        fake_claude_client: FakeClaudeClient,
        tmp_path: Path,
    ) -> None:
        # Arrange
        output = tmp_path / "output"
        output.mkdir()
        (output / "Page.md").write_text("page")
        (output / "notes.txt").write_text("not knowledge")
        (output / "folder.md").mkdir()

        # Act
        service.push("project")

        # Assert
        assert fake_claude_client.calls == [("create_doc", "project", "Page.md", "page")]

    @pytest.mark.parametrize(
        ("markdown", "file_name"),
        [
            ("![diagram](files/1/diagram.png)", "Page.pdf"),
            ("Logo: ![](https://example.com/logo.png)", "Page.pdf"),
            ("See [the guide](Guide.md) and [Docmost](https://docmost.com).", "Page.md"),
        ],
        ids=["attached-image", "external-image", "links-only"],
    )
    def test_pull_renders_pages_with_images_to_pdf_and_keeps_other_pages_as_markdown(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        build_zip: BuildZip,
        tmp_path: Path,
        markdown: str,
        file_name: str,
    ) -> None:
        # Arrange
        fake_docmost_client.exports = {
            ("root", "markdown"): build_zip({"Page.md": markdown}),
            ("root", "html"): build_zip({"Page.html": "<img>"}),
        }

        # Act
        service.pull("root")

        # Assert
        assert [file.name for file in (tmp_path / "output").iterdir()] == [file_name]

    def test_pull_flattens_the_page_tree_into_knowledge_file_names(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        build_zip: BuildZip,
        tmp_path: Path,
    ) -> None:
        # Arrange
        fake_docmost_client.exports["root", "markdown"] = build_zip({"Handbook/Guides/Setup.md": "text"})

        # Act
        service.pull("root")

        # Assert
        assert [file.name for file in (tmp_path / "output").iterdir()] == ["Handbook - Guides - Setup.md"]

    def test_pull_points_page_links_at_knowledge_file_names(
        self,
        service: ConversionService,
        fake_docmost_client: FakeDocmostClient,
        build_zip: BuildZip,
        tmp_path: Path,
    ) -> None:
        # Arrange
        fake_docmost_client.exports["root", "markdown"] = build_zip(
            {
                "Handbook.md": "See [Onboarding](Handbook/Onboarding.md) and [notes](Team%20Notes.md).",
                "Handbook/Onboarding.md": "Back to [the handbook](../Handbook.md) or [elsewhere](Other.md).",
                "Team Notes.md": "Notes and [site](https://example.com/page.md).",
            }
        )

        # Act
        service.pull("root")

        # Assert
        output = tmp_path / "output"
        assert (output / "Handbook.md").read_text() == (
            "See [Onboarding](Handbook - Onboarding.md) and [notes](Team Notes.md)."
        )
        assert (output / "Handbook - Onboarding.md").read_text() == (
            "Back to [the handbook](Handbook.md) or [elsewhere](Other.md)."
        )
        assert (output / "Team Notes.md").read_text() == "Notes and [site](https://example.com/page.md)."
