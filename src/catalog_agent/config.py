"""Validated configuration (Tech Stack §5). Credentials come only from the environment."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SiteConfig(BaseModel):
    adapter: Literal["safco"] = "safco"
    allowed_hosts: list[str]
    scope: Literal["selected", "all"] = "selected"
    discovery_url: str
    category_roots: list[str]
    user_agent: str = "CatalogAgent-POC/0.1 (low-rate research crawler)"


class CrawlerConfig(BaseModel):
    concurrency: int = Field(2, ge=1, le=4)
    request_interval_seconds: float = Field(2.0, ge=1.0)
    timeout_seconds: float = Field(30, gt=0)
    readiness_timeout_seconds: float = Field(15, gt=0)
    max_attempts: int = Field(3, ge=1, le=10)
    backoff_base_seconds: float = Field(2.0, ge=0)
    backoff_max_seconds: float = Field(60.0, ge=0)
    max_pages: int = Field(150, ge=1)
    max_products_per_category: int = Field(10, ge=1)
    max_invocation_seconds: float = Field(3600, gt=0)


class ExtractorAgentConfig(BaseModel):
    llm_enabled: bool = True
    max_model_calls_per_task: int = Field(3, ge=0)
    max_tool_calls_per_task: int = Field(6, ge=0)
    max_seconds_per_task: float = Field(60, gt=0)
    attribute_gaps: bool = True


class ClassifierAgentConfig(BaseModel):
    llm_fallback_enabled: bool = False  # P1, not implemented
    max_model_calls_per_task: int = 1

    @field_validator("llm_fallback_enabled")
    @classmethod
    def _p1_disabled(cls, v: bool) -> bool:
        if v:
            raise ValueError("classifier LLM fallback is P1 and not implemented in this build")
        return v


class RecoveryAgentConfig(BaseModel):
    repair_suggestions_enabled: bool = False  # P1, not implemented
    trigger_after_failures: int = 3
    max_suggestions_per_run: int = 5

    @field_validator("repair_suggestions_enabled")
    @classmethod
    def _p1_disabled(cls, v: bool) -> bool:
        if v:
            raise ValueError("repair suggestions are P1 and not implemented in this build")
        return v


class AgentsConfig(BaseModel):
    extractor: ExtractorAgentConfig = ExtractorAgentConfig()
    classifier: ClassifierAgentConfig = ClassifierAgentConfig()
    recovery: RecoveryAgentConfig = RecoveryAgentConfig()


class LLMConfig(BaseModel):
    max_model_calls_per_run: int = Field(60, ge=0)
    max_input_tokens_per_call: int = Field(8000, gt=0)
    max_output_tokens_per_call: int = Field(4000, gt=0)
    max_tool_result_chars: int = Field(12000, gt=0)


class StorageConfig(BaseModel):
    database: str = "data/catalog.db"
    snapshot_dir: str = "data/snapshots"
    output_dir: str = "outputs"
    log_dir: str = "data/logs"


class AppConfig(BaseModel):
    site: SiteConfig
    crawler: CrawlerConfig = CrawlerConfig()
    agents: AgentsConfig = AgentsConfig()
    llm: LLMConfig = LLMConfig()
    storage: StorageConfig = StorageConfig()
    # Offline fixture replay instead of live fetching (tests, smoke script). Replay output is never live data.
    replay_dir: str | None = None


class Secrets(BaseSettings):
    """Model credentials, read from the environment or .env (never committed)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None


def load_config(path: str | Path) -> AppConfig:
    data = yaml.safe_load(Path(path).read_text())
    return AppConfig.model_validate(data)
