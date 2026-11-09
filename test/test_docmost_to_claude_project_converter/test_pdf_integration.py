import io
import zipfile
from pathlib import Path

import pytest

from docmost_to_claude_project_converter.pdf import PatchrightPdfRenderer


@pytest.mark.integration()
class TestPatchrightPdfRendererIntegration:
    def test_render_creates_pdf_with_embedded_image_from_real_docmost_export(
        self, docmost_html_export: bytes, tmp_path: Path
    ) -> None:
        # Arrange
        with zipfile.ZipFile(io.BytesIO(docmost_html_export)) as archive:
            archive.extractall(tmp_path)

        # Act
        pdf = PatchrightPdfRenderer().render(tmp_path / "Handbook" / "Architecture.html")

        # Assert
        assert pdf.startswith(b"%PDF-")
        assert b"/Subtype /Image" in pdf
