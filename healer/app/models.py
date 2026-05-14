from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EvalMode(str, Enum):
    LOCAL = "local"
    GITHUB_ACTIONS = "github_actions"


class BucketType(str, Enum):
    PROMPT = "prompt"
    TOOL_SCHEMA = "tool_schema"
    TOOL_DESCRIPTION = "tool_description"
    WIRING_CODE = "wiring_code"


class ToolScore(BaseModel):
    tool_name: str
    score: float = Field(ge=0.0, le=1.0)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FailingExample(BaseModel):
    example_id: str
    tool_name: str
    input_payload: dict[str, Any]
    expected_output: str | None = None
    actual_output: str | None = None
    trace_id: str | None = None
    error_message: str | None = None


class DiffProposal(BaseModel):
    bucket: BucketType
    file_path: str
    diff: str
    reasoning: str


class CandidateChange(BaseModel):
    tool_name: str
    file_path: str
    branch_name: str
    commit_sha: str
    proposal: DiffProposal


class EvalRunResult(BaseModel):
    mode: EvalMode
    succeeded: bool
    run_identifier: str | None = None
    details: str | None = None
    completed_at: datetime = Field(default_factory=datetime.utcnow)


class ToolHealResult(BaseModel):
    tool_name: str
    status: str
    bucket: BucketType | None = None
    old_score: float | None = None
    new_score: float | None = None
    improvement: float | None = None
    proposal_reasoning: str | None = None
    pr_url: str | None = None
    anomaly_flagged: bool = False
    error: str | None = None


class HealRunState(BaseModel):
    run_id: str
    mode: EvalMode
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    tool_results: list[ToolHealResult] = Field(default_factory=list)
    error: str | None = None


class HealSummaryResponse(BaseModel):
    last_run: HealRunState | None = None


class ScoresResponse(BaseModel):
    scores: list[ToolScore]


class AnomaliesResponse(BaseModel):
    tools: list[str]

