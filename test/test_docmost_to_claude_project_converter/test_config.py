from pathlib import Path

import pytest
from pydantic import ValidationError

from docmost_to_claude_project_converter.config import ConverterConfig


@pytest.mark.usefixtures("environment_without_converter_settings")
class TestConverterConfig:
    def test_converter_config_requires_the_docmost_url(self) -> None:
        # Act
        with pytest.raises(ValidationError) as error:
            ConverterConfig(_env_file=None, DOCMOST_API_KEY="secret")  # type: ignore[call-arg]

        # Assert
        assert {location for problem in error.value.errors() for location in problem["loc"]} == {"DOCMOST_URL"}

    @pytest.mark.parametrize(
        "credentials",
        [{}, {"DOCMOST_EMAIL": "user@example.com"}, {"DOCMOST_PASSWORD": "password"}],
        ids=["none", "email-only", "password-only"],
    )
    def test_converter_config_requires_an_api_key_or_email_and_password(self, credentials: dict[str, str]) -> None:
        # Act / Assert
        with pytest.raises(ValidationError, match="CONV_DOCMOST_API_KEY"):
            ConverterConfig(_env_file=None, DOCMOST_URL="http://docmost.test", **credentials)  # type: ignore[call-arg]

    def test_converter_config_accepts_an_api_key_without_email_and_password(self) -> None:
        # Act
        config = ConverterConfig(_env_file=None, DOCMOST_URL="http://docmost.test", DOCMOST_API_KEY="secret")  # type: ignore[call-arg]

        # Assert
        assert config.DOCMOST_API_KEY == "secret"

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
