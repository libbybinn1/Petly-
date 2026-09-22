"""Application configuration loaded from environment variables.

Every external dependency is configured here and nowhere else, so a deployment
is changed by editing `.env` rather than by editing code. Secrets are never
defaulted to a real value - a missing secret raises rather than silently
falling back to something insecure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

import truststore
from dotenv import load_dotenv

# The corporate network re-signs HTTPS with a Palo Alto "Forward Trust CA".
# Python's bundled certificate list does not contain that root, so outbound
# HTTPS (Tavily) fails verification unless we use the Windows trust store.
# This must run before any HTTPS client is constructed anywhere in the process.
truststore.inject_into_ssl()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")


class MissingConfigurationError(RuntimeError):
    """Raised when a required environment variable is absent."""


def _required(variable_name: str) -> str:
    """Read an environment variable that has no safe default.

    Args:
        variable_name: The environment variable to read.

    Returns:
        The variable's value.

    Raises:
        MissingConfigurationError: If the variable is unset or empty.
    """
    value = os.environ.get(variable_name, "").strip()
    if not value:
        raise MissingConfigurationError(
            f"{variable_name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _optional(variable_name: str, default: str) -> str:
    """Read an environment variable that has a safe default."""
    return os.environ.get(variable_name, "").strip() or default


@dataclass(frozen=True)
class DatabaseConfiguration:
    """Connection settings for the cloud SQL Server database."""

    server: str
    database: str
    user: str
    password: str

    @property
    def sqlalchemy_url(self) -> str:
        """Build the SQLAlchemy URL, percent-encoding credentials.

        Passwords routinely contain characters that are meaningful inside a
        URL, so both credentials are quoted rather than interpolated raw.
        """
        safe_user = quote_plus(self.user)
        safe_password = quote_plus(self.password)
        return f"mssql+pymssql://{safe_user}:{safe_password}@{self.server}/{self.database}"


@dataclass(frozen=True)
class AgentConfiguration:
    """Settings for the independent agent process and its models."""

    ollama_base_url: str
    chat_model: str
    fast_chat_model: str
    embedding_model: str
    tavily_api_key: str
    poll_interval_seconds: int
    max_reasoning_steps: int

    @property
    def has_web_search(self) -> bool:
        """Whether a live web-search provider is configured.

        When false the agent falls back to a stub provider so the system and
        its tests still run offline.
        """
        return bool(self.tavily_api_key)


@dataclass(frozen=True)
class Configuration:
    """Complete application configuration."""

    database: DatabaseConfiguration
    agent: AgentConfiguration
    secret_key: str
    flask_port: int
    is_development: bool
    chroma_persist_directory: Path
    rag_collection_name: str
    upload_directory: Path
    invitation_expiry_hours: int

    @property
    def sqlalchemy_url(self) -> str:
        """The database URL, honouring a local override when one is set.

        `LOCAL_DATABASE_URL` lets a developer work offline against SQLite
        without touching the cloud configuration.
        """
        local_override = os.environ.get("LOCAL_DATABASE_URL", "").strip()
        return local_override or self.database.sqlalchemy_url


def load_configuration() -> Configuration:
    """Build the configuration object from the current environment."""
    database = DatabaseConfiguration(
        server=_required("DB_SERVER"),
        database=_required("DB_NAME"),
        user=_required("DB_USER"),
        password=_required("DB_PASSWORD"),
    )

    agent = AgentConfiguration(
        ollama_base_url=_optional("OLLAMA_BASE_URL", "http://localhost:11434"),
        chat_model=_optional("OLLAMA_CHAT_MODEL", "qwen2.5:3b-instruct"),
        fast_chat_model=_optional("OLLAMA_CHAT_MODEL_FAST", "qwen2.5:3b-instruct"),
        embedding_model=_optional("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        tavily_api_key=os.environ.get("TAVILY_API_KEY", "").strip(),
        poll_interval_seconds=int(_optional("AGENT_POLL_INTERVAL_SECONDS", "3")),
        max_reasoning_steps=int(_optional("AGENT_MAX_REASONING_STEPS", "8")),
    )

    return Configuration(
        database=database,
        agent=agent,
        secret_key=_required("FLASK_SECRET_KEY"),
        flask_port=int(_optional("FLASK_PORT", "5000")),
        is_development=_optional("FLASK_ENV", "development") == "development",
        chroma_persist_directory=PROJECT_ROOT / _optional("CHROMA_PERSIST_DIR", "data/chroma"),
        rag_collection_name=_optional("RAG_COLLECTION_NAME", "petmatch_knowledge"),
        upload_directory=PROJECT_ROOT / "app" / "static" / "uploads",
        invitation_expiry_hours=int(_optional("INVITATION_EXPIRY_HOURS", "72")),
    )
