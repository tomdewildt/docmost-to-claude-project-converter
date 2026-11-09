from pathlib import Path

import pytest
from pydantic import ValidationError

from docmost_to_claude_project_converter.config import ConverterConfig


@pytest.mark.usefixtures("environment_without_converter_settings")
class TestConverterConfig:
    def test_converter_config_requires_the_docmost_connection_settings(self) -> None:
        # Act
        with pytest.raises(ValidationError) as error:
            ConverterConfig(_env_file=None)  # type: ignore[call-arg]

        # Assert
        assert {location for problem in error.value.errors() for location in problem["loc"]} == {
            "DOCMOST_URL",
            "DOCMOST_EMAIL",
            "DOCMOST_PASSWORD",
        }

    def test_converter_config_loads_settings_from_dotenv_and_defaults_the_paths(self, tmp_path: Path) -> None:
        # Arrange
        env_file = tmp_path / ".env"
        env_file.write_text(
            "CONV_DOCMOST_URL=http://docmost.test\n"
            "CONV_DOCMOST_EMAIL=user@example.com\n"
            "CONV_DOCMOST_PASSWORD=password\n"
        )

        # Act
        config = ConverterConfig(_env_file=env_file)  # type: ignore[call-arg]

        # Assert
        assert config.DOCMOST_URL == "http://docmost.test"
        assert config.DOCMOST_OUTPUT_DIRECTORY == Path("data/output")
        assert config.CLAUDE_SESSION_PATH == Path("data/session.profile")
