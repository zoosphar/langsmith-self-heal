from __future__ import annotations

import json
import re
from dataclasses import dataclass

import structlog
from anthropic import AsyncAnthropic
from pydantic import ValidationError

from app.config import Settings
from app.models import BucketType, DiffProposal, FailingExample

logger = structlog.get_logger(__name__)


@dataclass
class ClaudeSuggester:
    settings: Settings

    def __post_init__(self) -> None:
        self._client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)

    async def suggest_atomic_diff(
        self,
        *,
        tool_name: str,
        bucket: BucketType,
        file_path: str,
        source_code: str,
        failures: list[FailingExample],
        current_score: float,
    ) -> DiffProposal:
        system_prompt = (
            "You are a senior Python reliability engineer.\n"
            "Return only valid JSON with keys: bucket, file_path, diff, reasoning.\n"
            "The diff must be ONE atomic change and reversible.\n"
            "Prefer this diff format:\n"
            "<<<SEARCH\\n<exact text to replace>\\n===\\n<replacement text>\\n>>>REPLACE\n"
            "Do not include markdown fences."
        )

        user_prompt = self._build_user_prompt(
            tool_name=tool_name,
            bucket=bucket,
            file_path=file_path,
            source_code=source_code,
            failures=failures,
            current_score=current_score,
        )

        response = await self._client.messages.create(
            model="claude-sonnet-4-20250514",
            temperature=0.1,
            max_tokens=1800,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text_blocks = [block.text for block in response.content if hasattr(block, "text")]
        raw_text = "\n".join(text_blocks).strip()
        payload = self._extract_json(raw_text)

        try:
            proposal = DiffProposal.model_validate(payload)
        except ValidationError as exc:
            logger.exception("invalid_diff_proposal_payload", error=str(exc), payload=payload)
            raise ValueError("Claude response did not match required proposal schema.") from exc

        return proposal

    def _build_user_prompt(
        self,
        *,
        tool_name: str,
        bucket: BucketType,
        file_path: str,
        source_code: str,
        failures: list[FailingExample],
        current_score: float,
    ) -> str:
        failure_blob = "\n\n".join(
            [
                f"Example ID: {item.example_id}\n"
                f"Input: {json.dumps(item.input_payload, ensure_ascii=True)}\n"
                f"Expected: {item.expected_output}\n"
                f"Actual: {item.actual_output}\n"
                f"Trace: {item.trace_id}\n"
                f"Error: {item.error_message}"
                for item in failures[:8]
            ]
        )
        return (
            f"Tool name: {tool_name}\n"
            f"Current score: {current_score:.4f}\n"
            f"Classified bucket: {bucket.value}\n"
            f"Target file path: {file_path}\n\n"
            "Current source file:\n"
            f"{source_code}\n\n"
            "Failing examples and traces:\n"
            f"{failure_blob}\n\n"
            "Produce one precise diff only for the provided file path."
        )

    @staticmethod
    def _extract_json(raw_text: str) -> dict[str, str]:
        try:
            payload = json.loads(raw_text)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if not match:
            raise ValueError("No JSON object found in Claude response.")
        payload = json.loads(match.group(0))
        if not isinstance(payload, dict):
            raise ValueError("Claude response JSON payload is not an object.")
        return payload

