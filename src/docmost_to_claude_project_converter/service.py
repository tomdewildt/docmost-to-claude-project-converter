import io
import posixpath
import re
import zipfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.parse import unquote

from loguru import logger

from docmost_to_claude_project_converter.claude import (
    ClaudeClient,
    ProjectDoc,
    ProjectFile,
    ensure_file_size_is_within_claude_limits,
)
from docmost_to_claude_project_converter.docmost import DocmostClient
from docmost_to_claude_project_converter.exceptions import (
    KnowledgeFileNameCollisionError,
    UnsafeExportEntryError,
    UnsafeKnowledgeFileNameError,
    UnsafeOutputFileError,
)
from docmost_to_claude_project_converter.pdf import PdfRenderer

_KNOWLEDGE_FILE_NAME_SEPARATOR = " - "
_KNOWLEDGE_FILE_SUFFIXES = (".md", ".pdf")

# Image in the markdown: ![diagram](files/<id>/diagram.png)
_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]\n]*\]\([^)\n]+\)")

# Link to another page of the export, relative to the linking page: [Onboarding](Handbook/Onboarding.md)
_MARKDOWN_PAGE_LINK_PATTERN = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+\.md)\)")


class ConversionService:
    def __init__(
        self,
        docmost_client: DocmostClient,
        claude_client: ClaudeClient,
        pdf_renderer: PdfRenderer,
        output_directory: Path,
    ) -> None:
        self._docmost_client = docmost_client
        self._claude_client = claude_client
        self._pdf_renderer = pdf_renderer
        self._output_directory = output_directory

    def login(self) -> None:
        self._claude_client.login()

    def pull(self, page_id: str) -> None:
        root_page_id = self._docmost_client.get_page_id(page_id)
        markdown_by_page_path = self._unzip_markdown_export(self._docmost_client.export_page(root_page_id, "markdown"))

        file_name_by_page_path = self._build_knowledge_file_names(markdown_by_page_path)
        self._ensure_knowledge_file_names_are_unique(list(file_name_by_page_path.values()))
        self._ensure_knowledge_file_names_are_safe(list(file_name_by_page_path.values()))

        markdown_files = self._build_markdown_files(markdown_by_page_path, file_name_by_page_path)
        pdf_files = self._build_pdf_files(root_page_id, markdown_by_page_path, file_name_by_page_path)

        self._write_local_knowledge_files(markdown_files | pdf_files)

    def push(self, project_id: str) -> None:
        # Read and validate every file before changing the project, especially before replacing existing knowledge.
        content_by_file_name = {file.name: file.read_bytes() for file in self._list_local_knowledge_files()}
        for file_name, content in content_by_file_name.items():
            ensure_file_size_is_within_claude_limits(file_name, content)

        project_docs = self._claude_client.get_docs(project_id)
        project_files = self._claude_client.get_files(project_id)

        for file_name, content in content_by_file_name.items():
            if file_name.endswith(".md"):
                self._push_markdown_file(project_id, file_name, content.decode(), project_docs)
            else:
                self._push_pdf_file(project_id, file_name, content, project_files)

    def sync(self, project_id: str, page_id: str) -> None:
        self.pull(page_id)
        self.push(project_id)
        self._remove_claude_knowledge_files_not_in_local_directory(project_id)

    @staticmethod
    def _unzip_markdown_export(markdown_export: bytes) -> dict[str, str]:
        markdown_files: dict[str, str] = {}

        with zipfile.ZipFile(io.BytesIO(markdown_export)) as archive:
            for file_name in archive.namelist():
                if file_name.endswith(".md"):
                    markdown_files[file_name.removesuffix(".md")] = archive.read(file_name).decode()

        return markdown_files

    @staticmethod
    def _unzip_html_export(html_export: bytes) -> dict[str, bytes]:
        html_files: dict[str, bytes] = {}

        with zipfile.ZipFile(io.BytesIO(html_export)) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue

                # Docmost attachment names may start with '/', but are always relative to the export root.
                file_path = entry.filename.lstrip("/")
                if ".." in PurePosixPath(file_path).parts or "\\" in file_path:
                    raise UnsafeExportEntryError(entry.filename)

                html_files[file_path] = archive.read(entry)

        return html_files

    def _build_knowledge_file_names(self, markdown_by_page_path: dict[str, str]) -> dict[str, str]:
        knowledge_file_names: dict[str, str] = {}

        for page_path, markdown in markdown_by_page_path.items():
            # Claude cannot see images in markdown, so pages with images become pdf's
            suffix = ".pdf" if self._has_markdown_text_images(markdown) else ".md"

            knowledge_file_names[page_path] = (
                _KNOWLEDGE_FILE_NAME_SEPARATOR.join(PurePosixPath(page_path).parts) + suffix
            )

        return knowledge_file_names

    @staticmethod
    def _has_markdown_text_images(markdown: str) -> bool:
        return _MARKDOWN_IMAGE_PATTERN.search(markdown) is not None

    @staticmethod
    def _ensure_knowledge_file_names_are_unique(file_names: list[str]) -> None:
        if len({file_name.casefold() for file_name in file_names}) != len(file_names):
            raise KnowledgeFileNameCollisionError()

    @staticmethod
    def _ensure_knowledge_file_names_are_safe(file_names: list[str]) -> None:
        for file_name in file_names:
            if Path(file_name).name != file_name or file_name.startswith("."):
                raise UnsafeKnowledgeFileNameError(file_name)

    def _build_markdown_files(
        self,
        markdown_by_page_path: dict[str, str],
        file_name_by_page_path: dict[str, str],
    ) -> dict[str, bytes]:
        markdown_files: dict[str, bytes] = {}

        for page_path, markdown in markdown_by_page_path.items():
            if self._has_markdown_text_images(markdown):
                continue

            markdown_files[file_name_by_page_path[page_path]] = (
                self._replace_markdown_page_links_with_knowledge_file_names(
                    markdown,
                    page_path,
                    file_name_by_page_path,
                ).encode()
            )

        return markdown_files

    @staticmethod
    def _replace_markdown_page_links_with_knowledge_file_names(
        markdown: str,
        page_path: str,
        file_name_by_page_path: dict[str, str],
    ) -> str:
        page_directory = posixpath.dirname(page_path)

        def convert_to_knowledge_file_link(page_link: re.Match[str]) -> str:
            link_text, link_target = page_link.groups()
            target_page_path = posixpath.normpath(posixpath.join(page_directory, unquote(link_target)))
            target_file_name = file_name_by_page_path.get(target_page_path.removesuffix(".md"))
            return f"[{link_text}]({target_file_name})" if target_file_name else page_link.group(0)

        return _MARKDOWN_PAGE_LINK_PATTERN.sub(convert_to_knowledge_file_link, markdown)

    def _build_pdf_files(
        self,
        root_page_id: str,
        markdown_by_page_path: dict[str, str],
        file_name_by_page_path: dict[str, str],
    ) -> dict[str, bytes]:
        page_paths = [
            page_path
            for page_path, markdown in markdown_by_page_path.items()
            if self._has_markdown_text_images(markdown)
        ]
        if not page_paths:
            return {}

        html_export = self._unzip_html_export(self._docmost_client.export_page(root_page_id, "html"))
        with TemporaryDirectory() as temporary_directory:
            html_directory = Path(temporary_directory)

            # The browser that renders a page to pdf reads it from disk and needs its images next to it.
            for file_path, content in html_export.items():
                destination = html_directory / file_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)

            pdf_files: dict[str, bytes] = {}

            for page_path in page_paths:
                file_name = file_name_by_page_path[page_path]
                html_file = html_directory / f"{page_path}.html"
                pdf_files[file_name] = self._pdf_renderer.render(html_file)

            return pdf_files

    def _list_local_knowledge_files(self) -> list[Path]:
        return sorted(
            file
            for file in self._output_directory.iterdir()
            if file.is_file() and file.suffix in _KNOWLEDGE_FILE_SUFFIXES
        )

    def _write_local_knowledge_files(self, knowledge_files: dict[str, bytes]) -> None:
        self._output_directory.mkdir(parents=True, exist_ok=True)
        for file_name in knowledge_files:
            destination = self._output_directory / file_name
            if destination.is_symlink() or destination.is_dir():
                raise UnsafeOutputFileError(destination)

        for file in self._list_local_knowledge_files():
            if file.name not in knowledge_files:
                file.unlink()

        for file_name, content in knowledge_files.items():
            (self._output_directory / file_name).write_bytes(content)
            logger.info("Wrote '{file_name}'", file_name=file_name)

    def _push_markdown_file(
        self,
        project_id: str,
        file_name: str,
        markdown: str,
        project_docs: list[ProjectDoc],
    ) -> None:
        existing_docs = [doc for doc in project_docs if doc.file_name == file_name]
        if existing_docs and all(doc.content == markdown for doc in existing_docs):
            return

        for doc in existing_docs:
            self._claude_client.delete_doc(project_id, doc.id)

        self._claude_client.create_doc(project_id, file_name, markdown)
        logger.info("Pushed '{file_name}'", file_name=file_name)

    def _push_pdf_file(
        self,
        project_id: str,
        file_name: str,
        pdf: bytes,
        project_files: list[ProjectFile],
    ) -> None:
        self._claude_client.upload_file(project_id, file_name, pdf)

        for file in project_files:
            if file.file_name == file_name:
                self._claude_client.delete_file(project_id, file.id)

        logger.info("Pushed '{file_name}'", file_name=file_name)

    def _remove_claude_knowledge_files_not_in_local_directory(self, project_id: str) -> None:
        local_file_names = {file.name for file in self._list_local_knowledge_files()}

        for doc in self._claude_client.get_docs(project_id):
            if doc.file_name not in local_file_names:
                self._claude_client.delete_doc(project_id, doc.id)

        for file in self._claude_client.get_files(project_id):
            if file.file_name not in local_file_names:
                self._claude_client.delete_file(project_id, file.id)
