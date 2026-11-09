from abc import ABC, abstractmethod
from pathlib import Path

from loguru import logger
from patchright.sync_api import sync_playwright

_PRINT_STYLESHEET = """
body {
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.5;
}

img {
    max-width: 100%;
    height: auto;
}

table {
    border-collapse: collapse;
}

th,
td {
    border: 1px solid #ccc;
    padding: 4px 8px;
}

pre,
code {
    font-family: Menlo, Consolas, monospace;
    background: #f5f5f5;
}

pre {
    padding: 8px;
    white-space: pre-wrap;
}
"""


class PdfRenderer(ABC):
    @abstractmethod
    def render(self, html_path: Path) -> bytes: ...


class PatchrightPdfRenderer(PdfRenderer):
    def render(self, html_path: Path) -> bytes:
        logger.info("Rendering '{name}' as pdf", name=html_path.stem)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            # A file url, unlike set_content, gives the page a base url so the relative image paths resolve.
            page.goto(html_path.as_uri())
            page.add_style_tag(content=_PRINT_STYLESHEET)
            pdf = page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "2cm", "bottom": "2cm", "left": "2cm", "right": "2cm"},
            )
            browser.close()

        return pdf
