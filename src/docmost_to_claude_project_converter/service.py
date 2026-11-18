import csv
import io
import json
import posixpath
import re
import zipfile
from html import escape, unescape
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

_DOCMOST_METADATA_FILE_NAME = "docmost-metadata.json"
_KNOWLEDGE_FILE_NAME_SEPARATOR = " - "
_KNOWLEDGE_FILE_SUFFIXES = (".md", ".pdf")

# Image in the markdown: ![diagram](files/<id>/diagram.png)
_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]\n]*\]\([^)\n]+\)")

# Link to another page of the export, relative to the linking page: [Onboarding](Handbook/Onboarding.md)
_MARKDOWN_PAGE_LINK_PATTERN = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+\.md)\)")

# Base embedded in the html export: <div data-page-id="01a1ef78-..." data-type="base-embed"></div>
_HTML_BASE_EMBED_PATTERN = re.compile(r"<div[^>]*data-type=\"base-embed\"[^>]*>\s*</div>")
_HTML_BASE_EMBED_PAGE_ID_PATTERN = re.compile(r"data-page-id=\"([^\"]+)\"")
_HTML_HEADING_OR_BASE_EMBED_PATTERN = re.compile(
    rf"<h(?P<level>[1-6])[^>]*>(?P<text>.*?)</h(?P=level)>|{_HTML_BASE_EMBED_PATTERN.pattern}",
    re.DOTALL,
)


def _normalize_heading_text(heading_text: str) -> str:
    # Html and markdown headings differ in tags, entities, emphasis markers and whitespace around emoji.
    plain_text = unescape(re.sub(r"<[^>]+>", "", heading_text))
    return " ".join(re.sub(r"[*_`]", "", plain_text).split())


def _find_markdown_section_end(markdown_lines: list[str], heading: tuple[int, str] | None) -> int:
    if heading is None:
        return len(markdown_lines)

    # ponytail: ignores code fences, a '# comment' line in a code block can end a section early.
    level, text = heading
    heading_index = next(
        (
            index
            for index, line in enumerate(markdown_lines)
            if line.startswith("#" * level + " ") and _normalize_heading_text(line[level + 1 :]) == text
        ),
        None,
    )
    if heading_index is None:
        return len(markdown_lines)

    next_heading_pattern = re.compile(rf"#{{1,{level}}} ")
    return next(
        (
            index
            for index in range(heading_index + 1, len(markdown_lines))
            if next_heading_pattern.match(markdown_lines[index])
        ),
        len(markdown_lines),
    )


