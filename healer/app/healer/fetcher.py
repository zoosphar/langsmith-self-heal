from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import structlog
from langsmith import Client

from app.config import Settings
from app.models import FailingExample, ToolScore

logger = structlog.get_logger(__name__)


@dataclass
class LangSmithFetcher:
    settings: Settings

    def __post_init__(self) -> None:
        self._client = Client(api_key=self.settings.langsmith_api_key)

    def fetch_scores(self) -> dict[str, ToolScore]:
        """
        Fetch latest score per tool from recent runs in the configured project.
        """
        score_by_tool: dict[str, ToolScore] = {}
        try:
            runs = self._client.list_runs(
                project_name=self.settings.langsmith_project_name,
                limit=200,
            )
        except Exception as exc:  # pragma: no cover - external SDK/network
            logger.exception("langsmith_list_runs_failed", error=str(exc))
            return score_by_tool

        for run in runs:
            tool_name = self._extract_tool_name(run)
            if not tool_name:
                continue
            score = self._extract_score(run)
            if score is None:
                continue

            existing = score_by_tool.get(tool_name)
            run_time = self._extract_time(run)
            if existing is None or run_time >= existing.updated_at:
                score_by_tool[tool_name] = ToolScore(
                    tool_name=tool_name,
                    score=score,
                    updated_at=run_time,
                )
        return score_by_tool

    def fetch_failing_examples(self, tool_name: str, max_items: int = 15) -> list[FailingExample]:
        """
        Fetch failing examples for a tool using run-level evidence and dataset context.
        """
        failures: list[FailingExample] = []
        dataset_examples = self._dataset_examples_by_id()

        try:
            runs = self._client.list_runs(
                project_name=self.settings.langsmith_project_name,
                limit=200,
            )
        except Exception as exc:  # pragma: no cover - external SDK/network
            logger.exception("langsmith_list_runs_failed_for_failures", tool_name=tool_name, error=str(exc))
            return failures

        for run in runs:
            run_tool = self._extract_tool_name(run)
            if run_tool != tool_name:
                continue

            score = self._extract_score(run)
            has_error = bool(getattr(run, "error", None))
            if score is not None and score >= 1.0 and not has_error:
                continue

            example_id = str(getattr(run, "reference_example_id", "") or getattr(run, "id", "unknown"))
            dataset_example = dataset_examples.get(example_id, {})
            inputs = self._as_dict(getattr(run, "inputs", None)) or self._as_dict(dataset_example.get("inputs"))
            outputs = self._as_dict(getattr(run, "outputs", None)) or self._as_dict(dataset_example.get("outputs"))

            failures.append(
                FailingExample(
                    example_id=example_id,
                    tool_name=tool_name,
                    input_payload=inputs,
                    expected_output=str(outputs.get("expected")) if outputs else None,
                    actual_output=str(outputs.get("actual")) if outputs else None,
                    trace_id=str(getattr(run, "trace_id", "")) or None,
                    error_message=str(getattr(run, "error", "")) or None,
                )
            )
            if len(failures) >= max_items:
                break
        return failures

    def _dataset_examples_by_id(self) -> dict[str, dict[str, Any]]:
        dataset_name = self.settings.langsmith_dataset_name
        if not dataset_name:
            return {}

        examples_by_id: dict[str, dict[str, Any]] = {}
        try:
            examples = self._client.list_examples(dataset_name=dataset_name)
        except Exception as exc:  # pragma: no cover - external SDK/network
            logger.exception("langsmith_list_examples_failed", error=str(exc))
            return {}

        for item in examples:
            example_id = str(getattr(item, "id", ""))
            if not example_id:
                continue
            examples_by_id[example_id] = {
                "inputs": self._as_dict(getattr(item, "inputs", None)),
                "outputs": self._as_dict(getattr(item, "outputs", None)),
            }
        return examples_by_id

    @staticmethod
    def _extract_tool_name(run: Any) -> str | None:
        metadata = getattr(run, "extra", {}) or {}
        if isinstance(metadata, dict):
            for key in ("tool_name", "tool", "name"):
                value = metadata.get(key)
                if isinstance(value, str) and value:
                    return value
            nested_meta = metadata.get("metadata")
            if isinstance(nested_meta, dict):
                value = nested_meta.get("tool_name") or nested_meta.get("tool")
                if isinstance(value, str) and value:
                    return value

        name = getattr(run, "name", None)
        if isinstance(name, str) and name:
            return name
        return None

    @staticmethod
    def _extract_score(run: Any) -> float | None:
        feedback_stats = getattr(run, "feedback_stats", None)
        if isinstance(feedback_stats, dict):
            candidates = LangSmithFetcher._extract_numeric_values(feedback_stats.values())
            if candidates:
                value = candidates[0]
                if 0.0 <= value <= 1.0:
                    return value
                if 0.0 <= value <= 100.0:
                    return value / 100.0
        return None

    @staticmethod
    def _extract_numeric_values(values: Iterable[Any]) -> list[float]:
        extracted: list[float] = []
        for value in values:
            if isinstance(value, (int, float)):
                extracted.append(float(value))
            elif isinstance(value, dict):
                for nested_key in ("avg", "mean", "score", "value"):
                    nested = value.get(nested_key)
                    if isinstance(nested, (int, float)):
                        extracted.append(float(nested))
        return extracted

    @staticmethod
    def _extract_time(run: Any) -> datetime:
        ended_at = getattr(run, "end_time", None)
        if isinstance(ended_at, datetime):
            return ended_at
        started_at = getattr(run, "start_time", None)
        if isinstance(started_at, datetime):
            return started_at
        return datetime.utcnow()

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        return {}

