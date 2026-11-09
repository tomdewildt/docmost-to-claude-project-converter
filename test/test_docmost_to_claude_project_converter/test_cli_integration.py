import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from docmost_to_claude_project_converter.config import config

from .conftest import DocmostPages, ImportPages


@pytest.mark.integration()
class TestCliIntegration:
    def test_pull_page_url_writes_the_subtree_to_the_output_argument(
        self,
        docmost_pages: DocmostPages,
        import_pages: ImportPages,
        png: bytes,
        tmp_path: Path,
    ) -> None:
        # Arrange
        title = f"CLI smoke {uuid.uuid4()}"
        import_pages(
            {
                "parent.md": f"# {title}\n\nParent text.\n",
                "parent/child.md": "# Illustrated child\n\n![diagram](image/diagram.png)\n",
                "parent/image/diagram.png": png,
            }
        )
        page = next(page for page in docmost_pages.root_pages() if page["title"] == title)
        page_url = f"{config.DOCMOST_URL}/s/unused/p/test-{page['slugId']}"
        output = tmp_path / "selected-output"
        output.mkdir()
        (output / "stale.md").write_text("stale")
        ignored_output = tmp_path / "ignored-output"
        missing_session = tmp_path / "missing-session.json"

        # Act
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "docmost_to_claude_project_converter.cli",
                "pull",
                page_url,
                "--output-directory",
                str(output),
            ],
            env={
                **os.environ,
                "CONV_DOCMOST_OUTPUT_DIRECTORY": str(ignored_output),
                "CONV_CLAUDE_SESSION_PATH": str(missing_session),
            },
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

        # Assert
        assert result.returncode == 0, result.stdout + result.stderr
        assert {file.name for file in output.iterdir()} == {f"{title}.md", f"{title} - Illustrated child.pdf"}
        assert "Parent text." in (output / f"{title}.md").read_text()
        assert (output / f"{title} - Illustrated child.pdf").read_bytes().startswith(b"%PDF-")
        assert not ignored_output.exists()
        assert not missing_session.exists()
