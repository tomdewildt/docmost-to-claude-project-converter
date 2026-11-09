from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ConverterConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONV_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DOCMOST_URL: str
    DOCMOST_EMAIL: str
    DOCMOST_PASSWORD: str
    DOCMOST_OUTPUT_DIRECTORY: Path = Path("data/output")
    CLAUDE_SESSION_PATH: Path = Path("data/session.profile")


config = ConverterConfig()  # type: ignore