def _convert_csv_to_html_table(csv_text: str) -> str:
    rows = [
        ["<br>".join(escape(line) for line in cell.splitlines()) for cell in row]
        for row in csv.reader(io.StringIO(csv_text))
    ]
    if not rows:
        return ""

    header, *body = rows
    header_html = "".join(f"<th>{cell}</th>" for cell in header)
    body_html = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in body)
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>"


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
        markdown_export = self._docmost_client.export_page(root_page_id, "markdown")
        markdown_by_page_path = self._unzip_markdown_export(markdown_export)
        page_id_by_page_path = self._unzip_page_ids(markdown_export)
        base_csv_by_page_id = self._export_base_csvs(markdown_by_page_path, page_id_by_page_path)

        # Docmost only exports images and base embeds as html, the markdown export drops their position.
        has_images = any(self._has_markdown_text_images(markdown) for markdown in markdown_by_page_path.values())
        html_export = (
            self._unzip_html_export(self._docmost_client.export_page(root_page_id, "html"))
            if has_images or base_csv_by_page_id
            else {}
        )
        markdown_by_page_path = self._add_base_tables(
            markdown_by_page_path,
            page_id_by_page_path,
            base_csv_by_page_id,
            html_export,
        )

        file_name_by_page_path = self._build_knowledge_file_names(markdown_by_page_path)
        self._ensure_knowledge_file_names_are_unique(list(file_name_by_page_path.values()))
        self._ensure_knowledge_file_names_are_safe(list(file_name_by_page_path.values()))

        markdown_files = self._build_markdown_files(markdown_by_page_path, file_name_by_page_path)
        pdf_files = self._build_pdf_files(
            markdown_by_page_path,
            file_name_by_page_path,
            html_export,
            base_csv_by_page_id,
        )

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
    def _unzip_page_ids(markdown_export: bytes) -> dict[str, str]:
        with zipfile.ZipFile(io.BytesIO(markdown_export)) as archive:
            if _DOCMOST_METADATA_FILE_NAME not in archive.namelist():
                return {}
            pages = json.loads(archive.read(_DOCMOST_METADATA_FILE_NAME))["pages"]

        # Unlike the zip entries, the metadata paths are url encoded: Customer%20Hubs/Users.md
        return {unquote(page_path).removesuffix(".md"): page["pageId"] for page_path, page in pages.items()}

    def _export_base_csvs(
        self,
        markdown_by_page_path: dict[str, str],
        page_id_by_page_path: dict[str, str],
    ) -> dict[str, str]:
        # Docmost exports a base as only its title, so only title-only pages can be bases.
        base_page_ids = [
            page_id
            for page_path, page_id in page_id_by_page_path.items()
            if "\n" not in markdown_by_page_path.get(page_path, "\n").strip() and self._docmost_client.is_base(page_id)
        ]
        return {page_id: self._docmost_client.export_base(page_id) for page_id in base_page_ids}

    def _add_base_tables(
        self,
        markdown_by_page_path: dict[str, str],
        page_id_by_page_path: dict[str, str],
        base_csv_by_page_id: dict[str, str],
        html_export: dict[str, bytes],
    ) -> dict[str, str]:
        base_table_by_page_id = {
            page_id: self._convert_csv_to_markdown_table(csv_text) for page_id, csv_text in base_csv_by_page_id.items()
        }
        markdown_with_base_tables: dict[str, str] = {}

        for page_path, markdown in markdown_by_page_path.items():
            own_base_table = base_table_by_page_id.get(page_id_by_page_path.get(page_path, ""))
            if own_base_table is not None:
                markdown_with_base_tables[page_path] = f"{markdown.strip()}\n\n{own_base_table}\n"
                continue

            page_html = html_export.get(f"{page_path}.html", b"").decode()
            markdown_with_base_tables[page_path] = self._insert_embedded_base_tables(
                markdown,
                page_html,
                base_table_by_page_id,
            )

        return markdown_with_base_tables

    @staticmethod
    def _insert_embedded_base_tables(markdown: str, page_html: str, base_table_by_page_id: dict[str, str]) -> str:
        markdown_lines = markdown.split("\n")
        heading: tuple[int, str] | None = None

        # Walk the html in document order, so each base lands at the end of the markdown section it is embedded in.
        for element in _HTML_HEADING_OR_BASE_EMBED_PATTERN.finditer(page_html):
            if element.group("level"):
                heading = (int(element.group("level")), _normalize_heading_text(element.group("text")))
                continue

            page_id = _HTML_BASE_EMBED_PAGE_ID_PATTERN.search(element.group(0))
            base_table = base_table_by_page_id.get(page_id.group(1)) if page_id else None
            if base_table is None:
                continue

            line_index = _find_markdown_section_end(markdown_lines, heading)
            needs_blank_line = line_index > 0 and markdown_lines[line_index - 1].strip() != ""
            markdown_lines[line_index:line_index] = ([""] if needs_blank_line else []) + [base_table, ""]

        return "\n".join(markdown_lines)

    @staticmethod
    def _replace_html_base_embeds_with_tables(page_html: str, base_csv_by_page_id: dict[str, str]) -> str:
        def convert_to_html_table(base_embed: re.Match[str]) -> str:
            page_id = _HTML_BASE_EMBED_PAGE_ID_PATTERN.search(base_embed.group(0))
            csv_text = base_csv_by_page_id.get(page_id.group(1)) if page_id else None
            return _convert_csv_to_html_table(csv_text) if csv_text is not None else base_embed.group(0)

        return _HTML_BASE_EMBED_PATTERN.sub(convert_to_html_table, page_html)

    @staticmethod
    def _convert_csv_to_markdown_table(csv_text: str) -> str:
        rows = [
            ["<br>".join(cell.replace("|", "\\|").splitlines()) for cell in row]
            for row in csv.reader(io.StringIO(csv_text))
        ]
        if not rows:
            return ""

        header, *body = rows
        return "\n".join(f"| {' | '.join(row)} |" for row in [header, ["---"] * len(header), *body])

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
        markdown_by_page_path: dict[str, str],
        file_name_by_page_path: dict[str, str],
        html_export: dict[str, bytes],
        base_csv_by_page_id: dict[str, str],
    ) -> dict[str, bytes]:
        page_paths = [
            page_path
            for page_path, markdown in markdown_by_page_path.items()
            if self._has_markdown_text_images(markdown)
        ]
        if not page_paths:
            return {}

        with TemporaryDirectory() as temporary_directory:
            html_directory = Path(temporary_directory)

            # The browser that renders a page to pdf reads it from disk and needs its images next to it.
            for file_path, content in html_export.items():
                if file_path.endswith(".html"):
                    content = self._replace_html_base_embeds_with_tables(
                        content.decode(),
                        base_csv_by_page_id,
                    ).encode()
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
