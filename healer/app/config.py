from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import EvalMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    langsmith_api_key: str = Field(alias="LANGSMITH_API_KEY")
    anthropic_api_key: str = Field(alias="ANTHROPIC_API_KEY")
    github_token: str = Field(alias="GITHUB_TOKEN")
    github_repo: str = Field(alias="GITHUB_REPO")
    slack_webhook_url: str = Field(alias="SLACK_WEBHOOK_URL")
    heal_threshold: float = Field(default=0.05, alias="HEAL_THRESHOLD")
    cron_schedule: str = Field(default="0 0 * * *", alias="CRON_SCHEDULE")

    eval_mode: EvalMode = Field(default=EvalMode.GITHUB_ACTIONS, alias="EVAL_MODE")
    eval_script_path: str = Field(default="./run_evals.sh", alias="EVAL_SCRIPT_PATH")

    github_tools_path: str = Field(alias="GITHUB_TOOLS_PATH")
    github_default_branch: str = Field(default="main", alias="GITHUB_DEFAULT_BRANCH")
    github_actions_workflow: str = Field(alias="GITHUB_ACTIONS_WORKFLOW")
    github_eval_workflow_ref: str = Field(default="main", alias="GITHUB_EVAL_WORKFLOW_REF")
    github_eval_poll_interval_seconds: int = Field(default=30, alias="GITHUB_EVAL_POLL_INTERVAL_SECONDS")
    github_eval_timeout_seconds: int = Field(default=1800, alias="GITHUB_EVAL_TIMEOUT_SECONDS")

    post_eval_langsmith_wait_seconds: int = Field(default=10, alias="POST_EVAL_LANGSMITH_WAIT_SECONDS")
    github_actions_extra_langsmith_wait_seconds: int = Field(
        default=20,
        alias="GITHUB_ACTIONS_EXTRA_LANGSMITH_WAIT_SECONDS",
    )

    langsmith_project_name: str = Field(alias="LANGSMITH_PROJECT_NAME")
    langsmith_dataset_name: str | None = Field(default=None, alias="LANGSMITH_DATASET_NAME")

    @field_validator("heal_threshold")
    @classmethod
    def validate_heal_threshold(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("HEAL_THRESHOLD must be between 0 and 1.")
        return value

    @field_validator(
        "github_eval_poll_interval_seconds",
        "github_eval_timeout_seconds",
        "post_eval_langsmith_wait_seconds",
        "github_actions_extra_langsmith_wait_seconds",
    )
    @classmethod
    def validate_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Duration values must be positive.")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

