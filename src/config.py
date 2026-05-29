"""Central, typed configuration loaded from environment / `.env`.

All knobs live here so milestones can wire in DB, Langfuse, and models without
scattering `os.getenv` calls. Synthetic data only; never put real secrets that
matter in `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── App ──────────────────────────────────────────────────────────────────
    app_name: str = "clinical-claims-analyst"
    environment: str = Field(default="local", alias="ENVIRONMENT")
    # Public-deploy guardrails for /ask (0 = disabled). Bound token spend on a public URL.
    rate_limit_per_min: int = Field(default=0, alias="RATE_LIMIT_PER_MIN")
    max_queries_per_day: int = Field(default=0, alias="MAX_QUERIES_PER_DAY")

    # ── LLM (via LiteLLM) ──────────────────────────────────────────────────────
    # `llm_model` is a LiteLLM model string, e.g. "gpt-4o-mini", "openai/gpt-4o".
    llm_model: str = Field(default="gpt-4o-mini", alias="LLM_MODEL")
    # Optional stronger model for the hard step (SQL generation + self-correction). Empty
    # = use llm_model everywhere. The cheap model still does plan / synthesize / judge —
    # the spec's "one strong + one cheap" routing for a real cost/accuracy tradeoff.
    sql_model: str = Field(default="", alias="SQL_MODEL")
    llm_temperature: float = Field(default=0.0, alias="LLM_TEMPERATURE")
    llm_max_tokens: int = Field(default=512, alias="LLM_MAX_TOKENS")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    # When true, LiteLLM returns a canned response instead of calling the provider.
    # Lets the trace pipeline (and demos / CI) run without a billable key.
    llm_mock: bool = Field(default=False, alias="LLM_MOCK")

    # ── Langfuse (self-hosted observability) ───────────────────────────────────
    langfuse_enabled: bool = Field(default=True, alias="LANGFUSE_ENABLED")
    # Host the SDK posts traces to. On the host that's localhost:3000; inside the
    # docker network it's http://langfuse-web:3000 (set per-container in compose).
    langfuse_host: str = Field(default="http://localhost:3000", alias="LANGFUSE_HOST")
    # Browser-facing base URL used to build clickable trace links. Defaults to the
    # ingest host; override when ingest != browser URL (e.g. app runs in compose).
    langfuse_public_url: str = Field(default="", alias="LANGFUSE_PUBLIC_URL")
    langfuse_public_key: str | None = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(default=None, alias="LANGFUSE_SECRET_KEY")
    # Matches LANGFUSE_INIT_PROJECT_ID from the headless-seeded project; used only
    # to construct trace URLs.
    langfuse_project_id: str = Field(
        default="clinical-claims-analyst", alias="LANGFUSE_PROJECT_ID"
    )

    # ── Data backend ────────────────────────────────────────────────────────
    # "sqlite" = the M1 base-table copy (the test/CI default — hermetic).
    # "postgres" = the M2 de-identified views via the least-privilege role.
    data_backend: str = Field(default="sqlite", alias="DATA_BACKEND")
    sqlite_path: str = Field(default="data/synthea.db", alias="SQLITE_PATH")

    # Three separate Postgres roles/DSNs (read from env; never hardcoded):
    #   owner  — loads base tables, applies views.sql, owns the audit table.
    #   analyst_ro — the agent's ONLY read path: SELECT on the v_* views only.
    #   audit_writer — INSERT-only on the append-only access_audit table.
    analytics_owner_dsn: str | None = Field(default=None, alias="ANALYTICS_OWNER_DSN")
    analyst_ro_dsn: str | None = Field(default=None, alias="ANALYST_RO_DSN")
    audit_writer_dsn: str | None = Field(default=None, alias="AUDIT_WRITER_DSN")
    # Passwords for the roles the bootstrap creates (separate from the DSNs so the
    # bootstrap can CREATE ROLE; the DSNs embed them for runtime connections).
    analyst_ro_password: str | None = Field(default=None, alias="ANALYST_RO_PASSWORD")
    audit_writer_password: str | None = Field(default=None, alias="AUDIT_WRITER_PASSWORD")

    @property
    def langfuse_browser_url(self) -> str:
        return (self.langfuse_public_url or self.langfuse_host).rstrip("/")

    @property
    def langfuse_ready(self) -> bool:
        return bool(
            self.langfuse_enabled
            and self.langfuse_public_key
            and self.langfuse_secret_key
        )

    @property
    def llm_ready(self) -> bool:
        # M0 wires OpenAI specifically; other providers added later as needed.
        return bool(self.openai_api_key)

    @property
    def sql_model_resolved(self) -> str:
        return self.sql_model or self.llm_model

    @property
    def use_postgres(self) -> bool:
        return self.data_backend.lower() == "postgres"

    @property
    def pg_ready(self) -> bool:
        """The agent can use the Postgres views path."""
        return self.use_postgres and bool(self.analyst_ro_dsn)


@lru_cache
def get_settings() -> Settings:
    return Settings()
